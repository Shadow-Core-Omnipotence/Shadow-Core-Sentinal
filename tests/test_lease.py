"""Idle suspension, and the gap it creates (2026-07-31).

The failure being designed out: watches accumulated for the life of the process,
so Sentinel ended up recording every project any session had ever visited. The
fix is a lease — but a lease that quietly re-armed would leave an invisible hole
in the trail covering exactly the period nobody was watching, which is worse
than the accumulation it replaces. These tests pin down BOTH halves: the watch
goes down when idle, and what happened while it was down is recorded.
"""
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from lease import LeaseSweeper, WatchLifecycle, diff_states, gap_report, scan_tree
from watch_registry import WatchRegistry


class FakeStore:
    def __init__(self, db_path, watch_path):
        self.rows = []

    def insert(self, event):
        self.rows.append(event)

    def close(self):
        pass


class FakeBuilder:
    def __init__(self, audit_dir, watch_path):
        self.audit_dir = audit_dir
        self.watch_path = watch_path
        self.events = []

    def append_event(self, event):
        self.events.append(event)


def never_ignored(path):
    return False


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / "src" / "main.py").write_text("original\n")
    (root / "README.md").write_text("readme\n")
    return root


@pytest.fixture
def registry(tmp_path):
    return WatchRegistry(tmp_path / "audit", FakeStore, FakeBuilder)


# ── scanning and diffing ────────────────────────────────────────────────────
def test_scan_keys_are_relative(project):
    state = scan_tree(project, never_ignored)
    assert set(state) == {"src/main.py", "README.md"}
    assert all(v and len(v) == 64 for v in state.values())


def test_scan_honours_ignores(project):
    (project / "node_modules").mkdir()
    (project / "node_modules" / "junk.js").write_text("x")

    def ignore_node(p):
        return "node_modules" in Path(p).parts

    state = scan_tree(project, ignore_node)
    assert not any("node_modules" in k for k in state)


def test_diff_detects_each_kind(project):
    before = scan_tree(project, never_ignored)
    (project / "src" / "main.py").write_text("changed\n")
    (project / "NEW.md").write_text("new\n")
    (project / "README.md").unlink()
    after = scan_tree(project, never_ignored)

    added, removed, modified = diff_states(before, after)
    assert added == ["NEW.md"]
    assert removed == ["README.md"]
    assert modified == ["src/main.py"]


def test_unreadable_file_is_not_reported_as_modified():
    """Unknown is not the same as changed — inventing a change would be exactly
    the false confidence the audit trail exists to prevent."""
    before = {"a.txt": None}
    after = {"a.txt": "abc"}
    assert diff_states(before, after) == ([], [], [])


def test_identical_trees_produce_no_diff(project):
    state = scan_tree(project, never_ignored)
    assert diff_states(state, dict(state)) == ([], [], [])


# ── lease bookkeeping ───────────────────────────────────────────────────────
def test_idle_entries_respects_ttl(registry, project):
    entry = registry.add(project)
    assert registry.idle_entries(ttl_seconds=100) == []

    entry.last_touch -= 200
    assert registry.idle_entries(ttl_seconds=100) == [entry]

    entry.touch()
    assert registry.idle_entries(ttl_seconds=100) == []


def test_suspended_entries_are_not_swept_again(registry, project):
    entry = registry.add(project)
    entry.last_touch -= 200
    entry.suspended = True
    assert registry.idle_entries(ttl_seconds=100) == []


def test_suspended_watch_is_not_removed(registry, project):
    """Suspension must keep the entry, its store and its history — resume has
    to be instant, and the trail has to stay continuous."""
    entry = registry.add(project)
    entry.store.insert("earlier-event")
    entry.suspended = True

    assert registry.get(project) is entry
    assert len(registry) == 1
    assert entry.store.rows == ["earlier-event"]


def test_touch_by_path_renews(registry, project):
    entry = registry.add(project)
    entry.last_touch -= 500
    assert registry.touch(project) is entry
    assert entry.idle_seconds() < 1


def test_touch_unwatched_path_is_none(registry, tmp_path):
    assert registry.touch(tmp_path / "nope") is None


# ── the sweeper ─────────────────────────────────────────────────────────────
def test_sweeper_suspends_only_idle_watches(registry, project, tmp_path):
    busy = tmp_path / "busy"
    busy.mkdir()
    idle_entry = registry.add(project)
    busy_entry = registry.add(busy)
    idle_entry.last_touch -= 200

    suspended = []
    sweeper = LeaseSweeper(registry, suspended.append, ttl_seconds=100,
                           interval_seconds=5)
    sweeper._sweep()

    assert suspended == [idle_entry]
    assert busy_entry not in suspended


def test_sweeper_disabled_when_ttl_zero(registry):
    sweeper = LeaseSweeper(registry, lambda e: None, ttl_seconds=0)
    assert sweeper.start() is None


def test_sweep_survives_a_failing_suspend(registry, project):
    """A suspend that raises must not kill the thread — watches would then stop
    expiring with nothing reporting that they had."""
    entry = registry.add(project)
    entry.last_touch -= 200

    def boom(e):
        raise RuntimeError("nope")

    sweeper = LeaseSweeper(registry, boom, ttl_seconds=100)
    sweeper._sweep()  # must not raise


# ── the gap report ──────────────────────────────────────────────────────────
def test_gap_report_names_the_window_and_changes(project):
    start = datetime(2026, 7, 31, 8, 0, tzinfo=timezone.utc)
    text = gap_report("proj", project, start, start + timedelta(minutes=90),
                      added=["NEW.md"], removed=["OLD.md"], modified=["src/main.py"])

    assert "90.0 minutes" in text
    assert "NEW.md" in text and "OLD.md" in text and "src/main.py" in text
    assert "Changes reconstructed:** 3" in text
    # The trail must not imply these were seen live.
    assert "not observed live" in text.lower()
    assert "DETECTION time" in text


def test_gap_report_says_so_when_nothing_changed(project):
    start = datetime(2026, 7, 31, 8, 0, tzinfo=timezone.utc)
    text = gap_report("proj", project, start, start + timedelta(minutes=5),
                      added=[], removed=[], modified=[])
    assert "No changes" in text


# ── suspend → blind edits → resume, against a real observer ─────────────────
@pytest.fixture
def live(tmp_path, project):
    """A running observer wired the way main() wires it."""
    from observer import AuditEventHandler, add_watch, remove_watch, start_bare_observer
    from report_builder import ReportBuilder
    from storage import EventStore

    registry = WatchRegistry(tmp_path / "audit", EventStore, ReportBuilder)
    handler = AuditEventHandler()
    recorded = []

    def record(event):
        entry = registry.route(event.src_path)
        if entry is None:
            return
        entry.touch()
        entry.store.insert(event)
        recorded.append(event)

    handler.subscribe(record)
    observer = start_bare_observer()

    lifecycle = WatchLifecycle(observer, handler, never_ignored,
                               add_watch, remove_watch)
    entry = registry.add(project)
    entry.handle = add_watch(observer, handler, entry.path)

    yield lifecycle, registry, entry, recorded

    observer.stop()
    observer.join(timeout=2)
    registry.close_all()


def test_changes_while_suspended_are_reconstructed(live, project):
    """The gap is the whole point: edits made while unwatched must land in the
    trail, not vanish. A silent hole would make Sentinel confidently wrong."""
    lifecycle, registry, entry, recorded = live

    lifecycle.suspend(entry)
    assert entry.suspended and entry.handle is None
    assert registry.get(project) is entry, "suspension must not remove the watch"

    # Sentinel is blind here — a git pull, a branch switch, another editor.
    (project / "src" / "main.py").write_text("edited while blind\n")
    (project / "ADDED.md").write_text("new file\n")
    (project / "README.md").unlink()
    time.sleep(0.3)
    assert recorded == [], "a suspended watch must record nothing live"

    assert lifecycle.resume(entry, reason="prompt") is True
    assert not entry.suspended and entry.handle is not None

    rows = entry.store.query_since("2000-01-01T00:00:00+00:00", limit=100)
    kinds = {(r["kind"], Path(r["src_path"]).name) for r in rows}
    assert ("MODIFIED", "main.py") in kinds
    assert ("CREATED", "ADDED.md") in kinds
    assert ("DELETED", "README.md") in kinds

    reports = list(entry.audit_dir.glob("gap-*.md"))
    assert len(reports) == 1
    text = reports[0].read_text(encoding="utf-8")
    assert "not observed live" in text.lower()
    assert "ADDED.md" in text


def test_watching_resumes_live_recording(live, project):
    """Resume must actually re-arm the observer, not just clear the flag."""
    lifecycle, registry, entry, recorded = live
    lifecycle.suspend(entry)
    lifecycle.resume(entry)
    recorded.clear()

    (project / "after_resume.txt").write_text("live again\n")
    deadline = time.time() + 5
    while time.time() < deadline and not recorded:
        time.sleep(0.1)

    assert any("after_resume" in str(e.src_path) for e in recorded)


def test_resume_on_unsuspended_watch_is_a_noop(live, project):
    lifecycle, registry, entry, recorded = live
    assert lifecycle.resume(entry) is False
    assert list(entry.audit_dir.glob("gap-*.md")) == []


def test_quiet_suspension_still_reports_the_window(live, project):
    """Nothing changed while away — the trail should say so explicitly rather
    than leave the period unaccounted for."""
    lifecycle, registry, entry, recorded = live
    lifecycle.suspend(entry)
    lifecycle.resume(entry)

    text = next(iter(entry.audit_dir.glob("gap-*.md"))).read_text(encoding="utf-8")
    assert "No changes" in text
