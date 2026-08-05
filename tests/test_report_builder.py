"""ReportBuilder writes and reads ONE project's artifacts.

A builder is created per watched project and owns that project's audit
directory. The failure this guards against is a read that resolves somewhere
else — historically `settings.audit_dir / name`, the global single-watch path,
which stays at the boot default because `watch_project` never moves it. The
names came from this builder's `list_artifacts()`; the reads went to a
different directory entirely.
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from models import AuditEvent, EventKind  # noqa: E402
from report_builder import ReportBuilder  # noqa: E402


@pytest.fixture
def project(tmp_path):
    audit = tmp_path / "audit" / "Alpha"
    audit.mkdir(parents=True)
    watched = tmp_path / "alpha"
    watched.mkdir()
    return ReportBuilder(audit, watched), audit, watched


def _event(path, kind=EventKind.MODIFIED, sha="abc123"):
    return AuditEvent(kind=kind, src_path=path, sha256=sha)


# ── the artifact a name refers to ────────────────────────────────────────────

def test_a_listed_snapshot_can_be_read_back_by_its_name(project):
    """The regression: list_artifacts and read must agree on a directory."""
    builder, _, _ = project
    snap = builder.build_snapshot([], label="probe")

    assert snap.name in [a.name for a in builder.list_artifacts()]
    assert builder.read_artifact_by_name(snap.name) is not None


def test_reading_by_name_uses_this_builders_directory(project, tmp_path):
    """A same-named artifact in another project must not be picked up."""
    builder, _, _ = project
    other = tmp_path / "audit" / "Beta"
    other.mkdir(parents=True)
    (other / "snapshot-decoy.md").write_text("BETA", encoding="utf-8")

    assert builder.read_artifact_by_name("snapshot-decoy.md") is None


def test_a_missing_artifact_is_none_not_an_error(project):
    builder, _, _ = project
    assert builder.read_artifact_by_name("snapshot-nope.md") is None


@pytest.mark.parametrize("name", [
    "../Beta/sentinel.db",
    r"..\Beta\sentinel.db",
    "sub/dir.md",
    "..",
    "",
])
def test_a_name_is_a_filename_not_a_path(project, name):
    """Snapshot names arrive from a tool argument, so they are untrusted."""
    builder, _, _ = project
    assert builder.read_artifact_by_name(name) is None


def test_traversal_cannot_reach_a_real_file_one_level_up(project, tmp_path):
    builder, audit, _ = project
    secret = audit.parent / "secret.md"
    secret.write_text("SECRET", encoding="utf-8")

    assert secret.exists()
    assert builder.read_artifact_by_name("../secret.md") is None


# ── daily log ────────────────────────────────────────────────────────────────

def test_the_daily_log_is_created_with_a_header_then_appended(project):
    builder, audit, watched = project
    evt = _event(watched / "main.py")

    path = builder.append_event(evt)
    text = path.read_text(encoding="utf-8")

    assert path.parent == audit
    assert f"# Folder Audit Log — {evt.date_key()}" in text
    assert str(watched) in text, "header must name the tree it describes"
    assert "main.py" in text


def test_appending_twice_keeps_one_header_and_two_rows(project):
    builder, _, watched = project
    builder.append_event(_event(watched / "a.py"))
    path = builder.append_event(_event(watched / "b.py"))

    text = path.read_text(encoding="utf-8")
    assert text.count("# Folder Audit Log") == 1
    assert "a.py" in text and "b.py" in text


def test_a_move_records_both_ends(project):
    builder, _, watched = project
    evt = AuditEvent(
        kind=EventKind.MOVED,
        src_path=watched / "old.py",
        dest_path=watched / "new.py",
        sha256="deadbeef",
    )
    text = builder.append_event(evt).read_text(encoding="utf-8")

    assert "old.py" in text and "new.py" in text
    assert "MOVED" in text


def test_the_sha_reaches_the_row(project):
    builder, _, watched = project
    text = builder.append_event(
        _event(watched / "a.py", sha="f" * 64)).read_text(encoding="utf-8")
    assert "f" * 64 in text


def test_reading_a_day_with_no_log_is_none(project):
    builder, _, _ = project
    assert builder.read_artifact("1999-01-01") is None


def test_the_day_written_is_the_day_read_back(project):
    builder, _, watched = project
    evt = _event(watched / "a.py")
    builder.append_event(evt)
    assert builder.read_artifact(evt.date_key()) is not None


# ── snapshots ────────────────────────────────────────────────────────────────

def test_an_empty_event_snapshot_says_so_rather_than_being_blank(project):
    builder, _, _ = project
    text = builder.build_snapshot([], label="empty").read_text(encoding="utf-8")
    assert "No events recorded" in text
    assert "**Events captured:** 0" in text


def test_an_event_snapshot_reports_what_it_captured(project):
    builder, _, watched = project
    events = [_event(watched / f"f{i}.py") for i in range(3)]
    text = builder.build_snapshot(events, label="three").read_text(encoding="utf-8")

    assert "**Events captured:** 3" in text
    for i in range(3):
        assert f"f{i}.py" in text


def test_a_disk_snapshot_inventories_the_builders_own_tree(project):
    """Not the global watch dir — the tree this builder was told about."""
    builder, _, watched = project
    (watched / "kept.py").write_text("x", encoding="utf-8")

    text = builder.build_disk_snapshot(label="disk").read_text(encoding="utf-8")

    assert "kept.py" in text
    assert str(watched) in text


def test_a_disk_snapshot_skips_ignored_directories(project):
    builder, _, watched = project
    (watched / "kept.py").write_text("x", encoding="utf-8")
    noise = watched / "__pycache__"
    noise.mkdir()
    (noise / "junk.pyc").write_text("x", encoding="utf-8")

    text = builder.build_disk_snapshot(label="disk").read_text(encoding="utf-8")

    assert "kept.py" in text
    assert "junk.pyc" not in text


def test_a_disk_snapshot_of_an_empty_tree_inventories_nothing(project):
    builder, _, _ = project
    text = builder.build_disk_snapshot(label="bare").read_text(encoding="utf-8")
    assert "**Files inventoried:** 0" in text


def test_snapshots_land_in_this_projects_audit_dir(project):
    builder, audit, _ = project
    assert builder.build_snapshot([], "a").parent == audit
    assert builder.build_disk_snapshot("b").parent == audit


def test_list_artifacts_is_newest_first(project):
    """Ordering is by mtime, so it must be set explicitly.

    Two writes inside one test land in the same filesystem timestamp tick,
    which makes the order arbitrary — the ambiguity is in the test, not in
    `list_artifacts`.
    """
    import os
    import time

    builder, audit, _ = project
    old = audit / "audit-2020-01-01.md"
    old.write_text("old", encoding="utf-8")
    new = builder.build_snapshot([], label="new")

    now = time.time()
    os.utime(old, (now - 3600, now - 3600))
    os.utime(new, (now, now))

    names = [a.name for a in builder.list_artifacts()]
    assert names.index(new.name) < names.index(old.name)


def test_the_watch_path_falls_back_to_the_global_when_unset(tmp_path):
    """Single-watch callers that pass no tree must still work."""
    from config import settings

    audit = tmp_path / "audit"
    audit.mkdir()
    assert ReportBuilder(audit).watch_path == settings.watch_dir


def test_a_timestamped_snapshot_name_is_sortable(project):
    builder, _, _ = project
    name = builder.build_snapshot([], label="probe").name
    stamp = name[len("snapshot-"):name.index("-probe")]
    parsed = datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    assert abs((datetime.now(timezone.utc) - parsed).total_seconds()) < 120
