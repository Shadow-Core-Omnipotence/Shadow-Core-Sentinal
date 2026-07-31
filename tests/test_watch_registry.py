"""Watching several projects at once (2026-07-31).

The first tests this repo has had (TECH_DEBT_AUDIT.md #15), and they cover the
change most likely to corrupt an audit trail: attributing an event to the wrong
project.

The failure being designed out: Sentinel watched ONE directory, and pivoting
called observer.unschedule_all(). With two Claude sessions on two projects
against one shared Sentinel, session B's pivot silently stopped session A's
monitoring — and session A carried on confirming changes against a directory it
was no longer watching.
"""
from pathlib import Path

import pytest

from watch_registry import WatchRegistry, safe_project_name


class FakeStore:
    def __init__(self, db_path, watch_path):
        self.db_path = db_path
        self.watch_path = watch_path
        self.closed = False
        self.rows = []

    def insert(self, event):
        self.rows.append(event)

    def close(self):
        self.closed = True


class FakeBuilder:
    def __init__(self, audit_dir):
        self.audit_dir = audit_dir


@pytest.fixture
def registry(tmp_path):
    return WatchRegistry(
        base_audit_dir=tmp_path / "audit_logs",
        make_store=FakeStore,
        make_builder=FakeBuilder,
    )


@pytest.fixture
def projects(tmp_path):
    a = tmp_path / "ProjectAlpha"
    b = tmp_path / "ProjectBeta"
    a.mkdir()
    b.mkdir()
    return a, b


# ── the point: watches accumulate ────────────────────────────────────────────
def test_adding_a_second_watch_keeps_the_first(registry, projects):
    """THE regression. Two sessions, two projects, neither disturbs the other."""
    a, b = projects
    registry.add(a)
    registry.add(b)

    assert len(registry) == 2
    assert set(registry.paths()) == {str(a), str(b)}


def test_adding_the_same_path_twice_is_one_watch(registry, projects):
    """A session may call this at startup without knowing another already did."""
    a, _ = projects
    first = registry.add(a)
    second = registry.add(a)

    assert first is second
    assert len(registry) == 1


def test_removal_is_explicit_and_leaves_the_others(registry, projects):
    a, b = projects
    registry.add(a)
    registry.add(b)

    removed = registry.remove(a)

    assert removed.path == a
    assert registry.paths() == [str(b)]


def test_removing_an_unwatched_path_is_not_an_error(registry, projects):
    a, _ = projects
    assert registry.remove(a) is None


def test_removal_closes_the_store(registry, projects):
    a, _ = projects
    entry = registry.add(a)
    registry.remove(a)
    assert entry.store.closed is True


# ── storage layout must not change ───────────────────────────────────────────
def test_each_project_keeps_its_own_database(registry, projects):
    a, b = projects
    ea = registry.add(a)
    eb = registry.add(b)

    assert ea.store.db_path != eb.store.db_path
    assert ea.store.db_path.name == "sentinel.db"
    assert ea.audit_dir.name == "ProjectAlpha"
    assert eb.audit_dir.name == "ProjectBeta"


def test_audit_folder_name_matches_the_old_single_watch_scheme(tmp_path):
    """Changing this would orphan every existing sentinel.db on disk."""
    assert safe_project_name(Path(r"C:\work\Shadow-Core Engineer")) == "Shadow-Core-Engineer"
    assert safe_project_name(Path(r"C:\work\My Project!")) == "My-Project"


def test_audit_directory_is_created(registry, projects):
    a, _ = projects
    entry = registry.add(a)
    assert entry.audit_dir.is_dir()


# ── routing: the way an audit trail gets silently corrupted ──────────────────
def test_an_event_is_attributed_to_its_own_project(registry, projects):
    a, b = projects
    registry.add(a)
    registry.add(b)

    assert registry.route(a / "src" / "main.py").path == a
    assert registry.route(b / "docs" / "readme.md").path == b


def test_a_nested_project_wins_over_its_parent(registry, tmp_path):
    """Watching C:\\work and C:\\work\\api — a file under api belongs to api.

    First-match or shortest-prefix would file every nested project's events
    under its parent, which is exactly how a trail becomes untrustworthy.
    """
    parent = tmp_path / "work"
    child = parent / "api"
    child.mkdir(parents=True)
    registry.add(parent)
    registry.add(child)

    assert registry.route(child / "server.py").path == child
    assert registry.route(parent / "notes.md").path == parent


def test_a_sibling_with_a_shared_prefix_is_not_matched(registry, tmp_path):
    """`C:\\work-old` must not match a `C:\\work` watch — a plain string
    startswith would say it does."""
    work = tmp_path / "work"
    work_old = tmp_path / "work-old"
    work.mkdir()
    work_old.mkdir()
    registry.add(work)

    assert registry.route(work_old / "file.txt") is None


def test_an_unwatched_path_routes_nowhere(registry, projects, tmp_path):
    a, _ = projects
    registry.add(a)
    assert registry.route(tmp_path / "elsewhere" / "file.txt") is None


def test_routing_the_watch_root_itself(registry, projects):
    a, _ = projects
    registry.add(a)
    assert registry.route(a).path == a


@pytest.mark.skipif(Path("C:/").exists() is False, reason="Windows path semantics")
def test_case_differences_do_not_create_a_second_watch(registry, tmp_path):
    """On Windows C:\\Work and C:\\work are one directory."""
    d = tmp_path / "CaseTest"
    d.mkdir()
    registry.add(d)
    registry.add(Path(str(d).upper()))
    assert len(registry) == 1


# ── shutdown ─────────────────────────────────────────────────────────────────
def test_close_all_closes_every_store(registry, projects):
    a, b = projects
    ea = registry.add(a)
    eb = registry.add(b)

    registry.close_all()

    assert ea.store.closed and eb.store.closed
    assert len(registry) == 0


def test_a_store_that_fails_to_close_does_not_break_shutdown(registry, projects):
    a, b = projects
    ea = registry.add(a)
    registry.add(b)

    def boom():
        raise OSError("database is locked")

    ea.store.close = boom
    registry.close_all()           # must not raise
    assert len(registry) == 0


# ── idle: the state Sentinel now BOOTS into ─────────────────────────────────
def test_a_new_registry_is_empty(registry):
    """Sentinel starts with the PC and watches nothing until asked.

    It used to schedule whatever was watched last, so the service came up
    recording a project nobody had asked about -- the running instance was
    found watching Shadow-Core Engineer purely because that was the last pivot.
    """
    assert len(registry) == 0
    assert registry.paths() == []
    assert registry.entries() == []


def test_routing_while_idle_returns_nothing(registry, tmp_path):
    """Every read path must survive having no project at all. Between sessions
    that is the NORMAL state, not an error."""
    assert registry.route(tmp_path / "anything.py") is None


def test_removing_the_last_watch_returns_to_idle(registry, projects):
    """Idle is a legitimate destination, not a failure to be refused."""
    a, _ = projects
    registry.add(a)
    registry.remove(a)

    assert len(registry) == 0
    assert registry.route(a / "file.py") is None


def test_watching_can_resume_after_going_idle(registry, projects):
    a, b = projects
    registry.add(a)
    registry.remove(a)

    entry = registry.add(b)
    assert len(registry) == 1
    assert registry.route(b / "x.py") is entry


# ── ignore patterns: found by watching a real project ────────────────────────
def test_virtualenv_directories_are_ignored():
    """Measured 2026-07-31: watching Scriptweaver recorded python.exe,
    pyvenv.cfg and distutils-precedence.pth as project activity, because the
    ignore list had `.venv` but not plain `venv` -- and that project keeps its
    interpreter at backend/venv. 1.3 GB of virtualenv churn drowns the signal.
    """
    from config import settings
    for name in ("venv", ".venv", ".venv311", "env", "site-packages",
                 "node_modules", "__pycache__"):
        assert name in settings.ignore_patterns, f"{name} should be ignored"
