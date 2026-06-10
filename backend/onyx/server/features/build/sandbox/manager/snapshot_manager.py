"""Snapshot management for sandbox state persistence."""

import tempfile
from pathlib import Path
from typing import IO
from uuid import uuid4

from onyx.configs.constants import FileOrigin
from onyx.file_store.file_store import FileStore
from onyx.utils.logger import setup_logger

logger = setup_logger()

# File type for snapshot archives
SNAPSHOT_FILE_TYPE = "application/gzip"


def digest_from_storage_path(storage_path: str) -> str | None:
    """Extract the tree digest embedded in a snapshot storage path, if any.

    Paths are ``.../{snapshot_id}.{digest}.tar.gz`` when a digest was recorded
    (``{snapshot_id}.tar.gz`` for older snapshots). The snapshot id is a UUID
    and the digest is hex, so neither contains ``.`` -- making the split
    unambiguous. Encoding the digest in the path avoids a dedicated DB column.
    """
    name = storage_path.rsplit("/", 1)[-1]
    if name.endswith(".tar.gz"):
        name = name[: -len(".tar.gz")]
    parts = name.split(".")
    return parts[1] if len(parts) > 1 else None


MAX_SNAPSHOT_ARCHIVE_BYTES = 100 * 1024 * 1024
_SNAPSHOT_COPY_CHUNK_BYTES = 8 * 1024 * 1024


def _copy_snapshot_stream_with_limit(
    source: IO[bytes],
    target: IO[bytes],
) -> int:
    size_bytes = 0
    while True:
        chunk = source.read(_SNAPSHOT_COPY_CHUNK_BYTES)
        if not chunk:
            break
        size_bytes += len(chunk)
        if size_bytes > MAX_SNAPSHOT_ARCHIVE_BYTES:
            raise RuntimeError(
                f"snapshot archive exceeds {MAX_SNAPSHOT_ARCHIVE_BYTES} byte limit"
            )
        target.write(chunk)
    return size_bytes


class SnapshotManager:
    """Manages sandbox snapshot creation and restoration.

    Snapshots are tar.gz archives of sandbox session state, stored using the
    file store abstraction.

    Responsible for:
    - Persisting sandbox-produced snapshot streams
    - Restoring stored snapshot streams back to sandbox managers
    - Deleting snapshots from storage
    """

    def __init__(self, file_store: FileStore) -> None:
        """Initialize SnapshotManager with a file store.

        Args:
            file_store: The file store to use for snapshot storage
        """
        self._file_store = file_store

    def create_snapshot_from_stream(
        self,
        stream: IO[bytes],
        sandbox_id: str,
        tenant_id: str,
        tree_digest: str | None = None,
    ) -> tuple[str, str, int]:
        """Persist an already-built tar.gz byte stream as a snapshot.

        This is used by backends (e.g. Docker) that produce the tar stream
        inside the sandbox container via exec and stream it back to the
        api_server, so no on-host outputs directory ever exists. The caller
        is responsible for producing a valid tar.gz stream.

        Args:
            stream: Binary, readable stream of tar.gz bytes.
            sandbox_id: Sandbox identifier (string form).
            tenant_id: Tenant identifier for multi-tenant isolation.

        Returns:
            Tuple of (snapshot_id, storage_path, size_bytes).
        """
        snapshot_id = str(uuid4())
        # Embed the tree digest in the path so the next snapshot can detect an
        # unchanged workspace without a dedicated DB column.
        digest_suffix = f".{tree_digest}" if tree_digest else ""
        storage_path = (
            f"sandbox-snapshots/{tenant_id}/{sandbox_id}/"
            f"{snapshot_id}{digest_suffix}.tar.gz"
        )
        display_name = f"sandbox-snapshot-{sandbox_id}-{snapshot_id}.tar.gz"
        metadata = {
            "sandbox_id": sandbox_id,
            "tenant_id": tenant_id,
            "snapshot_id": snapshot_id,
        }

        # Spool to a temp file so we can enforce the real stream size before
        # handing bytes to the file store and report exact snapshot metadata.
        tmp_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                suffix=".tar.gz", delete=False
            ) as tmp_file:
                tmp_path = tmp_file.name
                size_bytes = _copy_snapshot_stream_with_limit(stream, tmp_file)

            with open(tmp_path, "rb") as f:
                self._file_store.save_file(
                    content=f,
                    display_name=display_name,
                    file_origin=FileOrigin.SANDBOX_SNAPSHOT,
                    file_type=SNAPSHOT_FILE_TYPE,
                    file_id=storage_path,
                    file_metadata=metadata,
                )

            logger.info(
                "Created snapshot %s for sandbox %s, size: %s bytes",
                snapshot_id,
                sandbox_id,
                size_bytes,
            )
            return snapshot_id, storage_path, size_bytes
        except Exception as e:
            logger.error(
                "Failed to create streamed snapshot for sandbox %s: %s",
                sandbox_id,
                e,
            )
            raise RuntimeError(f"Failed to create snapshot: {e}") from e
        finally:
            if tmp_path:
                try:
                    Path(tmp_path).unlink(missing_ok=True)
                except Exception as cleanup_error:
                    logger.warning(
                        "Failed to cleanup temp file %s: %s",
                        tmp_path,
                        cleanup_error,
                    )

    def restore_snapshot_to_stream(
        self,
        storage_path: str,
        write_stream: IO[bytes],
    ) -> None:
        """Stream a stored snapshot's bytes into a caller-provided writer.

        Used by backends (e.g. Docker) that extract the archive inside the
        sandbox container by piping bytes into a remote ``tar -x`` process,
        avoiding an on-host extraction step.

        Args:
            storage_path: The file store path of the snapshot.
            write_stream: Binary writable stream the bytes are written into.
        """
        file_io = None
        try:
            file_io = self._file_store.read_file(storage_path, use_tempfile=True)
            _copy_snapshot_stream_with_limit(file_io, write_stream)
            logger.info("Streamed snapshot %s to caller writer", storage_path)
        except Exception as e:
            logger.error("Failed to stream snapshot %s to writer: %s", storage_path, e)
            raise RuntimeError(f"Failed to stream snapshot: {e}") from e
        finally:
            try:
                if file_io:
                    file_io.close()
            except Exception:
                pass

    def delete_snapshot(self, storage_path: str) -> None:
        """Delete snapshot from file store.

        Args:
            storage_path: The file store path of the snapshot to delete

        Raises:
            RuntimeError: If deletion fails (other than file not found)
        """
        try:
            self._file_store.delete_file(storage_path)
            logger.info("Deleted snapshot: %s", storage_path)
        except Exception as e:
            # Log but don't fail if snapshot doesn't exist
            logger.warning("Failed to delete snapshot %s: %s", storage_path, e)
            raise RuntimeError(f"Failed to delete snapshot: {e}") from e

    def get_snapshot_size(self, storage_path: str) -> int | None:
        """Get the size of a snapshot in bytes.

        Args:
            storage_path: The file store path of the snapshot

        Returns:
            Size in bytes, or None if not available
        """
        return self._file_store.get_file_size(storage_path)
