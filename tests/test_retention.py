"""Flushing recorded audit data at startup.

This is the only code in Sentinel that deletes anything, and it deletes at a
path supplied by an environment variable. The tests that matter most here are
the ones proving it REFUSES: a mis-set AUDIT_DIR must cost nothing, because
every other failure in this codebase is recoverable and this one is not.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from retention import flush_audit_data, flush_if_configured  # noqa: E402


def _project(root: Path, name: str, *, db=True, md=True) -> Path:
    d = root / name
    d.mkdir(parents=True)
    if db:
        (d / "sentinel.db").write_bytes(b"x" * 1024)
    if md:
        (d / "audit-2026-08-03.md").write_text("log", encoding="utf-8")
    return d


@pytest.fixture
def audit_root(tmp_path):
    root = tmp_path / "work" / "proj" / "audit_logs"
    root.mkdir(parents=True)
    return root


# ── it removes audit output ──────────────────────────────────────────────────

def test_project_directories_are_removed(audit_root):
    _project(audit_root, "Alpha")
    _project(audit_root, "Beta")

    result = flush_audit_data(audit_root)

    assert sorted(result["removed"]) == ["Alpha", "Beta"]
    assert not (audit_root / "Alpha").exists()
    assert not (audit_root / "Beta").exists()


def test_the_audit_root_itself_survives(audit_root):
    _project(audit_root, "Alpha")
    flush_audit_data(audit_root)
    assert audit_root.is_dir()


def test_bytes_freed_is_reported(audit_root):
    _project(audit_root, "Alpha")
    result = flush_audit_data(audit_root)
    assert result["bytes_freed"] > 1000


def test_empty_project_dirs_are_removed(audit_root):
    """Pivot creates one per project whether or not it ever records."""
    (audit_root / "NeverUsed").mkdir()
    result = flush_audit_data(audit_root)
    assert "NeverUsed" in result["removed"]


def test_snapshots_and_gap_reports_are_audit_output_too(audit_root):
    d = audit_root / "Alpha"
    d.mkdir()
    (d / "snapshot-20260803T000000Z-x.md").write_text("s", encoding="utf-8")
    (d / "gap-20260803T000000Z.md").write_text("g", encoding="utf-8")

    assert "Alpha" in flush_audit_data(audit_root)["removed"]


def test_loose_log_files_are_removed(audit_root):
    (audit_root / "sentinel.log").write_text("x", encoding="utf-8")
    (audit_root / "sentinel.stdio.log").write_text("x", encoding="utf-8")

    removed = flush_audit_data(audit_root)["removed"]
    assert "sentinel.log" in removed and "sentinel.stdio.log" in removed


def test_flushing_an_empty_root_is_not_an_error(audit_root):
    result = flush_audit_data(audit_root)
    assert result["removed"] == [] and result["errors"] == []


def test_a_missing_root_is_not_an_error(tmp_path):
    result = flush_audit_data(tmp_path / "does" / "not" / "exist")
    assert result["removed"] == [] and result["errors"] == []


# ── it refuses what is not its own ───────────────────────────────────────────

def test_an_unrecognised_directory_is_left_alone(audit_root):
    """The mis-set AUDIT_DIR case: source must survive."""
    src = audit_root / "src"
    src.mkdir()
    (src / "main.py").write_text("print(1)", encoding="utf-8")

    result = flush_audit_data(audit_root)

    assert src.exists() and (src / "main.py").exists()
    assert "src" not in result["removed"]
    assert any("unrecognised" in s for s in result["skipped"])


def test_an_unrecognised_loose_file_is_left_alone(audit_root):
    keeper = audit_root / "README.md"
    keeper.write_text("important", encoding="utf-8")

    flush_audit_data(audit_root)

    assert keeper.exists()


def test_a_directory_of_source_beside_a_project_survives(audit_root):
    _project(audit_root, "Alpha")
    src = audit_root / "mycode"
    src.mkdir()
    (src / "app.py").write_text("x", encoding="utf-8")

    flush_audit_data(audit_root)

    assert not (audit_root / "Alpha").exists(), "audit data still goes"
    assert (src / "app.py").exists(), "source still stays"


@pytest.mark.parametrize("bad", ["C:/", "E:/", "/"])
def test_a_root_too_close_to_the_filesystem_root_is_refused(bad):
    result = flush_audit_data(Path(bad))
    assert result["removed"] == []
    if Path(bad).is_dir():
        assert result["errors"], "must say why it refused"


def test_refusal_names_the_variable_to_check(tmp_path, monkeypatch):
    shallow = Path(tmp_path.anchor)
    result = flush_audit_data(shallow)
    if result["errors"]:
        assert "AUDIT_DIR" in result["errors"][0]


# ── keep list and dry run ────────────────────────────────────────────────────

def test_a_kept_project_survives(audit_root):
    _project(audit_root, "Alpha")
    _project(audit_root, "Keeper")

    result = flush_audit_data(audit_root, keep=["Keeper"])

    assert (audit_root / "Keeper").exists()
    assert not (audit_root / "Alpha").exists()
    assert "Alpha" in result["removed"]


def test_the_keep_list_is_case_insensitive(audit_root):
    _project(audit_root, "Keeper")
    flush_audit_data(audit_root, keep=["keeper"])
    assert (audit_root / "Keeper").exists()


def test_a_dry_run_deletes_nothing_but_reports_everything(audit_root):
    _project(audit_root, "Alpha")
    _project(audit_root, "Beta")

    result = flush_audit_data(audit_root, dry_run=True)

    assert sorted(result["removed"]) == ["Alpha", "Beta"]
    assert result["bytes_freed"] > 0
    assert (audit_root / "Alpha").exists()
    assert (audit_root / "Beta").exists()


# ── the settings switch ──────────────────────────────────────────────────────

class _Settings:
    def __init__(self, root, flush):
        self.base_audit_dir = root
        self.flush_on_start = flush


def test_disabled_means_nothing_is_touched(audit_root):
    _project(audit_root, "Alpha")

    result = flush_if_configured(_Settings(audit_root, False))

    assert result["removed"] == []
    assert (audit_root / "Alpha").exists()


def test_enabled_flushes(audit_root):
    _project(audit_root, "Alpha")

    result = flush_if_configured(_Settings(audit_root, True))

    assert "Alpha" in result["removed"]
    assert not (audit_root / "Alpha").exists()


def test_settings_without_the_attribute_do_not_flush(audit_root):
    """A stale Settings object must fail safe, not delete."""
    _project(audit_root, "Alpha")

    class Bare:
        base_audit_dir = audit_root

    assert flush_if_configured(Bare())["removed"] == []
    assert (audit_root / "Alpha").exists()


def test_the_old_single_watch_layout_is_flushed_too(audit_root):
    """Daily logs and snapshots used to be written into the audit ROOT."""
    (audit_root / "audit-2026-04-25.md").write_text("old", encoding="utf-8")
    (audit_root / "snapshot-20260425T025232Z-x.md").write_text("s", encoding="utf-8")
    (audit_root / "gap-20260425T025232Z.md").write_text("g", encoding="utf-8")

    removed = flush_audit_data(audit_root)["removed"]

    assert len(removed) == 3
    assert not any(audit_root.glob("*.md"))


def test_wal_sidecars_are_flushed(audit_root):
    for suffix in ("", "-wal", "-shm", "-journal"):
        (audit_root / f"sentinel.db{suffix}").write_bytes(b"x")

    flush_audit_data(audit_root)

    assert not any(audit_root.glob("sentinel.db*"))


def test_a_readme_still_survives_the_wider_patterns(audit_root):
    (audit_root / "README.md").write_text("keep", encoding="utf-8")
    (audit_root / "notes.md").write_text("keep", encoding="utf-8")

    flush_audit_data(audit_root)

    assert (audit_root / "README.md").exists()
    assert (audit_root / "notes.md").exists()
