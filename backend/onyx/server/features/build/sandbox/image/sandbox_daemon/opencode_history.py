"""Opencode history archive create/restore helpers for the sandbox sidecar."""

import os
import shutil
import sqlite3
import tarfile
import tempfile
from pathlib import Path

from sandbox_daemon.snapshot import MAX_SNAPSHOT_ARCHIVE_BYTES
from sandbox_daemon.snapshot import MAX_SNAPSHOT_UNCOMPRESSED_BYTES
from sandbox_daemon.snapshot import SESSIONS_ROOT
from sandbox_daemon.snapshot import SnapshotError

OPENCODE_DATA_DIR = SESSIONS_ROOT / ".opencode-data"
OPENCODE_DB_PATH = OPENCODE_DATA_DIR / "opencode" / "opencode.db"

_OPENCODE_ARCHIVE_ROOT = ".opencode-data"
_OPENCODE_ARCHIVE_DB_MEMBER = ".opencode-data/opencode/opencode.db"
_OPENCODE_ARCHIVE_ALLOWED_DIRS = frozenset(
    {".opencode-data", ".opencode-data/opencode"}
)
_SQLITE_MAGIC = b"SQLite format 3\x00"


def _safe_opencode_data_dir(*, create: bool) -> Path:
    sessions_root = SESSIONS_ROOT.resolve()
    if OPENCODE_DATA_DIR.is_symlink():
        raise SnapshotError("opencode data dir is a symlink; refusing access")

    if create:
        OPENCODE_DATA_DIR.mkdir(parents=True, exist_ok=True)

    if OPENCODE_DATA_DIR.exists() and not OPENCODE_DATA_DIR.is_dir():
        raise SnapshotError("opencode data path is not a directory")

    try:
        OPENCODE_DATA_DIR.resolve(strict=False).relative_to(sessions_root)
    except ValueError as e:
        raise SnapshotError("opencode data path escapes sessions root") from e
    return OPENCODE_DATA_DIR


def _safe_opencode_db_path() -> Path | None:
    data_dir = _safe_opencode_data_dir(create=False)
    db_parent = OPENCODE_DB_PATH.parent
    if db_parent.is_symlink() or OPENCODE_DB_PATH.is_symlink():
        raise SnapshotError("opencode db path contains a symlink; refusing access")
    if db_parent.exists() and not db_parent.is_dir():
        raise SnapshotError("opencode db parent is not a directory")
    if not OPENCODE_DB_PATH.exists():
        return None
    if not OPENCODE_DB_PATH.is_file():
        raise SnapshotError("opencode db path is not a regular file")
    try:
        OPENCODE_DB_PATH.resolve(strict=False).relative_to(data_dir.resolve())
    except ValueError as e:
        raise SnapshotError("opencode db path escapes opencode data dir") from e
    return OPENCODE_DB_PATH


def _backup_opencode_sqlite_db(source: Path, destination: Path) -> None:
    if source.stat().st_size > MAX_SNAPSHOT_UNCOMPRESSED_BYTES:
        raise SnapshotError(
            f"opencode history db exceeds {MAX_SNAPSHOT_UNCOMPRESSED_BYTES} byte limit"
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    src: sqlite3.Connection | None = None
    dst: sqlite3.Connection | None = None
    try:
        src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        dst = sqlite3.connect(destination)
        src.execute("PRAGMA busy_timeout = 5000")
        src.backup(dst)
    except sqlite3.Error as e:
        raise SnapshotError(f"opencode sqlite backup failed: {e}") from e
    finally:
        if dst is not None:
            dst.close()
        if src is not None:
            src.close()


def _validate_opencode_sqlite_db(db_path: Path) -> None:
    try:
        with db_path.open("rb") as db_file:
            if db_file.read(len(_SQLITE_MAGIC)) != _SQLITE_MAGIC:
                raise SnapshotError("opencode history archive db is not sqlite")

        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            result = conn.execute("PRAGMA quick_check").fetchone()
        finally:
            conn.close()
    except sqlite3.Error as e:
        raise SnapshotError(f"opencode history db integrity check failed: {e}") from e

    if result is None or result[0] != "ok":
        detail = result[0] if result is not None else "no result"
        raise SnapshotError(f"opencode history db integrity check failed: {detail}")


def create_opencode_history_archive_file() -> Path | None:
    """Create a local opencode history tarball, or None when no DB exists."""
    db_path = _safe_opencode_db_path()
    if db_path is None:
        return None

    tmp_path: Path | None = None
    with tempfile.TemporaryDirectory(
        dir=SESSIONS_ROOT, prefix=".opencode-history-"
    ) as tmp_dir:
        staging_root = Path(tmp_dir)
        staged_db = staging_root / _OPENCODE_ARCHIVE_DB_MEMBER
        _backup_opencode_sqlite_db(db_path, staged_db)
        if staged_db.stat().st_size > MAX_SNAPSHOT_UNCOMPRESSED_BYTES:
            raise SnapshotError(
                "opencode history db exceeds "
                f"{MAX_SNAPSHOT_UNCOMPRESSED_BYTES} byte limit"
            )
        _validate_opencode_sqlite_db(staged_db)

        with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp_file:
            tmp_path = Path(tmp_file.name)

        try:
            with tarfile.open(tmp_path, "w:gz", compresslevel=6) as tar:
                tar.add(
                    staging_root / _OPENCODE_ARCHIVE_ROOT,
                    arcname=_OPENCODE_ARCHIVE_ROOT,
                    recursive=True,
                )
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

    size_bytes = tmp_path.stat().st_size
    if size_bytes > MAX_SNAPSHOT_ARCHIVE_BYTES:
        tmp_path.unlink(missing_ok=True)
        raise SnapshotError(
            f"opencode history archive exceeds {MAX_SNAPSHOT_ARCHIVE_BYTES} byte limit"
        )
    return tmp_path


def _validate_opencode_history_member(member: tarfile.TarInfo) -> str:
    try:
        member.name.encode("utf-8")
    except UnicodeEncodeError as e:
        raise SnapshotError(f"non-UTF-8 opencode history path: {member.name!r}") from e

    if member.issym() or member.islnk():
        raise SnapshotError(f"opencode history links are not allowed: {member.name}")
    if not (member.isfile() or member.isdir()):
        raise SnapshotError(
            f"opencode history special file is not allowed: {member.name}"
        )
    if os.path.isabs(member.name):
        raise SnapshotError(
            f"absolute opencode history path is not allowed: {member.name}"
        )

    normalized = os.path.normpath(member.name)
    if normalized in ("", ".") or normalized == ".." or normalized.startswith("../"):
        raise SnapshotError(f"opencode history path escapes root: {member.name}")
    if not (
        normalized == _OPENCODE_ARCHIVE_ROOT
        or normalized.startswith(f"{_OPENCODE_ARCHIVE_ROOT}/")
    ):
        raise SnapshotError(f"opencode history path has unexpected root: {member.name}")
    if member.isdir():
        if normalized not in _OPENCODE_ARCHIVE_ALLOWED_DIRS:
            raise SnapshotError(
                f"opencode history archive has unexpected directory: {member.name}"
            )
    elif normalized != _OPENCODE_ARCHIVE_DB_MEMBER:
        raise SnapshotError(
            f"opencode history archive has unexpected file: {member.name}"
        )
    return normalized


def restore_opencode_history_archive(archive_path: Path) -> None:
    """Restore opencode's SQLite history store from a sidecar-local archive."""
    _safe_opencode_data_dir(create=True)
    total_uncompressed_bytes = 0

    try:
        with tempfile.TemporaryDirectory(
            dir=SESSIONS_ROOT,
            prefix=".opencode-history-restore-",
        ) as tmp_dir:
            staging_path = Path(tmp_dir)
            staged_parent = staging_path / "opencode"
            staged_db = staged_parent / "opencode.db"
            staged_parent.mkdir(parents=True, exist_ok=True)

            with tarfile.open(archive_path, "r:gz") as tar:
                db_member: tarfile.TarInfo | None = None
                for member in tar.getmembers():
                    _validate_opencode_history_member(member)
                    if member.isfile():
                        if db_member is not None:
                            raise SnapshotError(
                                "opencode history archive has duplicate opencode.db"
                            )
                        db_member = member
                        total_uncompressed_bytes += member.size
                        if total_uncompressed_bytes > MAX_SNAPSHOT_UNCOMPRESSED_BYTES:
                            raise SnapshotError(
                                "opencode history uncompressed size exceeds "
                                f"{MAX_SNAPSHOT_UNCOMPRESSED_BYTES} byte limit"
                            )

                if db_member is None:
                    raise SnapshotError(
                        "opencode history archive is missing opencode.db"
                    )
                src = tar.extractfile(db_member)
                if src is None:
                    raise SnapshotError(
                        f"cannot read opencode history entry: {db_member.name}"
                    )
                with src, staged_db.open("wb") as out_file:
                    shutil.copyfileobj(src, out_file)
                os.chmod(staged_db, 0o600)

            _validate_opencode_sqlite_db(staged_db)

            target_parent = OPENCODE_DB_PATH.parent
            if target_parent.is_symlink() or OPENCODE_DB_PATH.is_symlink():
                raise SnapshotError(
                    "existing opencode db path contains a symlink; refusing restore"
                )
            if target_parent.exists() and not target_parent.is_dir():
                raise SnapshotError("existing opencode db parent is not a directory")
            if target_parent.exists():
                shutil.rmtree(target_parent)
            target_parent.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staged_parent, target_parent)
    except (tarfile.TarError, OSError) as e:
        raise SnapshotError(f"invalid opencode history archive: {e}") from e
