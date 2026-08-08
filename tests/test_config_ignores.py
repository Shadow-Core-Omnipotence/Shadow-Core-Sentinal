"""The ignore filter must not silently blind a watch.

REGRESSION
----------
`_auto_ignore_audit_dir()` ran on every `update_watch_dir` and appended
`rel.parts[0]` — the first path component between the watched root and the
audit dir — to the process-global `ignore_patterns`. That list is matched
component-wise against every path in every watched project, so a bare
directory name landing in it disables recording far beyond the project that
caused it.

Measured before the fix: after `update_watch_dir(Path(r"C:\\work\\projects"))`
the literal pattern `Shadow-Core Sentinel` was added and
`is_ignored(...\\Shadow-Core Sentinel\\main.py)` returned True — the audit tool
had stopped recording its own source tree, permanently, with no signal.

Audit output is now excluded by containment instead, which covers every
project's audit dir at once and writes nothing into shared state.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import Settings  # noqa: E402


@pytest.fixture
def settings(tmp_path, monkeypatch):
    """A Settings of our own — never the module-global singleton.

    These tests mutate watch_dir, and the shared `config.settings` object is
    read by observer, report_builder and lease during the rest of the suite.
    """
    monkeypatch.chdir(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    return Settings(watch_dir=work, base_audit_dir=tmp_path / "audit_logs")


# ── the regression ───────────────────────────────────────────────────────────

def test_pivoting_to_a_parent_does_not_blind_the_child(settings, tmp_path):
    """The exact reproduction, at the shape it was measured."""
    parent = tmp_path / "Projects"
    child = parent / "Shadow-Core Sentinel"
    child.mkdir(parents=True)
    source = child / "main.py"
    source.write_text("x", encoding="utf-8")

    settings.update_watch_dir(parent)

    assert not settings.is_ignored(source), (
        "watching a parent must not make its child's source invisible"
    )


def test_a_pivot_adds_no_patterns_at_all(settings, tmp_path):
    """Pattern count was 32 → 34 per pivot, growing without bound."""
    before = list(settings.ignore_patterns)

    other = tmp_path / "other"
    other.mkdir()
    settings.update_watch_dir(other)
    settings.update_watch_dir(tmp_path / "work")

    assert settings.ignore_patterns == before


def test_no_project_directory_name_leaks_into_the_patterns(settings, tmp_path):
    parent = tmp_path / "Projects"
    (parent / "Distinctive-Name").mkdir(parents=True)
    settings.update_watch_dir(parent)

    assert "Distinctive-Name" not in settings.ignore_patterns
    assert "Projects" not in settings.ignore_patterns


def test_a_second_project_is_unaffected_by_the_first_ones_pivot(settings, tmp_path):
    """The cross-session failure: one pivot must not blind another watch."""
    other = tmp_path / "elsewhere" / "work"
    other.mkdir(parents=True)
    its_file = other / "app.py"
    its_file.write_text("x", encoding="utf-8")

    settings.update_watch_dir(tmp_path / "Projects" / "deep" / "nested")
    assert not settings.is_ignored(its_file)


# ── audit output is still excluded ───────────────────────────────────────────

def test_the_current_audit_dir_is_ignored(settings):
    assert settings.is_ignored(settings.audit_dir / "audit-2026-01-01.md")


def test_the_base_audit_dir_is_ignored(settings):
    assert settings.is_ignored(settings.base_audit_dir / "anything.md")


def test_another_projects_audit_dir_is_ignored_too(settings):
    """Containment covers every project at once, not just the primary."""
    assert settings.is_ignored(
        settings.base_audit_dir / "SomeOtherProject" / "sentinel.db")


def test_the_audit_dir_stays_ignored_after_a_pivot(settings, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    settings.update_watch_dir(other)

    assert settings.is_ignored(settings.audit_dir / "audit-2026-01-01.md")
    assert settings.is_ignored(settings.base_audit_dir / "x.md")


def test_an_audit_dir_nested_inside_the_watched_tree_is_ignored(tmp_path, monkeypatch):
    """The case the deleted anchor logic was written for."""
    monkeypatch.chdir(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    s = Settings(watch_dir=work, base_audit_dir=work / "audit_logs")

    assert s.is_ignored(work / "audit_logs" / "Work" / "audit-2026-01-01.md")
    assert not s.is_ignored(work / "main.py")


# ── the ordinary patterns still work ─────────────────────────────────────────

def test_noise_directories_are_still_ignored(settings, tmp_path):
    for noisy in ("node_modules", ".git", "__pycache__", "venv", ".venv"):
        assert settings.is_ignored(tmp_path / "work" / noisy / "f.py"), noisy


def test_editor_temp_files_are_still_ignored(settings, tmp_path):
    assert settings.is_ignored(tmp_path / "work" / "main.py.tmp.15328.5bb47c1b")


def test_real_source_is_still_recorded(settings, tmp_path):
    assert not settings.is_ignored(tmp_path / "work" / "main.py")


def test_build_globs_still_do_not_swallow_real_source_dirs(settings, tmp_path):
    assert not settings.is_ignored(tmp_path / "work" / "distribution" / "a.py")
    assert not settings.is_ignored(tmp_path / "work" / "builder" / "a.py")
    assert settings.is_ignored(tmp_path / "work" / "build_new" / "a.py")


def test_an_added_pattern_takes_effect(settings, tmp_path):
    settings.add_ignore_pattern("*.secret")
    assert settings.is_ignored(tmp_path / "work" / "keys.secret")


def test_adding_the_same_pattern_twice_stores_one(settings):
    settings.add_ignore_pattern("*.secret")
    settings.add_ignore_pattern("*.secret")
    assert settings.ignore_patterns.count("*.secret") == 1


# ── rollback ─────────────────────────────────────────────────────────────────

def test_rollback_restores_the_previous_watch_dir(settings, tmp_path):
    first = settings.watch_dir
    other = tmp_path / "other"
    other.mkdir()

    settings.update_watch_dir(other)
    assert settings.watch_dir == other.resolve()

    settings.rollback_watch_dir()
    assert settings.watch_dir == first


def test_rollback_with_no_history_is_none(settings):
    assert settings.rollback_watch_dir() is None


def test_audit_dir_follows_the_watch_dir(settings, tmp_path):
    other = tmp_path / "Renamed"
    other.mkdir()
    settings.update_watch_dir(other)

    assert settings.audit_dir == settings.base_audit_dir / "Renamed"
    assert settings.project_name == "Renamed"
