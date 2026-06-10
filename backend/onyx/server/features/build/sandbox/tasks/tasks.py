"""Celery tasks for sandbox operations (cleanup, etc.)."""

import datetime

from celery import shared_task
from celery import Task
from redis.lock import Lock as RedisLock

from onyx.background.celery.apps.app_base import task_logger
from onyx.configs.constants import OnyxCeleryTask
from onyx.configs.constants import OnyxRedisLocks
from onyx.db.engine.sql_engine import get_session_with_current_tenant
from onyx.db.enums import SandboxStatus
from onyx.db.models import Sandbox
from onyx.file_store.file_store import get_default_file_store
from onyx.redis.redis_pool import get_redis_client
from onyx.redis.redis_tenant_work_gating import maybe_mark_tenant_active
from onyx.server.features.build.configs import SANDBOX_IDLE_TIMEOUT_SECONDS
from onyx.server.features.build.configs import (
    SANDBOX_PERIODIC_SNAPSHOT_INTERVAL_SECONDS,
)
from onyx.server.features.build.configs import SNAPSHOT_KEEP_LAST_N
from onyx.server.features.build.configs import SNAPSHOT_RETENTION_DAYS
from onyx.server.features.build.db.build_session import clear_nextjs_ports_for_user
from onyx.server.features.build.db.build_session import (
    mark_user_sessions_idle__no_commit,
)
from onyx.server.features.build.db.sandbox import create_snapshot__no_commit
from onyx.server.features.build.db.sandbox import get_latest_snapshot_for_session
from onyx.server.features.build.db.sandbox import get_prunable_snapshots
from onyx.server.features.build.db.sandbox import get_running_sandboxes
from onyx.server.features.build.db.sandbox import update_sandbox_status__no_commit
from onyx.server.features.build.sandbox.base import get_sandbox_manager
from onyx.server.features.build.sandbox.manager.snapshot_manager import (
    digest_from_storage_path,
)
from onyx.server.features.build.sandbox.manager.snapshot_manager import SnapshotManager

# 100 minutes - snapshotting can take time
TIMEOUT_SECONDS = 6000

# Snapshot pruning is I/O-light; cap its runtime well under the beat cadence.
SNAPSHOT_CLEANUP_TIMEOUT_SECONDS = 600


def _is_idle(sandbox: Sandbox, now: datetime.datetime) -> bool:
    """Idle = no heartbeat for the timeout (NULL heartbeat falls back to
    created_at so legacy/edge-case rows don't sit RUNNING forever)."""
    reference = sandbox.last_heartbeat or sandbox.created_at
    return reference < now - datetime.timedelta(seconds=SANDBOX_IDLE_TIMEOUT_SECONDS)


@shared_task(
    name=OnyxCeleryTask.CLEANUP_IDLE_SANDBOXES,
    soft_time_limit=TIMEOUT_SECONDS,
    bind=True,
    ignore_result=True,
)
def cleanup_idle_sandboxes_task(self: Task, *, tenant_id: str) -> None:  # noqa: ARG001
    """Sweep RUNNING sandboxes: snapshot changed sessions, put idle ones to sleep.

    Background snapshots bound data loss from ungraceful pod death (kubelet
    eviction, node loss, spot reclaim) to
    ~SANDBOX_PERIODIC_SNAPSHOT_INTERVAL_SECONDS. Two gates keep the sweep
    cheap: an age gate (skip fresh-snapshotted sessions without touching the
    pod; not applied at reap) and the digest dedupe (``previous_digest`` — the
    pod skips re-archiving unchanged workspaces). Because the freshest
    snapshot tracks workspace state, the reap-time snapshot is usually an
    unchanged no-op and reap is effectively a delete. Reap stays fail-closed:
    snapshot failure on a reachable pod keeps the sandbox RUNNING for retry
    next sweep.
    """
    task_logger.info(f"cleanup_idle_sandboxes_task starting for tenant {tenant_id}")

    redis_client = get_redis_client(tenant_id=tenant_id)
    lock: RedisLock = redis_client.lock(
        OnyxRedisLocks.CLEANUP_IDLE_SANDBOXES_BEAT_LOCK,
        timeout=TIMEOUT_SECONDS,
    )

    # Prevent overlapping runs of this task
    if not lock.acquire(blocking=False):
        task_logger.info("cleanup_idle_sandboxes_task - lock not acquired, skipping")
        return

    try:
        sandbox_manager = get_sandbox_manager()

        with get_session_with_current_tenant() as db_session:
            running_sandboxes = get_running_sandboxes(db_session)
            if not running_sandboxes:
                task_logger.debug("No running sandboxes found")
                return

            # Tenant-work-gating hook: refresh this tenant's active-set
            # membership whenever the sweep has work to do.
            maybe_mark_tenant_active(tenant_id, caller="sandbox_cleanup")

            now = datetime.datetime.now(datetime.timezone.utc)
            snapshot_cutoff = now - datetime.timedelta(
                seconds=SANDBOX_PERIODIC_SNAPSHOT_INTERVAL_SECONDS
            )

            for sandbox in running_sandboxes:
                sandbox_id = sandbox.id
                idle = _is_idle(sandbox, now)

                try:
                    # List session directories in the sandbox via the
                    # backend-agnostic manager API. K8s lists pod paths via
                    # exec; Docker lists container paths via exec; Local
                    # walks the on-disk sessions/ directory.
                    session_ids = sandbox_manager.list_session_workspaces(sandbox_id)

                    snapshot_failed = False
                    for session_id in session_ids:
                        try:
                            latest = get_latest_snapshot_for_session(
                                db_session, session_id
                            )
                            if (
                                not idle
                                and latest
                                and latest.created_at > snapshot_cutoff
                            ):
                                continue

                            previous_digest = (
                                digest_from_storage_path(latest.storage_path)
                                if latest
                                else None
                            )
                            snapshot_result = sandbox_manager.create_snapshot(
                                sandbox_id,
                                session_id,
                                tenant_id,
                                previous_digest=previous_digest,
                            )
                            if snapshot_result and snapshot_result.unchanged:
                                # Workspace identical to the last snapshot; reuse
                                # it instead of writing a duplicate full tarball.
                                task_logger.debug(
                                    f"Workspace unchanged for session {session_id}; "
                                    f"reusing prior snapshot"
                                )
                            elif snapshot_result:
                                # Create DB record for the snapshot. The tree
                                # digest is embedded in storage_path, not a column.
                                create_snapshot__no_commit(
                                    db_session,
                                    session_id,
                                    snapshot_result.storage_path,
                                    snapshot_result.size_bytes,
                                )
                                db_session.commit()
                                task_logger.info(
                                    f"Snapshot created for session {session_id}"
                                )
                        except Exception as e:
                            snapshot_failed = True
                            task_logger.warning(
                                f"Failed to create snapshot for session {session_id}: {e}"
                            )
                            db_session.rollback()

                    if not idle:
                        continue

                    task_logger.info(f"Putting sandbox {sandbox_id} to sleep")

                    # Fail-closed: terminating with an unsnapshotted workspace
                    # loses it (restore falls back to a fresh template). Keep the
                    # sandbox RUNNING to retry next cycle — unless the pod is
                    # unreachable, where snapshots can never succeed and the
                    # workspace is already gone, so don't pin it RUNNING forever.
                    if snapshot_failed:
                        if sandbox_manager.health_check(sandbox_id, timeout=5.0):
                            task_logger.error(
                                f"Snapshot failed for sandbox {sandbox_id}; "
                                f"leaving it RUNNING to retry next cycle"
                            )
                            continue
                        task_logger.warning(
                            f"Sandbox {sandbox_id} pod is unreachable; "
                            f"terminating despite snapshot failure (cannot recover "
                            f"its workspace, won't pin it RUNNING forever)"
                        )

                    # Terminate the pod (but keep sandbox record)
                    sandbox_manager.terminate(sandbox_id)

                    # Zero out nextjs ports for all sessions (ports are no longer in use)
                    cleared = clear_nextjs_ports_for_user(db_session, sandbox.user_id)
                    task_logger.debug(
                        f"Cleared {cleared} nextjs_port allocations for user {sandbox.user_id}"
                    )

                    # Mark all active sessions as IDLE
                    idled = mark_user_sessions_idle__no_commit(
                        db_session, sandbox.user_id
                    )
                    task_logger.debug(
                        f"Marked {idled} sessions as IDLE for user {sandbox.user_id}"
                    )

                    update_sandbox_status__no_commit(
                        db_session, sandbox_id, SandboxStatus.SLEEPING
                    )
                    db_session.commit()
                    task_logger.info(f"Sandbox {sandbox_id} is now sleeping")

                except Exception as e:
                    task_logger.error(
                        f"Failed to sweep sandbox {sandbox_id}: {e}",
                        exc_info=True,
                    )
                    db_session.rollback()

    except Exception:
        task_logger.exception("Error in cleanup_idle_sandboxes_task")
        raise

    finally:
        if lock.owned():
            lock.release()

    task_logger.info("cleanup_idle_sandboxes_task completed")


@shared_task(
    name=OnyxCeleryTask.CLEANUP_OLD_SNAPSHOTS,
    soft_time_limit=SNAPSHOT_CLEANUP_TIMEOUT_SECONDS,
    bind=True,
    ignore_result=True,
)
def cleanup_old_snapshots_task(self: Task, *, tenant_id: str) -> None:  # noqa: ARG001
    """Prune expired/excess session snapshots from the file store and DB.

    Enforces SNAPSHOT_RETENTION_DAYS + SNAPSHOT_KEEP_LAST_N (see
    onyx.server.features.build.db.sandbox.get_prunable_snapshots). The most
    recent snapshot per session is always retained so a workspace is never
    orphaned. Blobs are deleted before their DB rows: if a blob delete fails,
    the row is left in place and retried next cycle, so we never leak storage.
    """
    redis_client = get_redis_client(tenant_id=tenant_id)
    lock: RedisLock = redis_client.lock(
        OnyxRedisLocks.CLEANUP_OLD_SNAPSHOTS_BEAT_LOCK,
        timeout=SNAPSHOT_CLEANUP_TIMEOUT_SECONDS,
    )

    if not lock.acquire(blocking=False):
        task_logger.info("cleanup_old_snapshots_task - lock not acquired, skipping")
        return

    try:
        snapshot_manager = SnapshotManager(get_default_file_store())

        with get_session_with_current_tenant() as db_session:
            prunable = get_prunable_snapshots(
                db_session,
                retention_days=SNAPSHOT_RETENTION_DAYS,
                keep_last_n=SNAPSHOT_KEEP_LAST_N,
            )

            if not prunable:
                task_logger.debug("No snapshots to prune")
                return

            maybe_mark_tenant_active(tenant_id, caller="snapshot_cleanup")
            task_logger.info(f"Pruning {len(prunable)} expired/excess snapshots")

            deleted = 0
            for snapshot in prunable:
                try:
                    snapshot_manager.delete_snapshot(snapshot.storage_path)
                except Exception as e:
                    task_logger.warning(
                        f"Skipping snapshot {snapshot.id}; blob delete failed "
                        f"(will retry next cycle): {e}"
                    )
                    continue
                db_session.delete(snapshot)
                deleted += 1

            db_session.commit()
            task_logger.info(f"Pruned {deleted} snapshots")

    except Exception:
        task_logger.exception("Error in cleanup_old_snapshots_task")
        raise

    finally:
        if lock.owned():
            lock.release()
