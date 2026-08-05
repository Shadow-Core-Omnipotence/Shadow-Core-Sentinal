"""Which project a dashboard request is answered from.

This logic sat in ten closures inside `main()` and could not be imported
without starting an observer, a sweeper and two HTTP servers. The rule it
enforces is subtle and load-bearing: naming a project in a REQUEST changes what
that request reads and must never move the server's default, or one browser tab
would silently repoint another session's `recent_changes`.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dashboard_wiring import DashboardWiring  # noqa: E402
from main import SentinelState  # noqa: E402
from models import AuditEvent, EventKind  # noqa: E402
from watch_registry import WatchRegistry  # noqa: E402


class FakeStore:
    def __init__(self, db_path, watch_path=None):
        self.rows = []

    def total_count(self):
        return len(self.rows)

    def close(self):
        pass


class FakeBuilder:
    def __init__(self, audit_dir, watch_path=None):
        self.audit_dir = audit_dir
        self.watch_path = watch_path
        self.snapshots = []

    def list_artifacts(self):
        return [Path("audit-2026-08-03.md"), Path("snapshot-a.md")]

    def build_disk_snapshot(self, label):
        self.snapshots.append(("disk", label))
        return Path(f"snapshot-{label}.md")

    def build_snapshot(self, events, label):
        self.snapshots.append(("events", label, len(events)))
        return Path(f"snapshot-{label}.md")


class FakeHandler:
    def __init__(self):
        self.events = []
        self.alerts = []

    def recent_events(self, n):
        return self.events[-n:]

    def recent_alerts(self):
        return self.alerts


class FakeSettings:
    max_memory_events = 500
    mcp_server_version = "test"

    def __init__(self):
        self.ignore_patterns = ["node_modules"]
        self.watch_dir = None
        self._prev = None

    def add_ignore_pattern(self, p):
        if p not in self.ignore_patterns:
            self.ignore_patterns.append(p)

    def update_watch_dir(self, p):
        self._prev, self.watch_dir = self.watch_dir, p

    def rollback_watch_dir(self):
        if self._prev is None:
            return None
        self.watch_dir, self._prev = self._prev, self.watch_dir
        return self.watch_dir


@pytest.fixture
def wiring(tmp_path):
    registry = WatchRegistry(tmp_path / "audit", FakeStore, FakeBuilder)
    state = SentinelState(handler=FakeHandler(), registry=registry, primary=None)
    scheduled = []

    def fake_add_watch(observer, handler, path):
        scheduled.append(path)
        return f"handle:{path}"

    w = DashboardWiring(state, FakeSettings(), fake_add_watch)
    w.state = state
    w.registry = registry
    w.scheduled = scheduled
    return w


def _project(tmp_path, name):
    p = tmp_path / name
    p.mkdir(exist_ok=True)
    return p


def _event(path):
    return AuditEvent(kind=EventKind.MODIFIED, src_path=path, sha256="abc")


# ── resolve: the request decides, not the server ─────────────────────────────

def test_naming_a_project_reads_that_project(wiring, tmp_path):
    wiring.registry.add(_project(tmp_path, "alpha"))
    beta = wiring.registry.add(_project(tmp_path, "beta"))
    wiring.state.primary = tmp_path / "alpha"

    assert wiring.resolve(str(beta.path)) is beta


def test_naming_a_project_does_not_move_the_primary(wiring, tmp_path):
    """The whole reason scoping travels in the request."""
    alpha = wiring.registry.add(_project(tmp_path, "alpha"))
    beta = wiring.registry.add(_project(tmp_path, "beta"))
    wiring.state.primary = alpha.path

    wiring.resolve(str(beta.path))

    assert wiring.state.primary == alpha.path


def test_naming_nothing_falls_back_to_the_primary(wiring, tmp_path):
    alpha = wiring.registry.add(_project(tmp_path, "alpha"))
    wiring.registry.add(_project(tmp_path, "beta"))
    wiring.state.primary = alpha.path

    assert wiring.resolve(None) is alpha


def test_naming_an_unwatched_project_falls_back(wiring, tmp_path):
    alpha = wiring.registry.add(_project(tmp_path, "alpha"))
    wiring.state.primary = alpha.path

    assert wiring.resolve(str(tmp_path / "nowhere")) is alpha


def test_resolving_while_idle_is_none(wiring):
    assert wiring.resolve(None) is None


# ── the feed is narrowed to one project ──────────────────────────────────────

def test_the_feed_shows_only_this_projects_events(wiring, tmp_path):
    alpha = wiring.registry.add(_project(tmp_path, "alpha"))
    beta = wiring.registry.add(_project(tmp_path, "beta"))
    wiring.state.handler.events = [
        _event(alpha.path / "a.py"),
        _event(beta.path / "b.py"),
        _event(alpha.path / "c.py"),
    ]

    names = [Path(e.src_path).name for e in wiring.events_for(alpha)]
    assert names == ["a.py", "c.py"]


def test_a_nested_project_takes_its_own_events(wiring, tmp_path):
    """Attribution uses the same longest-prefix match the writer uses."""
    parent = wiring.registry.add(_project(tmp_path, "work"))
    child_dir = tmp_path / "work" / "api"
    child_dir.mkdir(parents=True, exist_ok=True)
    child = wiring.registry.add(child_dir)

    wiring.state.handler.events = [
        _event(parent.path / "root.py"),
        _event(child.path / "nested.py"),
    ]

    assert [Path(e.src_path).name for e in wiring.events_for(parent)] == ["root.py"]
    assert [Path(e.src_path).name for e in wiring.events_for(child)] == ["nested.py"]


def test_the_feed_of_no_project_is_empty(wiring):
    assert wiring.events_for(None) == []


# ── stats and tabs ───────────────────────────────────────────────────────────

def test_stats_are_scoped_to_the_named_project(wiring, tmp_path):
    wiring.registry.add(_project(tmp_path, "alpha"))
    beta = wiring.registry.add(_project(tmp_path, "beta"))

    stats = wiring.get_stats(str(beta.path))
    assert stats["project_name"] == "beta"
    assert stats["watch_dir"] == str(beta.path)


def test_stats_while_idle_do_not_raise(wiring):
    stats = wiring.get_stats(None)
    assert stats["watch_dir"] is None
    assert stats["project_name"] is None


def test_alerts_are_labelled_process_wide(wiring, tmp_path):
    """Every other field is scoped; this one cannot be."""
    wiring.registry.add(_project(tmp_path, "alpha"))
    assert wiring.get_stats(None)["alerts_scope"] == "process"


def test_the_tab_strip_lists_every_watched_project(wiring, tmp_path):
    wiring.registry.add(_project(tmp_path, "alpha"))
    wiring.registry.add(_project(tmp_path, "beta"))

    assert {p["name"] for p in wiring.get_projects()} == {"alpha", "beta"}


def test_a_suspended_project_still_appears_in_the_tabs(wiring, tmp_path):
    """Hiding it would read as unwatched; its history is intact."""
    alpha = wiring.registry.add(_project(tmp_path, "alpha"))
    alpha.suspended = True

    (tab,) = [p for p in wiring.get_projects() if p["name"] == "alpha"]
    assert tab["suspended"] is True


def test_exactly_one_tab_is_marked_primary(wiring, tmp_path):
    alpha = wiring.registry.add(_project(tmp_path, "alpha"))
    wiring.registry.add(_project(tmp_path, "beta"))
    wiring.state.primary = alpha.path

    assert [p["is_primary"] for p in wiring.get_projects()].count(True) == 1


def test_snapshots_exclude_the_daily_logs(wiring, tmp_path):
    wiring.registry.add(_project(tmp_path, "alpha"))
    assert wiring.get_snapshots(None) == ["snapshot-a.md"]


# ── pivot and rollback ───────────────────────────────────────────────────────

def test_pivot_adds_a_watch_without_removing_the_other(wiring, tmp_path):
    """The failure this replaced: pivot used to unschedule everything."""
    alpha = wiring.registry.add(_project(tmp_path, "alpha"))
    beta_dir = _project(tmp_path, "beta")

    result = wiring.do_pivot(str(beta_dir))

    assert result["status"] == "ok"
    assert wiring.registry.get(alpha.path) is not None, "other watch survived"
    assert len(wiring.registry) == 2


def test_pivot_makes_the_new_project_the_default(wiring, tmp_path):
    beta_dir = _project(tmp_path, "beta")
    wiring.do_pivot(str(beta_dir))
    assert wiring.state.primary == beta_dir.resolve()


def test_pivot_schedules_the_new_directory(wiring, tmp_path):
    beta_dir = _project(tmp_path, "beta")
    wiring.do_pivot(str(beta_dir))
    assert beta_dir.resolve() in wiring.scheduled


def test_pivot_to_an_already_watched_project_does_not_double_schedule(
    wiring, tmp_path
):
    alpha = wiring.registry.add(_project(tmp_path, "alpha"))
    wiring.do_pivot(str(alpha.path))

    assert wiring.scheduled == []
    assert len(wiring.registry) == 1


def test_pivot_to_a_missing_directory_is_an_error(wiring, tmp_path):
    result = wiring.do_pivot(str(tmp_path / "nope"))
    assert result["status"] == "error"


def test_pivot_to_a_file_is_an_error(wiring, tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("x", encoding="utf-8")
    assert wiring.do_pivot(str(f))["status"] == "error"


def test_pivot_with_no_path_is_an_error(wiring):
    assert wiring.do_pivot("")["status"] == "error"


def test_rollback_returns_the_default_without_unwatching(wiring, tmp_path):
    alpha_dir = _project(tmp_path, "alpha")
    beta_dir = _project(tmp_path, "beta")
    wiring.do_pivot(str(alpha_dir))
    wiring.do_pivot(str(beta_dir))

    result = wiring.do_rollback()

    assert result["status"] == "ok"
    assert wiring.state.primary == alpha_dir.resolve()
    assert len(wiring.registry) == 2, "rollback must not unwatch"


def test_rollback_with_no_history_is_an_error(wiring):
    assert wiring.do_rollback()["status"] == "error"


# ── audit and ignore ─────────────────────────────────────────────────────────

def test_an_audit_snapshots_the_project_being_viewed(wiring, tmp_path):
    wiring.registry.add(_project(tmp_path, "alpha"))
    beta = wiring.registry.add(_project(tmp_path, "beta"))
    wiring.state.primary = tmp_path / "alpha"

    result = wiring.do_audit("label", "disk", str(beta.path))

    assert result["project_name"] == "beta"
    assert beta.builder.snapshots == [("disk", "label")]


def test_an_events_audit_captures_that_projects_events_only(wiring, tmp_path):
    alpha = wiring.registry.add(_project(tmp_path, "alpha"))
    beta = wiring.registry.add(_project(tmp_path, "beta"))
    wiring.state.handler.events = [
        _event(alpha.path / "a.py"),
        _event(beta.path / "b.py"),
    ]

    wiring.do_audit("l", "events", str(beta.path))
    assert beta.builder.snapshots == [("events", "l", 1)]


def test_an_audit_while_idle_is_an_error(wiring):
    assert wiring.do_audit("l", "disk", None)["status"] == "error"


def test_an_ignore_pattern_is_added(wiring):
    assert wiring.do_ignore("*.secret")["status"] == "ok"
    assert "*.secret" in wiring._settings.ignore_patterns


def test_an_empty_ignore_pattern_is_an_error(wiring):
    assert wiring.do_ignore("")["status"] == "error"


def test_the_callback_tuple_matches_start_dashboard(wiring):
    """Positional order is the contract with dashboard.start_dashboard."""
    import inspect

    from dashboard import start_dashboard

    params = list(inspect.signature(start_dashboard).parameters)[1:]
    names = [c.__name__ for c in wiring.as_callbacks()]
    assert names == params
