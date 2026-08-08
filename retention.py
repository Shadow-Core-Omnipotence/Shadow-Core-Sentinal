"""What happens to recorded audit data when Sentinel restarts.

FLUSH ON START
--------------
Sentinel already discards everything that carries across a restart EXCEPT the
recorded data: it boots watching nothing, and its in-RAM ring starts empty.
Flushing completes that — a new run begins with no history at all.

The cost is explicit and worth stating in the code that implements it: with
this enabled, `list_audit_dates`, `get_daily_report` and `query_events` can
only ever answer about the CURRENT run. Cross-restart forensics — "what changed
while I wasn't looking", the one question git cannot answer — is gone, because
the evidence for it is deleted before it can be asked about. Gap reconstruction
still works, but only across a suspend/resume inside one run.

That is a deliberate trade for bounded disk: the trail had grown to 252 MB with
no retention policy of any kind, 38% of it a dead project created by a typo.

DELETING IS THE DANGEROUS PART
------------------------------
This removes directories at a path that comes from an environment variable. If
`AUDIT_DIR` is ever mis-set — to a repo root, a home directory, a drive — a
naive implementation would delete whatever it found there. So nothing is
removed unless it LOOKS LIKE audit output: a directory holding a sentinel.db or
Sentinel's own markdown artifacts, or an empty directory Sentinel itself
created. Anything else is left alone and logged, because refusing to delete an
unrecognised file is always recoverable and deleting one is not.
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Iterable, Optional

logger = logging.getLogger("sc.sentinel.retention")

# A directory is Sentinel's if it holds one of these.
_AUDIT_MARKERS = ("sentinel.db", "audit-*.md", "snapshot-*.md", "gap-*.md")

# Loose files Sentinel writes at the top of the audit root.
#
# The markdown patterns matter: under the ORIGINAL single-watch layout the
# daily logs and snapshots were written straight into the audit root rather
# than a per-project subdirectory, and 13 of them were still sitting there.
# Without these, a "flush everything" would quietly leave them behind forever.
# The -wal/-shm sidecars appear because the store now runs in WAL mode.
_LOOSE_PATTERNS = (
    "sentinel.log", "sentinel.stdio.log",
    "sentinel.db", "sentinel.db-journal", "sentinel.db-wal", "sentinel.db-shm",
    "audit-*.md", "snapshot-*.md", "gap-*.md",
)

# An audit root shallower than this is refused outright. `C:\` has 1 part and
# `C:\audit` has 2; anything that short is far more likely to be a
# misconfiguration than a real audit directory.
_MIN_PATH_PARTS = 3


def _looks_like_audit_dir(path: Path) -> bool:
    """True if this directory holds Sentinel's own output, or is empty.

    An empty directory is included because Sentinel creates one per project on
    every pivot, whether or not that project ever records anything — there were
    14 such empty directories at the time this was written.
    """
    if not path.is_dir():
        return False
    try:
        entries = list(path.iterdir())
    except OSError:
        return False
    if not entries:
        return True
    for marker in _AUDIT_MARKERS:
        if any(path.glob(marker)):
            return True
    return False


def flush_audit_data(
    base_audit_dir: Path,
    keep: Iterable[str] = (),
    dry_run: bool = False,
) -> dict:
    """Delete recorded audit data under `base_audit_dir`.

    Args:
        base_audit_dir: The audit root. Only its direct children are touched.
        keep: Project directory names to preserve.
        dry_run: Report what would go without removing anything.

    Returns a summary: removed, skipped, bytes_freed, errors.
    """
    root = Path(base_audit_dir)
    summary = {"removed": [], "skipped": [], "bytes_freed": 0, "errors": []}

    try:
        root = root.resolve()
    except OSError as exc:
        summary["errors"].append(f"cannot resolve audit root: {exc}")
        return summary

    if not root.is_dir():
        return summary

    # ── refuse an implausible root ───────────────────────────────────────────
    if len(root.parts) < _MIN_PATH_PARTS:
        msg = (f"Refusing to flush {root}: too close to a filesystem root. "
               f"Check AUDIT_DIR.")
        logger.error(msg)
        summary["errors"].append(msg)
        return summary

    keep_set = {k.casefold() for k in keep}

    for child in sorted(root.iterdir()):
        if child.name.casefold() in keep_set:
            summary["skipped"].append(f"{child.name} (kept)")
            continue

        if child.is_dir():
            if not _looks_like_audit_dir(child):
                # Not ours. Say so rather than guessing — an unrecognised
                # directory under the audit root means the root is probably
                # not what it was thought to be.
                logger.warning(
                    "Not audit output, leaving alone: %s", child)
                summary["skipped"].append(f"{child.name} (unrecognised)")
                continue
            size = _dir_size(child)
            if dry_run:
                summary["removed"].append(child.name)
                summary["bytes_freed"] += size
                continue
            try:
                shutil.rmtree(child)
                summary["removed"].append(child.name)
                summary["bytes_freed"] += size
            except OSError as exc:
                logger.warning("Could not remove %s: %s", child, exc)
                summary["errors"].append(f"{child.name}: {exc}")

        elif child.is_file():
            if not any(child.match(p) for p in _LOOSE_PATTERNS):
                summary["skipped"].append(f"{child.name} (unrecognised)")
                continue
            size = child.stat().st_size
            if dry_run:
                summary["removed"].append(child.name)
                summary["bytes_freed"] += size
                continue
            try:
                child.unlink()
                summary["removed"].append(child.name)
                summary["bytes_freed"] += size
            except OSError as exc:
                summary["errors"].append(f"{child.name}: {exc}")

    return summary


def _dir_size(path: Path) -> int:
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            pass
    return total


def flush_if_configured(settings, keep: Optional[Iterable[str]] = None) -> dict:
    """Run the startup flush if enabled, and log the result.

    Called before logging is configured — Sentinel's own log lives inside the
    directory being flushed, so opening it first would mean deleting a file
    already held open. The summary is logged once logging is up.
    """
    if not getattr(settings, "flush_on_start", False):
        return {"removed": [], "skipped": [], "bytes_freed": 0, "errors": []}
    return flush_audit_data(settings.base_audit_dir, keep=keep or ())
