"""Background snapshot behavior of the sandbox sweep (Celery task).

Exercises the snapshot half of ``cleanup_idle_sandboxes_task`` — non-idle
RUNNING sandboxes get their changed sessions snapshotted in place — end-to-end
against real Postgres + Redis. Sandbox operations (``list_session_workspaces``,
``create_snapshot``) are routed through the ``StubSandboxManager`` from
``conftest.py``.

For sandboxes that are NOT idle, the sweep must never terminate pods or
change sandbox/session status — it only bounds data loss from ungraceful pod
death (kubelet eviction, node loss). The reap half is covered by
``test_idle_cleanup.py``.
"""

from __future__ import annotations

import datetime
import logging
from collections.abc import Generator
from uuid import UUID
from uuid import uuid4

import pytest
from sqlalchemy import update
from sqlalchemy.orm import Session

from onyx.configs.constants import OnyxRedisLocks
from onyx.db.enums import BuildSessionStatus
from onyx.db.enums import SandboxStatus
from onyx.db.models import BuildSession
from onyx.db.models import Sandbox
from onyx.db.models import Snapshot
from onyx.db.models import User
from onyx.redis.redis_pool import get_redis_client
from onyx.server.features.build.sandbox.models import SnapshotResult
from onyx.server.features.build.sandbox.tasks import tasks as tasks_module
from onyx.server.features.build.sandbox.tasks.tasks import cleanup_idle_sandboxes_task
from tests.external_dependency_unit.constants import TEST_TENANT_ID
from tests.external_dependency_unit.craft._test_helpers import make_sandbox
from tests.external_dependency_unit.craft._test_helpers import make_user
from tests.external_dependency_unit.craft.stubs import StubSandboxManager

_DIGEST = "a" * 64

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def stubbed_sweep(
    stub_sandbox_manager: StubSandboxManager,
    monkeypatch: pytest.MonkeyPatch,
) -> StubSandboxManager:
    """Wire the stub so the sweep runs entirely against it."""
    monkeypatch.setattr(
        tasks_module, "get_sandbox_manager", lambda: stub_sandbox_manager
    )
    return stub_sandbox_manager


@pytest.fixture(autouse=True)
def _quiesce_leaked_sandboxes(db_session: Session) -> None:
    """Terminate RUNNING sandboxes leaked by earlier tests.

    The sweep covers ALL RUNNING sandboxes globally, so rows committed by
    other tests in this directory would otherwise leak into our assertions.
    """
    db_session.execute(
        update(Sandbox)
        .where(Sandbox.status == SandboxStatus.RUNNING)
        .values(status=SandboxStatus.TERMINATED)
    )
    db_session.commit()


@pytest.fixture(autouse=True)
def _isolated_redis_lock() -> Generator[None, None, None]:
    """Make sure the sweep beat lock is free before + after."""
    redis_client = get_redis_client(tenant_id=TEST_TENANT_ID)
    redis_client.delete(OnyxRedisLocks.CLEANUP_IDLE_SANDBOXES_BEAT_LOCK)
    try:
        yield
    finally:
        redis_client.delete(OnyxRedisLocks.CLEANUP_IDLE_SANDBOXES_BEAT_LOCK)


def _make_session(db_session: Session, user: User) -> BuildSession:
    session_row = BuildSession(
        user_id=user.id,
        name="background-snapshot-session",
        status=BuildSessionStatus.ACTIVE,
    )
    db_session.add(session_row)
    db_session.commit()
    db_session.refresh(session_row)
    return session_row


def _add_snapshot(
    db_session: Session,
    session_id: UUID,
    *,
    age_seconds: int,
    digest: str | None = None,
) -> Snapshot:
    """Insert a snapshot row backdated by ``age_seconds``."""
    name = f"{uuid4()}.{digest}.tar.gz" if digest else f"{uuid4()}.tar.gz"
    snapshot = Snapshot(
        session_id=session_id,
        storage_path=f"sandbox-snapshots/test/{name}",
        size_bytes=100,
    )
    db_session.add(snapshot)
    db_session.commit()
    db_session.execute(
        update(Snapshot)
        .where(Snapshot.id == snapshot.id)
        .values(
            created_at=datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(seconds=age_seconds)
        )
    )
    db_session.commit()
    db_session.refresh(snapshot)
    return snapshot


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_running_sandbox_snapshotted_without_termination(
    db_session: Session,
    test_user: User,  # noqa: ARG001
    stubbed_sweep: StubSandboxManager,
) -> None:
    """Happy path: snapshot is recorded; pod and statuses are untouched."""
    user = make_user(db_session)
    sandbox = make_sandbox(db_session, user)
    session_row = _make_session(db_session, user)
    db_session.commit()

    stubbed_sweep.list_session_workspaces_returns = [session_row.id]
    stubbed_sweep.create_snapshot_returns = SnapshotResult(
        storage_path=f"s3://snapshots/{sandbox.id}/{uuid4()}.{_DIGEST}.tar.gz",
        size_bytes=4321,
    )

    cleanup_idle_sandboxes_task.run(tenant_id=TEST_TENANT_ID)

    db_session.expire_all()
    snapshots = (
        db_session.query(Snapshot).filter(Snapshot.session_id == session_row.id).all()
    )
    assert len(snapshots) == 1
    assert snapshots[0].size_bytes == 4321

    refreshed = db_session.get(Sandbox, sandbox.id)
    assert refreshed is not None
    assert refreshed.status == SandboxStatus.RUNNING
    refreshed_session = db_session.get(BuildSession, session_row.id)
    assert refreshed_session is not None
    assert refreshed_session.status == BuildSessionStatus.ACTIVE
    assert stubbed_sweep.terminate_count == 0


def test_fresh_snapshot_skipped_by_age_gate(
    db_session: Session,
    test_user: User,  # noqa: ARG001
    stubbed_sweep: StubSandboxManager,
) -> None:
    """Sessions whose latest snapshot is newer than the interval are skipped
    without any pod traffic."""
    user = make_user(db_session)
    make_sandbox(db_session, user)
    session_row = _make_session(db_session, user)
    _add_snapshot(db_session, session_row.id, age_seconds=10)

    stubbed_sweep.list_session_workspaces_returns = [session_row.id]

    cleanup_idle_sandboxes_task.run(tenant_id=TEST_TENANT_ID)

    assert stubbed_sweep.create_snapshot_count == 0


def test_stale_snapshot_passes_previous_digest(
    db_session: Session,
    test_user: User,  # noqa: ARG001
    stubbed_sweep: StubSandboxManager,
) -> None:
    """A stale session is re-snapshotted with the latest snapshot's digest so
    the pod can dedupe unchanged workspaces."""
    user = make_user(db_session)
    sandbox = make_sandbox(db_session, user)
    session_row = _make_session(db_session, user)

    interval = tasks_module.SANDBOX_PERIODIC_SNAPSHOT_INTERVAL_SECONDS
    _add_snapshot(db_session, session_row.id, age_seconds=interval * 2, digest=_DIGEST)

    stubbed_sweep.list_session_workspaces_returns = [session_row.id]
    stubbed_sweep.create_snapshot_returns = SnapshotResult(
        storage_path=f"s3://snapshots/{sandbox.id}/{uuid4()}.{'b' * 64}.tar.gz",
        size_bytes=999,
    )

    cleanup_idle_sandboxes_task.run(tenant_id=TEST_TENANT_ID)

    assert stubbed_sweep.create_snapshot_count == 1
    assert stubbed_sweep.last_create_snapshot_payload is not None
    assert stubbed_sweep.last_create_snapshot_payload["previous_digest"] == _DIGEST

    db_session.expire_all()
    snapshots = (
        db_session.query(Snapshot).filter(Snapshot.session_id == session_row.id).all()
    )
    assert len(snapshots) == 2


def test_unchanged_workspace_creates_no_new_row(
    db_session: Session,
    test_user: User,  # noqa: ARG001
    stubbed_sweep: StubSandboxManager,
) -> None:
    """An unchanged result (digest match in the pod) is a successful no-op:
    no new row, no error."""
    user = make_user(db_session)
    make_sandbox(db_session, user)
    session_row = _make_session(db_session, user)

    interval = tasks_module.SANDBOX_PERIODIC_SNAPSHOT_INTERVAL_SECONDS
    stale = _add_snapshot(
        db_session, session_row.id, age_seconds=interval * 2, digest=_DIGEST
    )
    stale_id = stale.id

    stubbed_sweep.list_session_workspaces_returns = [session_row.id]
    stubbed_sweep.create_snapshot_returns = SnapshotResult(
        storage_path="", size_bytes=0, unchanged=True
    )

    cleanup_idle_sandboxes_task.run(tenant_id=TEST_TENANT_ID)

    assert stubbed_sweep.create_snapshot_count == 1
    db_session.expire_all()
    remaining = (
        db_session.query(Snapshot).filter(Snapshot.session_id == session_row.id).all()
    )
    assert [s.id for s in remaining] == [stale_id]


def test_snapshot_failure_continues_other_sessions(
    db_session: Session,
    test_user: User,  # noqa: ARG001
    stubbed_sweep: StubSandboxManager,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failing ``create_snapshot`` is logged and the sweep continues."""
    user = make_user(db_session)
    make_sandbox(db_session, user)
    session_a = _make_session(db_session, user)
    session_b = _make_session(db_session, user)

    stubbed_sweep.list_session_workspaces_returns = [session_a.id, session_b.id]

    real_result = SnapshotResult(
        storage_path=f"s3://snapshots/{uuid4()}.tar.gz", size_bytes=55
    )

    def _snapshot(
        _sandbox_id: object,
        session_id: object,
        _tenant_id: object,
        previous_digest: str | None = None,  # noqa: ARG001
    ) -> SnapshotResult:
        if session_id == session_a.id:
            raise RuntimeError("FileStore unreachable")
        return real_result

    monkeypatch.setattr(stubbed_sweep, "create_snapshot", _snapshot)

    with caplog.at_level(logging.WARNING):
        cleanup_idle_sandboxes_task.run(tenant_id=TEST_TENANT_ID)

    db_session.expire_all()
    snapshots_a = (
        db_session.query(Snapshot).filter(Snapshot.session_id == session_a.id).all()
    )
    snapshots_b = (
        db_session.query(Snapshot).filter(Snapshot.session_id == session_b.id).all()
    )
    assert snapshots_a == []
    assert len(snapshots_b) == 1
    assert any("Failed to create snapshot" in r.getMessage() for r in caplog.records)


def test_no_running_sandboxes_is_a_noop(
    db_session: Session,
    test_user: User,  # noqa: ARG001
    stubbed_sweep: StubSandboxManager,
) -> None:
    """SLEEPING sandboxes are never swept."""
    user = make_user(db_session)
    make_sandbox(db_session, user, status=SandboxStatus.SLEEPING)
    db_session.commit()

    cleanup_idle_sandboxes_task.run(tenant_id=TEST_TENANT_ID)

    assert stubbed_sweep.list_session_workspaces_count == 0
    assert stubbed_sweep.create_snapshot_count == 0
