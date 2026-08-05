"""EventStore is the durable half of the audit trail.

The failure this guards against is a DROPPED ROW that nobody hears about.
`insert` used to catch every exception, log it, and return None; callers took
that as success and went on to append the same event to the markdown trail.
The two records of one period then disagreed with no signal anywhere — which,
for a service whose whole claim is "this is what actually happened on disk",
is worse than crashing.
"""
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from models import AuditEvent, EventKind  # noqa: E402
from storage import EventStore  # noqa: E402


@pytest.fixture
def store(tmp_path):
    s = EventStore(tmp_path / "sentinel.db", watch_path=str(tmp_path))
    yield s
    s.close()


def _event(name="main.py", kind=EventKind.MODIFIED, ago_seconds=0, sha="abc"):
    return AuditEvent(
        kind=kind,
        src_path=Path("/proj") / name,
        timestamp=datetime.now(timezone.utc) - timedelta(seconds=ago_seconds),
        sha256=sha,
    )


# ── a write either lands or is counted ───────────────────────────────────────

def test_a_successful_insert_reports_success(store):
    assert store.insert(_event()) is True
    assert store.total_count() == 1
    assert store.failed_writes == 0


def test_a_failed_insert_reports_failure_rather_than_lying(store):
    store.close()  # the connection is now unusable

    assert store.insert(_event()) is False


def test_failed_writes_counts_every_drop(store):
    store.close()

    for _ in range(3):
        store.insert(_event())

    assert store.failed_writes == 3


def test_one_bad_row_does_not_kill_the_writer(store, tmp_path):
    """Monitoring must survive a bad row; it must not survive it silently."""
    broken = AuditEvent(kind=EventKind.MODIFIED, src_path=Path("/p/a.py"))
    object.__setattr__(broken, "timestamp", object())  # unserialisable

    assert store.insert(broken) is False
    assert store.failed_writes == 1

    assert store.insert(_event()) is True, "writer still usable after a failure"
    assert store.total_count() == 1


def test_the_count_is_not_reset_by_a_later_success(store):
    broken = AuditEvent(kind=EventKind.MODIFIED, src_path=Path("/p/a.py"))
    object.__setattr__(broken, "timestamp", object())
    store.insert(broken)
    store.insert(_event())

    assert store.failed_writes == 1, "a gap in the trail does not go away"


# ── round trip ───────────────────────────────────────────────────────────────

def test_an_event_round_trips_with_every_field(store):
    evt = AuditEvent(
        kind=EventKind.MOVED,
        src_path=Path("/proj/old.py"),
        dest_path=Path("/proj/new.py"),
        sha256="f" * 64,
    )
    store.insert(evt)

    (row,) = store.query_by_date(evt.date_key())
    assert row["kind"] == "MOVED"
    assert row["src_path"].endswith("old.py")
    assert row["dest_path"].endswith("new.py")
    assert row["sha256"] == "f" * 64


def test_the_watch_path_tag_is_recorded(store, tmp_path):
    store.insert(_event())
    (row,) = store.query_by_date(_event().date_key())
    assert row["watch_path"] == str(tmp_path)


def test_a_null_dest_and_sha_survive(store):
    evt = AuditEvent(kind=EventKind.DELETED, src_path=Path("/proj/gone.py"))
    store.insert(evt)

    (row,) = store.query_by_date(evt.date_key())
    assert row["dest_path"] is None
    assert row["sha256"] is None


def test_querying_a_date_with_nothing_on_it_is_empty(store):
    assert store.query_by_date("1999-01-01") == []


# ── the schema migration ─────────────────────────────────────────────────────

def _make_pre_migration_db(path: Path) -> None:
    """The older layout: no watch_path column."""
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE events (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            ts        TEXT NOT NULL,
            date_key  TEXT NOT NULL,
            kind      TEXT NOT NULL,
            src_path  TEXT NOT NULL,
            dest_path TEXT,
            sha256    TEXT
        );
    """)
    conn.execute(
        "INSERT INTO events (ts, date_key, kind, src_path) VALUES (?,?,?,?)",
        ("2020-01-01T00:00:00+00:00", "2020-01-01", "MODIFIED", "/old/a.py"),
    )
    conn.commit()
    conn.close()


def test_an_old_database_gains_the_watch_path_column(tmp_path):
    db = tmp_path / "sentinel.db"
    _make_pre_migration_db(db)

    store = EventStore(db, watch_path=str(tmp_path))
    try:
        cols = [r[1] for r in store._conn.execute("PRAGMA table_info(events)")]
        assert "watch_path" in cols
    finally:
        store.close()


def test_migrating_preserves_the_existing_history(tmp_path):
    """An upgrade must never cost recorded events."""
    db = tmp_path / "sentinel.db"
    _make_pre_migration_db(db)

    store = EventStore(db, watch_path=str(tmp_path))
    try:
        assert store.total_count() == 1
        (row,) = store.query_by_date("2020-01-01")
        assert row["src_path"] == "/old/a.py"
        assert row["watch_path"] is None, "pre-migration rows have no tag"
    finally:
        store.close()


def test_new_rows_after_a_migration_carry_the_tag(tmp_path):
    db = tmp_path / "sentinel.db"
    _make_pre_migration_db(db)

    store = EventStore(db, watch_path=str(tmp_path))
    try:
        store.insert(_event())
        assert store.total_count() == 2
        (row,) = store.query_by_date(_event().date_key())
        assert row["watch_path"] == str(tmp_path)
    finally:
        store.close()


def test_opening_an_already_migrated_database_is_a_noop(tmp_path):
    db = tmp_path / "sentinel.db"
    first = EventStore(db, watch_path=str(tmp_path))
    first.insert(_event())
    first.close()

    second = EventStore(db, watch_path=str(tmp_path))
    try:
        assert second.total_count() == 1
    finally:
        second.close()


def test_history_survives_a_reopen(tmp_path):
    db = tmp_path / "sentinel.db"
    s = EventStore(db, watch_path=str(tmp_path))
    s.insert(_event())
    s.close()

    s2 = EventStore(db, watch_path=str(tmp_path))
    try:
        assert s2.total_count() == 1
    finally:
        s2.close()


# ── batching ─────────────────────────────────────────────────────────────────

def test_a_batch_writes_every_row(store):
    events = [_event(f"f{i}.py") for i in range(50)]
    assert store.insert_many(events) == 50
    assert store.total_count() == 50


def test_an_empty_batch_is_not_an_error(store):
    assert store.insert_many([]) == 0


def test_a_batch_and_single_inserts_are_interchangeable(store):
    store.insert(_event("a.py"))
    store.insert_many([_event("b.py"), _event("c.py")])

    paths = {r["src_path"] for r in store.query_by_date(_event().date_key())}
    assert {p.rsplit("\\", 1)[-1].rsplit("/", 1)[-1] for p in paths} == {
        "a.py", "b.py", "c.py"}


def test_one_bad_event_costs_only_its_own_row(store):
    """A batch must degrade to per-row, not lose the whole window."""
    broken = AuditEvent(kind=EventKind.MODIFIED, src_path=Path("/p/bad.py"))
    object.__setattr__(broken, "timestamp", object())
    events = [_event("good1.py"), broken, _event("good2.py")]

    assert store.insert_many(events) == 2
    assert store.total_count() == 2
    assert store.failed_writes == 1


def test_wal_is_enabled(store):
    mode = store._conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"
