"""Unit tests for snapshot retention selection (keep-last-N + expiry)."""

import datetime
from uuid import UUID
from uuid import uuid4

from onyx.db.models import Snapshot
from onyx.server.features.build.db.sandbox import _select_prunable_snapshots

_NOW = datetime.datetime(2026, 6, 10, tzinfo=datetime.timezone.utc)
_CUTOFF = _NOW - datetime.timedelta(days=30)


def _snap(session_id: UUID, age_days: int) -> Snapshot:
    snap = Snapshot(
        id=uuid4(),
        session_id=session_id,
        storage_path=f"snap/{uuid4()}.tar.gz",
        size_bytes=1,
    )
    snap.created_at = _NOW - datetime.timedelta(days=age_days)
    return snap


def _ordered(*snaps: Snapshot) -> list[Snapshot]:
    """Mimic the SQL ordering: by session_id, then created_at desc."""
    return sorted(snaps, key=lambda s: (str(s.session_id), -s.created_at.timestamp()))


def test_keeps_latest_even_when_ancient() -> None:
    session = uuid4()
    only = _snap(session, age_days=400)
    assert _select_prunable_snapshots([only], _CUTOFF, keep_last_n=5) == []


def test_hard_caps_at_keep_last_n() -> None:
    session = uuid4()
    snaps = [_snap(session, age_days=d) for d in (0, 1, 2, 3)]
    prunable = _select_prunable_snapshots(_ordered(*snaps), _CUTOFF, keep_last_n=2)
    # newest two (age 0, 1) kept; the rest pruned despite being recent.
    pruned_ages = sorted((_NOW - s.created_at).days for s in prunable)
    assert pruned_ages == [2, 3]


def test_expires_old_within_cap_but_keeps_anchor() -> None:
    session = uuid4()
    snaps = [_snap(session, age_days=d) for d in (40, 35, 31)]
    # All older than retention, but position 0 (age 31, newest) is the anchor.
    prunable = _select_prunable_snapshots(_ordered(*snaps), _CUTOFF, keep_last_n=10)
    pruned_ages = sorted((_NOW - s.created_at).days for s in prunable)
    assert pruned_ages == [35, 40]


def test_recent_within_cap_are_kept() -> None:
    session = uuid4()
    snaps = [_snap(session, age_days=d) for d in (0, 5, 10)]
    assert _select_prunable_snapshots(_ordered(*snaps), _CUTOFF, keep_last_n=10) == []


def test_independent_per_session() -> None:
    s1, s2 = uuid4(), uuid4()
    snaps = [
        _snap(s1, 0),
        _snap(s1, 40),
        _snap(s2, 0),
        _snap(s2, 50),
    ]
    prunable = _select_prunable_snapshots(_ordered(*snaps), _CUTOFF, keep_last_n=10)
    # Each session keeps its own newest; only the old second entries prune.
    pruned_ages = sorted((_NOW - s.created_at).days for s in prunable)
    assert pruned_ages == [40, 50]
