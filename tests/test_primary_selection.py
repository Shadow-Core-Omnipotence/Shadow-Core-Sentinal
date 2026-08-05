"""The read side must not report idle while a project is recording.

REGRESSION
----------
`state.primary` was assigned only by the dashboard's pivot/rollback and by
unwatch_project — never by `watch_project`, which is the documented and only
session-facing way to start monitoring. Since `SentinelState.store` and
`.builder` resolve through `_primary_entry()`, a session that watched correctly
left every read tool answering "No project is being watched. Call watch_project
first." while events were being written to disk the whole time.

Confirmed against the running service before the fix: two projects watched,
`"primary": "None"`, `recent_changes` reporting idle.

That is the single worst failure mode this codebase can have — the verification
tool being confidently wrong about whether it is verifying anything.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from main import SentinelState  # noqa: E402
from watch_registry import WatchRegistry  # noqa: E402


class FakeStore:
    def __init__(self, db_path, watch_path=None):
        self.db_path = db_path
        self.watch_path = watch_path

    def close(self):
        pass


class FakeBuilder:
    def __init__(self, audit_dir, watch_path=None):
        self.audit_dir = audit_dir
        self.watch_path = watch_path


@pytest.fixture
def registry(tmp_path):
    return WatchRegistry(
        base_audit_dir=tmp_path / "audit",
        make_store=FakeStore,
        make_builder=FakeBuilder,
    )


@pytest.fixture
def state(registry):
    return SentinelState(handler=None, registry=registry, primary=None)


def _project(tmp_path, name):
    p = tmp_path / name
    p.mkdir()
    return p


def test_idle_service_really_has_no_store(state):
    """Nothing watched is genuinely idle — the fallback must not invent one."""
    assert state.store is None
    assert state.builder is None


def test_one_watched_project_is_readable_without_an_explicit_primary(
    state, registry, tmp_path
):
    """The regression: watched, recording, and readable — with primary unset."""
    registry.add(_project(tmp_path, "alpha"))

    assert state.primary is None
    assert state.store is not None, "read side blind while one project records"
    assert state.builder is not None


def test_the_fallback_picks_the_project_that_is_actually_watched(
    state, registry, tmp_path
):
    entry = registry.add(_project(tmp_path, "alpha"))
    assert state.store is entry.store


def test_two_projects_without_a_primary_stay_ambiguous(state, registry, tmp_path):
    """Two watches and no stated default is a real ambiguity.

    Guessing here would silently answer questions about the wrong project,
    which is worse than saying nothing. `watch_project` sets `primary`, so
    this state is not reachable through the supported path.
    """
    registry.add(_project(tmp_path, "alpha"))
    registry.add(_project(tmp_path, "beta"))

    assert state.store is None


def test_an_explicit_primary_wins_over_the_fallback(state, registry, tmp_path):
    registry.add(_project(tmp_path, "alpha"))
    beta = registry.add(_project(tmp_path, "beta"))

    state.primary = beta.path
    assert state.store is beta.store
    assert state.builder is beta.builder


def test_a_primary_pointing_at_an_unwatched_path_reads_as_idle(
    state, registry, tmp_path
):
    """Stale primary after an unwatch must not resurrect a closed store."""
    alpha = registry.add(_project(tmp_path, "alpha"))
    registry.add(_project(tmp_path, "beta"))
    state.primary = alpha.path
    registry.remove(alpha.path)

    # Two entries became one, but `primary` still names the removed project.
    assert state.store is None


# ── how the primary is REPORTED ──────────────────────────────────────────────

def test_an_unset_primary_is_none_not_the_string_None(state):
    """`str(None)` is "None" — it was being emitted as a project path."""
    assert state.primary_path is None


def test_primary_path_names_the_project_being_answered_about(
    state, registry, tmp_path
):
    """It must agree with `store`, not with the raw `primary` field."""
    entry = registry.add(_project(tmp_path, "alpha"))

    assert state.primary is None
    assert state.primary_path == entry.path
    assert state.store is entry.store


def test_primary_path_follows_an_explicit_primary(state, registry, tmp_path):
    registry.add(_project(tmp_path, "alpha"))
    beta = registry.add(_project(tmp_path, "beta"))
    state.primary = beta.path

    assert state.primary_path == beta.path
