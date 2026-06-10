# Snapshot Retention & Skip-Unchanged

## What a snapshot is

When a Craft sandbox goes idle, the cleanup task snapshots each session's
workspace (`outputs/`, `attachments/`, `.opencode-data/`) into a `tar.gz` and
persists it through the Onyx FileStore, then terminates the pod. On wake, the
latest snapshot is restored. Snapshots are internal sleep/wake plumbing — they
are not a user-facing version history, and the user's most recent state is
never lost to retention (see below).

## Retention policy

Pruning runs in the `cleanup_old_snapshots` Celery beat task (daily, `SANDBOX`
queue). For each session, snapshots are ordered newest-first and:

- **Position 0 (the latest) is always kept** — the workspace anchor. A session
  is never left without a restorable snapshot, regardless of age.
- **Snapshots beyond `SNAPSHOT_KEEP_LAST_N` are pruned** (hard cap), even if
  recent. This bounds per-session storage.
- **Within the cap, snapshots older than `SNAPSHOT_RETENTION_DAYS` are pruned.**

Net effect: a user keeps their latest project state forever; only older history
and excess snapshots are cleaned up. Because the latest is always retained,
retention has **no user-visible data-loss impact** — there is nothing to warn
users about. The only thing reclaimed is redundant snapshot history in S3.

Selection lives in `get_prunable_snapshots` /
`_select_prunable_snapshots` (`onyx/server/features/build/db/sandbox.py`).

### Configuration

Both knobs are env-overridable (`onyx/server/features/build/configs.py`):

| Env var                  | Default | Meaning                                  |
| ------------------------ | ------- | ---------------------------------------- |
| `SNAPSHOT_RETENTION_DAYS`| `30`    | Age beyond which surplus snapshots prune |
| `SNAPSHOT_KEEP_LAST_N`   | `3`     | Newest-N per session always kept         |

To run a 90-day window, set `SNAPSHOT_RETENTION_DAYS=90` for the environment.

### Deletion is blob-then-row, fail-safe

`cleanup_old_snapshots_task` deletes the S3 blob first, then the `snapshot` DB
row. If the blob delete fails, the row is **left in place and retried next
cycle** — so we never delete a row while leaking its blob, and never orphan a
session. A row therefore only survives a prune cycle if its blob could not be
removed.

## Skip-unchanged

To avoid writing an identical full `tar.gz` every idle cycle (e.g. a user who
sleeps/wakes daily without editing), the sidecar computes a cheap digest of the
snapshot tree — `sha256` over sorted `(relpath, size, int(mtime))` — in
`compute_snapshot_digest` (`sandbox_daemon/snapshot.py`).

Flow:

1. The api-server passes the previous snapshot's digest to `/snapshot/create`
   via `previous_digest`. The previous digest is recovered from the prior
   snapshot's `storage_path` (it is embedded as
   `…/{snapshot_id}.{digest}.tar.gz`), so **no DB column is required**.
2. If the current digest matches, the sidecar returns `204` with
   `X-Snapshot-Unchanged: 1` and skips archiving. The cleanup task treats this
   as success and reuses the existing snapshot (no new blob, no new row).
3. Otherwise it streams the archive with `X-Snapshot-Digest`, which the
   api-server embeds into the new snapshot's `storage_path`.

Restore re-applies file mtimes (`os.utime`), and `tar` stores mtimes at
1-second granularity, so an untouched workspace yields the same digest across
sleep/wake — which is what makes the skip reliable.

Note: the digest/skip logic lives in the sandbox **image**, so changes to it
take effect only after the sandbox image is rebuilt and redeployed.
