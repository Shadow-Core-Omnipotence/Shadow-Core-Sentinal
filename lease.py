"""Idle watches suspend themselves, and the gap that creates is recorded.

WHY THIS EXISTS
---------------
Watches were additive and never expired. Sentinel starts with the machine and
runs until reboot, so every project any session ever touched stayed scheduled —
each one a live recursive watchdog subscription and an open SQLite handle — long
after the work on it stopped. Watching six projects because you visited six
projects last Tuesday is not monitoring, it is accumulation.

A LEASE, NOT A REAPER
---------------------
`watch_registry` argues that removal must always be explicit, because silently
unwatching a directory leaves a session confirming its work against something
nobody is recording. That argument is sound and this module does not weaken it:
nothing here REMOVES a watch. A watch is SUSPENDED — unscheduled from the
observer, while its entry, its store and its history all stay exactly where they
were — and any of these renews it:

  * a filesystem event routed to it (so a long working session never expires
    mid-flight, even one where the user is quiet for an hour),
  * a `/api/touch` from the UserPromptSubmit hook (the user typing in that
    project's conversation),
  * any session calling watch_project on it again.

Because resume is automatic and immediate, a wrong suspension is cheap: the
worst case is that monitoring lapsed over a stretch where nothing happened, and
the next prompt re-arms it before the first tool call runs. That is what makes a
timeout acceptable here when a plain reaper would not be.

THE GAP IS THE POINT
--------------------
A suspended watch does not see git pulls, branch switches, or another editor.
If resume just quietly re-armed, the trail would have an invisible hole and
Sentinel would once again be confidently wrong about a project — the single
failure this codebase is built to avoid.

So suspension records a SHA-256 map of the tree, resume recomputes it, and the
difference is written into the audit trail as real events plus a report naming
the window they happened in. Changes made while Sentinel was not looking are
therefore VISIBLE and marked as reconstructed, rather than absent.
"""
from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from hasher import sha256_of

logger = logging.getLogger("sc.sentinel.lease")

# relative posix path -> sha256 (None when the file could not be read)
DiskState = Dict[str, Optional[str]]


def scan_tree(root: Path, is_ignored: Callable[[Path], bool]) -> DiskState:
    """SHA-256 of every non-ignored file under `root`, keyed by relative path.

    Keys are relative and posix-separated so a state map stays comparable even
    if the project is later reached by a different absolute spelling.
    """
    state: DiskState = {}
    root = Path(root)
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        dirnames[:] = [d for d in dirnames if not is_ignored(here / d)]
        for name in filenames:
            fpath = here / name
            if is_ignored(fpath):
                continue
            try:
                rel = fpath.relative_to(root).as_posix()
            except ValueError:
                continue
            state[rel] = sha256_of(fpath)
    return state


def diff_states(before: DiskState, after: DiskState) -> Tuple[List[str], List[str], List[str]]:
    """(added, removed, modified) between two scans.

    A file whose hash could not be read on either side is not reported as
    modified — an unreadable file is unknown, not changed, and inventing a
    change would be exactly the false confidence this exists to prevent.
    """
    before_keys, after_keys = set(before), set(after)
    added = sorted(after_keys - before_keys)
    removed = sorted(before_keys - after_keys)
    modified = sorted(
        k for k in before_keys & after_keys
        if before[k] is not None and after[k] is not None and before[k] != after[k]
    )
    return added, removed, modified


class LeaseSweeper:
    """Background thread that suspends watches idle longer than `ttl_seconds`.

    `suspend` is injected rather than called directly so this module never
    touches the observer or the stores; main.py owns that wiring.
    """

    def __init__(
        self,
        registry,
        suspend: Callable[[object], None],
        ttl_seconds: int,
        interval_seconds: int = 60,
    ) -> None:
        self._registry = registry
        self._suspend = suspend
        self._ttl = ttl_seconds
        self._interval = max(5, interval_seconds)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> Optional[threading.Thread]:
        if self._ttl <= 0:
            logger.info("Idle suspension disabled (ttl=%s)", self._ttl)
            return None
        self._thread = threading.Thread(
            target=self._run, name="lease-sweeper", daemon=True)
        self._thread.start()
        logger.info(
            "Idle suspension armed: watches idle > %ss are suspended (checked every %ss)",
            self._ttl, self._interval,
        )
        return self._thread

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self._sweep()
            except Exception as exc:  # noqa: BLE001 — a bad sweep must never
                # kill the thread; monitoring would stop expiring silently.
                logger.warning("Lease sweep error: %s", exc)

    def _sweep(self) -> None:
        for entry in self._registry.idle_entries(self._ttl):
            logger.info(
                "Suspending %s — idle %.0fs", entry.path, entry.idle_seconds())
            try:
                self._suspend(entry)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to suspend %s: %s", entry.path, exc)


class WatchLifecycle:
    """Suspends and resumes watches, and reconstructs what was missed.

    Lives here rather than inline in `main()` so the gap handling — the part
    that decides whether the audit trail tells the truth about a suspension —
    can be tested against a real observer instead of only being read.

    The observer calls are injected so this stays testable without watchdog.
    """

    def __init__(self, observer, handler, is_ignored, add_watch, remove_watch) -> None:
        self._observer = observer
        self._handler = handler
        self._is_ignored = is_ignored
        self._add_watch = add_watch
        self._remove_watch = remove_watch

    def suspend(self, entry) -> None:
        """Unschedule an idle watch, keeping everything else about it.

        The tree is inventoried FIRST: the diff at resume is only as honest as
        the baseline it starts from, and scanning after unscheduling would miss
        anything written in between.
        """
        try:
            entry.gap_state = scan_tree(entry.path, self._is_ignored)
        except OSError as exc:
            # A project on a disconnected drive cannot be inventoried. Suspend
            # anyway, but with no baseline — resume then reports the gap as
            # unmeasurable rather than diffing against an empty tree and
            # claiming every file was deleted.
            logger.warning("Could not inventory %s before suspend: %s", entry.path, exc)
            entry.gap_state = None
        self._remove_watch(self._observer, entry.handle)
        entry.handle = None
        entry.suspended = True
        entry.suspended_at = datetime.now(tz=timezone.utc)
        logger.info(
            "Suspended %s (idle) — resumes on next prompt or watch_project", entry.path)

    def resume(self, entry, reason: str = "activity") -> bool:
        """Re-arm a suspended watch and record what was missed.

        Order matters: the observer is scheduled BEFORE the tree is rescanned,
        so a change landing during the scan is double-counted rather than lost.
        Duplicate evidence is recoverable; a hole in the trail is not.
        """
        if not entry.suspended:
            entry.touch()
            return False

        entry.handle = self._add_watch(self._observer, self._handler, entry.path)
        entry.suspended = False
        entry.touch()
        before, entry.gap_state = entry.gap_state, None
        suspended_at, entry.suspended_at = entry.suspended_at, None
        logger.info("Resumed %s (%s)", entry.path, reason)

        if before is None or suspended_at is None:
            return True
        try:
            self.record_gap(entry, before, suspended_at)
        except Exception as exc:  # noqa: BLE001 — monitoring is live again
            # either way; failing to describe the gap must not undo the resume.
            logger.warning("Gap reconstruction failed for %s: %s", entry.path, exc)
        return True

    def record_gap(self, entry, before: DiskState, suspended_at: datetime) -> dict:
        """Write what changed while the watch was down into the audit trail.

        Reconstructed events carry DETECTION time, not the time the edit
        happened — that information does not exist, and inventing it would make
        the trail lie about when. The gap report states the real window.
        """
        from models import AuditEvent, EventKind

        after = scan_tree(entry.path, self._is_ignored)
        added, removed, modified = diff_states(before, after)
        resumed_at = datetime.now(tz=timezone.utc)

        report = gap_report(entry.project_name, entry.path, suspended_at,
                            resumed_at, added, removed, modified)
        name = f"gap-{resumed_at.strftime('%Y%m%dT%H%M%SZ')}.md"
        report_path = Path(entry.audit_dir) / name
        report_path.write_text(report, encoding="utf-8")

        for kind, paths in ((EventKind.CREATED, added),
                            (EventKind.MODIFIED, modified),
                            (EventKind.DELETED, removed)):
            for rel in paths:
                evt = AuditEvent(
                    kind=kind,
                    src_path=Path(entry.path) / rel,
                    timestamp=resumed_at,
                    sha256=after.get(rel),
                )
                entry.store.insert(evt)
                entry.builder.append_event(evt)

        total = len(added) + len(removed) + len(modified)
        if total:
            logger.info("Recorded %d change(s) missed while %s was suspended → %s",
                        total, entry.project_name, name)
        return {"added": added, "removed": removed, "modified": modified,
                "report": report_path}


def gap_report(
    project_name: str,
    watch_path: Path,
    suspended_at: datetime,
    resumed_at: datetime,
    added: Iterable[str],
    removed: Iterable[str],
    modified: Iterable[str],
) -> str:
    """Markdown describing what changed while Sentinel was not watching."""
    added, removed, modified = list(added), list(removed), list(modified)
    total = len(added) + len(removed) + len(modified)
    gap_min = (resumed_at - suspended_at).total_seconds() / 60.0

    lines = [
        f"# Monitoring Gap — {project_name}",
        "",
        "> These changes were **not observed live**. Sentinel was suspended for",
        "> inactivity and reconstructed them by comparing a SHA-256 inventory",
        "> taken at suspension against one taken at resume. Timestamps below are",
        "> DETECTION time, not the time the edits actually happened.",
        "",
        f"> **Project:** `{watch_path}`",
        f"> **Suspended:** {suspended_at.isoformat(timespec='seconds')}",
        f"> **Resumed:** {resumed_at.isoformat(timespec='seconds')}",
        f"> **Gap:** {gap_min:.1f} minutes",
        f"> **Changes reconstructed:** {total}",
        "",
    ]
    if not total:
        lines += ["## No changes", "", "The tree was identical at resume.", ""]
        return "\n".join(lines)

    for title, items in (("Added", added), ("Modified", modified), ("Removed", removed)):
        if not items:
            continue
        lines += [f"## {title} ({len(items)})", ""]
        lines += [f"- `{p}`" for p in items]
        lines += [""]
    return "\n".join(lines)
