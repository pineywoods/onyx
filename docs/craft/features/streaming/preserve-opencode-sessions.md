# Preserve opencode sessions

## Context

Craft sessions use `opencode serve` as the long-lived agent runtime inside a
sandbox. Onyx persists the `BuildSession.opencode_session_id` in Postgres so a
later turn can reconnect to the same opencode session instead of creating a
fresh one every message.

That ID alone is not enough after a Kubernetes sandbox sleeps, is evicted, or is
recreated. The opencode session rows live inside opencode's SQLite history DB in
the sandbox filesystem. If the sandbox-level DB is not persisted and restored,
the Postgres ID points at nothing.

The current implementation persists opencode history as sandbox-global state,
separate from normal per-session workspace snapshots.

## Goals

1. Preserve opencode's SQLite session history across Kubernetes sandbox sleep,
   recovery, and reprovision.
2. Keep durable storage ownership in the API server / FileStore layer, not in
   sandbox pods.
3. Keep normal session snapshots focused on per-session files.
4. Avoid touching Docker sandbox manager behavior for this change.
5. Be optimistic when a saved opencode ID is missing from a restored DB: mint a
   replacement ID and persist it. A later follow-up can replay saved chat history
   into that replacement session.
6. Avoid resurrecting deleted opencode sessions from a stale durable history
   archive.

## Storage Model

There are two distinct persistence surfaces.

### Per-session workspace snapshots

Normal session snapshots capture only session-local user output:

- `outputs/`
- `attachments/` when present and non-empty

They deliberately do not capture `.opencode-data`. These archives are created
and restored by the sandbox sidecar through:

- `POST /snapshot/create`
- `POST /snapshot/restore/{session_id}`

The sidecar owns local filesystem access. The API server streams the archive
into FileStore through `SnapshotManager`.

### Sandbox-global opencode history

Opencode history is shared by all BuildSessions in a sandbox. In Kubernetes the
pod is configured with:

- `OPENCODE_DATA_HOME=/workspace/sessions/.opencode-data`
- `OPENCODE_PAUSE_FILE=/workspace/sessions/.opencode-serve-paused`

The history DB path is:

```text
/workspace/sessions/.opencode-data/opencode/opencode.db
```

The durable FileStore object is deterministic per sandbox:

```text
sandbox-snapshots/{tenant_id}/{sandbox_id}/opencode-history.tar.gz
```

This archive contains exactly:

```text
.opencode-data/opencode/opencode.db
```

That narrow format is intentional. It keeps opencode persistence separate from
session workspaces while avoiding a custom per-session opencode store that would
not match opencode's actual sandbox-level DB.

## Important Components

### `SnapshotManager`

`backend/onyx/server/features/build/sandbox/manager/snapshot_manager.py`

Owns FileStore persistence for both normal session snapshots and sandbox-global
opencode history snapshots. It enforces the API-server-side archive size cap and
uses the deterministic opencode history storage path above.

### Sandbox sidecar snapshot endpoints

`backend/onyx/server/features/build/sandbox/image/sandbox_daemon/server.py`

The sidecar exposes local filesystem operations as signed HTTP endpoints. It
does not upload to S3 and does not know tenant storage credentials.

For opencode history:

- `POST /opencode-history/create` returns `204` when no live opencode DB exists,
  otherwise streams a gzip archive.
- `POST /opencode-history/restore` accepts a signed, hash-verified archive body
  and restores the DB locally.

### Opencode history archive helpers

`backend/onyx/server/features/build/sandbox/image/sandbox_daemon/opencode_history.py`

This module owns the SQLite-specific archive logic:

- find and validate the opencode DB path under `/workspace/sessions`
- copy the live SQLite DB with SQLite's backup API
- run `PRAGMA quick_check`
- create a narrow tar.gz archive
- validate restore archives before extraction
- reject links, special files, path traversal, unexpected roots, unexpected
  files, duplicate DB members, and oversized payloads
- replace only the opencode DB parent directory during restore

This is separate from `snapshot.py`, which now remains focused on normal
session workspace snapshotting.

### Kubernetes sandbox manager

`backend/onyx/server/features/build/sandbox/kubernetes/kubernetes_sandbox_manager.py`

The K8s manager coordinates pod lifecycle, sidecar calls, FileStore streaming,
and `opencode serve` pause/resume around restore.

It is the only backend currently advertising:

```python
supports_opencode_history_persistence = True
```

The Kubernetes sandbox backend requires Kubernetes 1.33 or newer. The manager
checks the server version during initialization and raises a clear error on
older clusters instead of carrying compatibility branches for pre-1.33
behavior.

## Provision And Restore Flow

When a Kubernetes sandbox is provisioned or reused:

1. The pod runs `opencode serve` with `XDG_DATA_HOME` pointed at
   `/workspace/sessions/.opencode-data`.
2. The K8s manager waits for the sidecar and `opencode serve` readiness.
3. If no durable opencode history snapshot exists, the manager marks history as
   restored and continues.
4. If a durable history snapshot exists, the API server reads it from FileStore
   into a bounded temp file and computes its SHA-256.
5. The manager creates the pause file inside the sandbox and terminates any
   currently running `opencode serve` process.
6. The manager posts the signed archive to `/opencode-history/restore`.
7. The sidecar validates and restores the DB.
8. The manager removes the pause file, waits for `opencode serve` to become
   ready again, and marks the pod with an in-pod restore marker.

The pause-file loop lives in `entrypoint.sh`. If the pause file exists,
`opencode serve` waits rather than opening the SQLite DB while restore is in
progress.

For a newly created pod, the provisioner performs the restore and writes the
marker. For a concurrent caller that finds an already-running pod, the manager
waits for that marker before using the sandbox.

## Snapshot Creation Flow

Opencode history snapshots are created before a sandbox sleeps and in a few
recovery/delete paths.

1. The K8s manager signs an empty request to `/opencode-history/create`.
2. The sidecar checks whether the live DB exists.
3. If no DB exists, the sidecar returns `204`.
4. If the DB exists, the sidecar uses SQLite backup into a temporary DB, runs
   SQLite integrity checks, creates the narrow tar.gz archive, and streams it
   back.
5. The API server streams the response into `SnapshotManager`.
6. `SnapshotManager` stores it at the stable sandbox-level FileStore key.

The default behavior for a `204` empty live store is to preserve any existing
durable history archive. That default is important for idle/recovery paths: a
transient live empty/missing DB should not destroy the last known good history.

There is one explicit exception. Session deletion passes
`delete_existing_if_empty=True`. In that case, if the live store is empty after
deleting a session, the K8s manager deletes the durable history archive so a
future restore cannot resurrect stale opencode sessions.

## Send Message Flow

The prompt path is intentionally optimistic.

1. The session row carries `BuildSession.opencode_session_id` when one has been
   persisted.
2. Before a turn, `_ensure_opencode_session_id` mints and persists an opencode
   session ID if the row has none.
3. `yield_sandbox_events` calls `sandbox_manager.send_message` with the saved
   ID and an `on_opencode_session_resolved` callback.
4. `_send_message_via_serve` calls `OpencodeServeClient.ensure_session`.
5. If the saved ID exists, opencode returns `200` and the same ID is reused.
6. If opencode returns `404`, Onyx creates a fresh opencode session and invokes
   the callback so the BuildSession row is updated.
7. Non-404 lookup errors still raise. A runtime outage should not silently mint
   a replacement session.
8. The message is sent to the resolved opencode session and normal event
   streaming proceeds.

This means a restored sandbox with a missing opencode ID does not fail the user
turn. It starts a new opencode session and records the new ID. The tradeoff is
that opencode itself does not yet receive prior chat history in that newly
created session. That replay behavior is intentionally out of scope for this
change.

## Delete Session Flow

Deleting a BuildSession is special because opencode history is sandbox-global.
If the live DB still contains the opencode session, deleting only the SQL row
and workspace is not enough; a future history restore could bring the opencode
session back.

For an active sandbox:

1. `SessionManager.delete_session` acquires the session prompt slot.
2. If the session has saved chat history but no persisted opencode ID, deletion
   fails. There is no reliable live opencode session to remove from the shared
   DB.
3. If an opencode ID exists, the manager calls `delete_opencode_session`.
4. `delete_opencode_session` uses a unary `OpencodeServeClient` without event
   bus setup.
5. After successful live deletion, the manager snapshots opencode history with
   `delete_existing_if_empty=True`.
6. Normal workspace cleanup and Snapshot FileStore cleanup proceed.
7. The BuildSession DB row is deleted.

For a sleeping or otherwise not-running sandbox:

- If there is no durable opencode history snapshot, the DB/session cleanup can
  proceed.
- If there is a durable opencode history snapshot and the deleted session may be
  represented in it, deletion fails. The sandbox must be running so the live
  opencode session can be removed and the shared history can be resnapshotted.

This is stricter than the send-message path. Send can optimistically mint a new
session because it is forward progress. Delete must avoid leaving durable shared
state stale.

## Idle Sleep Flow

The sandbox cleanup task handles idle running sandboxes.

1. If the backend supports opencode history persistence, it first attempts an
   opencode history snapshot.
2. If that snapshot fails but the sandbox still passes health check, the task
   leaves the sandbox running. Sleeping a healthy sandbox without fresh history
   would risk losing recent agent context.
3. If the pod is already unreachable, the task logs a warning and continues
   cleanup. At that point the live filesystem cannot be trusted or accessed for
   a fresh snapshot.
4. The task then snapshots each session workspace.
5. The sandbox can be put to sleep after required snapshots complete.

## Recovery Flow

When Onyx detects an unhealthy running sandbox and needs to terminate/recover it,
the lifecycle code attempts a best-effort opencode history snapshot before
termination.

This path is best-effort because the sandbox may already be partially dead. A
failure is logged but does not block recovery forever.

On reprovision, the normal restore flow restores the last durable opencode
history snapshot if one exists.

## Reset / Start Fresh Flow

User-requested sandbox reset is a destructive "start fresh" operation.

1. Terminate the sandbox resources.
2. Commit the sandbox DB row as `TERMINATED`.
3. Delete the durable opencode history snapshot.
4. Return success only after the durable history delete succeeds.

This is intentionally different from idle sleep. Sleep preserves history; reset
removes it.

## Why This Is Not A Normal Session Snapshot

The normal snapshot loop iterates session directories and stores each session's
workspace. That is the right model for outputs and attachments.

Opencode history is different:

- opencode stores all sessions in one sandbox-level SQLite DB
- a per-session archive cannot safely represent the shared DB
- multiple BuildSessions can share the same opencode history store
- deleting one session requires updating the shared DB and then resnapshotting
  it
- reset must delete the shared archive, not merge or preserve per-session stores

So the implementation reuses the same high-level snapshot infrastructure
(`SnapshotManager`, FileStore, signed sidecar streaming), but keeps opencode
history as a sandbox-level archive with its own create/restore endpoints and
policy.

## Operational Invariants

- The sandbox pod does not own durable storage credentials.
- The API server owns FileStore reads/writes.
- The sidecar owns pod-local filesystem reads/writes.
- Normal session snapshots do not contain `.opencode-data`.
- Opencode history snapshots contain only `.opencode-data/opencode/opencode.db`.
- Restore archives are treated as untrusted and are validated before extraction.
- SQLite DBs are copied through SQLite backup, not raw file copy.
- `opencode serve` is paused and killed before DB restore, then restarted.
- A missing saved opencode ID during send mints a new session and persists it.
- Runtime lookup errors other than `404` still fail the turn.
- Session delete updates the shared opencode DB before deleting the BuildSession
  row.
- Reset deletes durable opencode history after terminating the sandbox.

## Known Follow-Up

If a restored sandbox does not contain the saved opencode ID, Onyx now mints a
new opencode session and persists it. That avoids blocking the user, but the
new opencode session does not yet contain prior chat history.

The planned follow-up is to detect this replacement-session case and replay the
saved BuildMessage history into opencode before sending the next user prompt.
That should live above the low-level snapshot/restore path. The snapshot layer
should continue to restore the DB when possible and stay storage-focused.

## Files Worth Reading

- `backend/onyx/server/features/build/sandbox/image/sandbox_daemon/opencode_history.py`
- `backend/onyx/server/features/build/sandbox/image/sandbox_daemon/server.py`
- `backend/onyx/server/features/build/sandbox/manager/snapshot_manager.py`
- `backend/onyx/server/features/build/sandbox/kubernetes/kubernetes_sandbox_manager.py`
- `backend/onyx/server/features/build/sandbox/opencode/serve_client.py`
- `backend/onyx/server/features/build/sandbox/serve_transport.py`
- `backend/onyx/server/features/build/session/streaming.py`
- `backend/onyx/server/features/build/session/manager.py`
