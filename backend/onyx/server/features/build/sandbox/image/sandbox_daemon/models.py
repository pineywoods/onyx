"""Request/response models shared between the sandbox daemon and the api-server.

Both sides import these to keep the wire schema in sync. The daemon imports
them as ``sandbox_daemon.models`` (the Dockerfile copies ``sandbox_daemon/``
to ``/workspace/sandbox_daemon/``); the api-server imports the full module
path.
"""

from uuid import UUID

from pydantic import BaseModel
from pydantic import ConfigDict


class SnapshotCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: UUID
    # Digest of the last persisted snapshot, if any. When the current workspace
    # digest matches, the daemon skips re-archiving and returns 204 with the
    # ``X-Snapshot-Unchanged`` header so the caller can reuse the prior snapshot.
    previous_digest: str | None = None


# Restore has no response body — failures raise, success is the 204.
