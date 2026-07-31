"""One edit should be one event, and nothing should claim a live file was
deleted (2026-07-31).

Measured against the real trail while Sentinel watched its own repo: a single
edit to mcp_server.py recorded FOUR rows —

    09:04:14.187  CREATED   mcp_server.py.tmp.15328.5bb47c1b481e
    09:04:14.188  MODIFIED  mcp_server.py.tmp.15328.5bb47c1b481e
    09:04:14.318  MOVED     mcp_server.py.tmp... -> mcp_server.py
    09:04:14.374  DELETED   mcp_server.py

247 events in 45 minutes for ~20 edits. Three of those four rows are about a
scratch file that no longer exists, and the fourth is FALSE: mcp_server.py was
never deleted, it was atomically replaced. A caller asking Sentinel to confirm
an edit landed would read that last row and conclude the opposite.
"""
import time
from pathlib import Path

import pytest

from config import settings
from models import EventKind
from observer import AuditEventHandler, add_watch, start_bare_observer


@pytest.fixture
def watched(tmp_path):
    """A live observer over a real directory, collecting emitted events."""
    root = tmp_path / "proj"
    root.mkdir()
    (root / "main.py").write_text("original\n")

    handler = AuditEventHandler()
    seen = []
    handler.subscribe(seen.append)

    observer = start_bare_observer()
    add_watch(observer, handler, root)
    time.sleep(0.3)
    seen.clear()

    yield root, seen

    observer.stop()
    observer.join(timeout=2)
    handler.shutdown()


def settle(seen, quiet=1.2, limit=6.0):
    """Wait until events stop arriving."""
    deadline = time.time() + limit
    last = len(seen)
    stable = time.time()
    while time.time() < deadline:
        time.sleep(0.15)
        if len(seen) != last:
            last, stable = len(seen), time.time()
        elif time.time() - stable > quiet:
            break
    return seen


def atomic_write(target: Path, text: str, pid: int = 15328) -> None:
    """Replicate the write Claude Code's Edit tool performs."""
    tmp = target.with_name(f"{target.name}.tmp.{pid}.5bb47c1b481e")
    tmp.write_text(text)
    tmp.replace(target)


# ── ignore patterns ─────────────────────────────────────────────────────────
def test_editor_temp_files_are_ignored(tmp_path):
    assert settings.is_ignored(tmp_path / "main.py.tmp.15328.5bb47c1b481e")
    assert settings.is_ignored(tmp_path / "notes.txt.tmp")
    assert settings.is_ignored(tmp_path / "main.py~")
    assert settings.is_ignored(tmp_path / ".main.py.swp")
    assert settings.is_ignored(tmp_path / "main.py.orig")


def test_real_source_files_are_not_ignored(tmp_path):
    """The temp globs must not swallow ordinary files."""
    assert not settings.is_ignored(tmp_path / "main.py")
    assert not settings.is_ignored(tmp_path / "tmp.py")
    assert not settings.is_ignored(tmp_path / "tmpfile.md")
    assert not settings.is_ignored(tmp_path / "src" / "temperature.py")


def test_staging_build_dirs_are_ignored(tmp_path):
    assert settings.is_ignored(tmp_path / "build_new" / "app" / "PYZ-00.toc")
    assert settings.is_ignored(tmp_path / "dist_new" / "app.exe")
    assert settings.is_ignored(tmp_path / "build" / "x.o")
    assert settings.is_ignored(tmp_path / "dist" / "x.whl")


def test_build_globs_do_not_swallow_real_source_dirs(tmp_path):
    """Why the pattern is "build_*" and not "build*": ignore globs match on any
    path COMPONENT, so a broader glob would silently stop recording real code."""
    assert not settings.is_ignored(tmp_path / "distributed" / "worker.py")
    assert not settings.is_ignored(tmp_path / "distribution" / "setup.py")
    assert not settings.is_ignored(tmp_path / "builder" / "make.py")
    assert not settings.is_ignored(tmp_path / "buildings" / "model.py")


# ── the phantom delete ──────────────────────────────────────────────────────
def test_atomic_replace_never_reports_a_delete(watched):
    """The bug that made the trail actively misleading."""
    root, seen = watched
    atomic_write(root / "main.py", "edited\n")
    settle(seen)

    deletes = [e for e in seen if e.kind is EventKind.DELETED]
    assert deletes == [], f"file still exists but was reported deleted: {deletes}"
    assert (root / "main.py").exists()


def test_atomic_replace_records_the_edit(watched):
    """Suppressing the noise must not lose the write. The rename IS the edit —
    dropping it because the source is an ignored temp path would leave the edit
    unrecorded, which is a worse failure than the noise it replaces."""
    root, seen = watched
    atomic_write(root / "main.py", "edited\n")
    settle(seen)

    hits = [e for e in seen if Path(e.src_path).name == "main.py"]
    assert hits, "the edit was not recorded at all"
    assert all(e.kind is EventKind.MODIFIED for e in hits), [e.kind for e in hits]
    assert hits[0].sha256 and len(hits[0].sha256) == 64


def test_no_temp_paths_reach_the_trail(watched):
    root, seen = watched
    atomic_write(root / "main.py", "edited\n")
    settle(seen)

    leaked = [str(e.src_path) for e in seen if ".tmp." in str(e.src_path)]
    leaked += [str(e.dest_path) for e in seen if e.dest_path and ".tmp." in str(e.dest_path)]
    assert leaked == [], f"scratch files leaked into the audit trail: {leaked}"


def test_one_edit_is_one_event(watched):
    """The ratio that made the feed unreadable: four rows per edit."""
    root, seen = watched
    atomic_write(root / "main.py", "edited\n")
    settle(seen)
    assert len(seen) <= 2, [f"{e.kind.value} {e.src_path}" for e in seen]


# ── real deletes must survive ───────────────────────────────────────────────
def test_a_real_delete_is_still_recorded(watched):
    """The existence check must not suppress genuine deletions — that would
    trade one silent inaccuracy for another."""
    root, seen = watched
    (root / "main.py").unlink()
    settle(seen)

    deletes = [e for e in seen if e.kind is EventKind.DELETED]
    assert deletes, "a real deletion was suppressed"
    assert Path(deletes[0].src_path).name == "main.py"


def test_ordinary_rename_is_still_a_move(watched):
    """A genuine rename between two real paths keeps MOVED — only moves out of
    ignored scratch paths are rewritten."""
    root, seen = watched
    (root / "main.py").rename(root / "renamed.py")
    settle(seen)

    moves = [e for e in seen if e.kind is EventKind.MOVED]
    assert moves, [f"{e.kind.value} {e.src_path}" for e in seen]
    assert Path(moves[0].dest_path).name == "renamed.py"


def test_plain_write_still_recorded(watched):
    """Non-atomic writes (most tools) must be unaffected by all of this."""
    root, seen = watched
    (root / "plain.txt").write_text("hello\n")
    settle(seen)

    hits = [e for e in seen if Path(e.src_path).name == "plain.txt"]
    assert hits
    assert hits[0].sha256
