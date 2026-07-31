"""End-to-end watching: real observer, real files, real databases.

WHY THESE EXIST SEPARATELY FROM test_watch_registry.py
------------------------------------------------------
Those tests use fake stores and never touch a filesystem, so they prove the
registry's LOGIC. They cannot prove the WIRING — that watchdog actually
delivers events, that the routing subscriber is connected, that rows land in
the right SQLite file.

That gap is exactly how the FastMCP/raw-Server mismatch shipped in May: every
component was fine, the wiring between two of them was not, and nothing failed
loudly. TECH_DEBT_AUDIT.md #1.

These are slower (they wait on real filesystem events) but they exercise the
path a change actually takes: file written → watchdog → handler → hasher →
router → the owning project's database.
"""
import time
from pathlib import Path

import pytest

from observer import AuditEventHandler, add_watch, remove_watch, start_bare_observer
from report_builder import ReportBuilder
from storage import EventStore
from watch_registry import WatchRegistry

# Filesystem events are asynchronous and hashing is offloaded to a thread pool,
# so every assertion polls rather than sleeping a fixed amount. Generous, since
# a slow disk should not be a test failure.
SETTLE_TIMEOUT = 15.0
POLL = 0.25


def _wait_for(predicate, timeout=SETTLE_TIMEOUT):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(POLL)
    return False


@pytest.fixture
def live(tmp_path):
    """A running observer wired to a registry, exactly as main() wires it."""
    registry = WatchRegistry(tmp_path / "audit", EventStore, ReportBuilder)
    handler = AuditEventHandler()

    def record(event):
        entry = registry.route(event.src_path)
        if entry is None:
            return
        entry.store.insert(event)

    handler.subscribe(record)
    observer = start_bare_observer()

    yield registry, handler, observer

    observer.stop()
    observer.join(timeout=5)
    handler.shutdown()
    registry.close_all()


def _watch(live, path):
    registry, handler, observer = live
    entry = registry.add(path)
    entry.handle = add_watch(observer, handler, entry.path)
    return entry


def test_it_boots_idle_and_records_nothing(live, tmp_path):
    """The new startup contract: running, but watching nothing."""
    registry, _, _ = live
    stray = tmp_path / "unwatched"
    stray.mkdir()

    (stray / "file.txt").write_text("nobody asked for this")
    time.sleep(1.5)

    assert len(registry) == 0
    assert registry.route(stray / "file.txt") is None


def test_watching_a_directory_starts_recording_it(live, tmp_path):
    proj = tmp_path / "Alpha"
    proj.mkdir()
    entry = _watch(live, proj)

    (proj / "hello.py").write_text("print('hi')\n")

    assert _wait_for(lambda: entry.store.total_count() > 0), \
        "no event recorded after writing a file into a watched directory"


def test_a_recorded_event_carries_a_hash(live, tmp_path):
    """The SHA is the whole point — it is what makes the trail verifiable."""
    proj = tmp_path / "Alpha"
    proj.mkdir()
    entry = _watch(live, proj)

    (proj / "hello.py").write_text("print('hi')\n")
    assert _wait_for(lambda: entry.store.total_count() > 0)

    date_key = time.strftime("%Y-%m-%d", time.gmtime())
    rows = entry.store.query_by_date(date_key)
    assert rows, "event recorded but not queryable by today's date"
    assert any(r["sha256"] for r in rows), "no event carried a SHA-256"


def test_two_projects_record_into_their_own_databases(live, tmp_path):
    """THE multi-watch guarantee. Two sessions, two projects, no crosstalk."""
    a = tmp_path / "Alpha"
    b = tmp_path / "Beta"
    a.mkdir()
    b.mkdir()
    ea = _watch(live, a)
    eb = _watch(live, b)

    (a / "only_in_a.py").write_text("a\n")
    assert _wait_for(lambda: ea.store.total_count() > 0)

    (b / "only_in_b.py").write_text("b\n")
    assert _wait_for(lambda: eb.store.total_count() > 0)

    assert ea.store._path != eb.store._path, "projects must not share a database"

    date_key = time.strftime("%Y-%m-%d", time.gmtime())
    a_paths = " ".join(r["src_path"] for r in ea.store.query_by_date(date_key))
    b_paths = " ".join(r["src_path"] for r in eb.store.query_by_date(date_key))

    assert "only_in_a" in a_paths and "only_in_b" not in a_paths
    assert "only_in_b" in b_paths and "only_in_a" not in b_paths


def test_a_nested_project_records_into_the_child(live, tmp_path):
    """Watching a parent AND a child: the child's files belong to the child."""
    parent = tmp_path / "work"
    child = parent / "api"
    child.mkdir(parents=True)
    ep = _watch(live, parent)
    ec = _watch(live, child)

    (child / "server.py").write_text("nested\n")
    assert _wait_for(lambda: ec.store.total_count() > 0)

    date_key = time.strftime("%Y-%m-%d", time.gmtime())
    parent_rows = " ".join(r["src_path"] for r in ep.store.query_by_date(date_key))
    assert "server.py" not in parent_rows, \
        "a nested project's change was filed under its parent"


def test_unwatching_stops_recording_and_leaves_the_other_alone(live, tmp_path):
    a = tmp_path / "Alpha"
    b = tmp_path / "Beta"
    a.mkdir()
    b.mkdir()
    ea = _watch(live, a)
    eb = _watch(live, b)
    registry, _, observer = live

    (a / "before.py").write_text("1\n")
    assert _wait_for(lambda: ea.store.total_count() > 0)

    remove_watch(observer, ea.handle)
    registry.remove(a)
    time.sleep(1.0)

    (a / "after.py").write_text("2\n")     # must NOT be recorded anywhere
    (b / "still_here.py").write_text("3\n")

    assert _wait_for(lambda: eb.store.total_count() > 0), \
        "unwatching one project stopped the other -- the original bug"
    assert registry.route(a / "after.py") is None


def test_returning_to_idle_and_resuming(live, tmp_path):
    """Removing the last watch is legal; watching again afterwards must work."""
    registry, handler, observer = live
    a = tmp_path / "Alpha"
    a.mkdir()
    ea = _watch(live, a)
    remove_watch(observer, ea.handle)
    registry.remove(a)

    assert len(registry) == 0

    b = tmp_path / "Beta"
    b.mkdir()
    eb = _watch(live, b)
    (b / "resumed.py").write_text("ok\n")

    assert _wait_for(lambda: eb.store.total_count() > 0)


@pytest.mark.slow
def test_a_burst_of_changes_is_not_dropped(live, tmp_path):
    """A large refactor writes many files at once. Hashing is offloaded to a
    thread pool precisely so the OS event queue is not starved -- if that
    breaks, events vanish silently and the trail is quietly incomplete."""
    proj = tmp_path / "Burst"
    proj.mkdir()
    entry = _watch(live, proj)

    count = 120
    for i in range(count):
        (proj / f"file_{i:03d}.py").write_text(f"# file {i}\n")

    # Not all N will be distinct rows (debounce collapses rapid duplicates on
    # the same path), but a large majority must survive.
    got = _wait_for(lambda: entry.store.total_count() >= count * 0.8, timeout=45.0)
    assert got, (
        f"only {entry.store.total_count()} of {count} files recorded — "
        "events are being dropped under load"
    )
