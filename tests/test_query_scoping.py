"""Task-sized queries (2026-07-31).

query_by_date returns a whole DAY. Measured on the Engineer project:
19,936 events on 2026-06-02. That is not an answer to "did my edit land?" --
it is more rows than anyone can read, and it costs enormously to hand back.

A task-sized window is tens of rows. These pin that scoping works and that the
hash, at 64 chars per row, is opt-in.
"""
from datetime import datetime, timedelta, timezone

import pytest

from models import AuditEvent, EventKind
from storage import EventStore


@pytest.fixture
def store(tmp_path):
    s = EventStore(tmp_path / "sentinel.db", watch_path=str(tmp_path))
    yield s
    s.close()


def _add(store, path, minutes_ago, kind=EventKind.MODIFIED):
    # AuditEvent is a frozen dataclass, so the timestamp must be passed in
    # rather than assigned afterwards.
    ts = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    store.insert(AuditEvent(kind=kind, src_path=path, timestamp=ts, sha256="a" * 64))


def _cutoff(minutes):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


def test_only_events_inside_the_window_come_back(store, tmp_path):
    _add(store, tmp_path / "old.py", minutes_ago=120)
    _add(store, tmp_path / "recent.py", minutes_ago=2)

    rows = store.query_since(_cutoff(15))

    assert [r["src_path"].split("\\")[-1].split("/")[-1] for r in rows] == ["recent.py"]


def test_a_wide_window_still_finds_the_old_event(store, tmp_path):
    _add(store, tmp_path / "old.py", minutes_ago=120)
    assert len(store.query_since(_cutoff(180))) == 1


def test_counting_is_available_without_fetching_rows(store, tmp_path):
    """The cheap question to ask first: is anything happening at all?"""
    for i in range(30):
        _add(store, tmp_path / f"f{i}.py", minutes_ago=1)

    assert store.count_since(_cutoff(10)) == 30
    assert store.count_since(_cutoff(0)) == 0


def test_newest_first(store, tmp_path):
    _add(store, tmp_path / "first.py", minutes_ago=9)
    _add(store, tmp_path / "second.py", minutes_ago=1)

    rows = store.query_since(_cutoff(30))
    assert "second.py" in rows[0]["src_path"]


def test_limit_caps_a_burst(store, tmp_path):
    for i in range(200):
        _add(store, tmp_path / f"f{i}.py", minutes_ago=1)

    assert len(store.query_since(_cutoff(10), limit=25)) == 25
    # The count still reports the truth, so a caller can tell it was truncated.
    assert store.count_since(_cutoff(10)) == 200


def test_hashes_are_opt_out(store, tmp_path):
    """64 chars per row, rarely needed to see WHAT changed."""
    _add(store, tmp_path / "f.py", minutes_ago=1)

    with_hash = store.query_since(_cutoff(10), include_hashes=True)[0]
    without = store.query_since(_cutoff(10), include_hashes=False)[0]

    assert with_hash["sha256"] == "a" * 64
    assert "sha256" not in without
    # Dropping it from the response must not drop it from the database.
    assert store.query_by_date(datetime.now(timezone.utc).strftime("%Y-%m-%d"))[0]["sha256"]


def test_an_empty_window_is_not_an_error(store):
    assert store.query_since(_cutoff(5)) == []
    assert store.count_since(_cutoff(5)) == 0
