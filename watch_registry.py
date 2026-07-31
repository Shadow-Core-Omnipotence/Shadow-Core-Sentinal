"""Multiple watched projects at once, and routing events to the right one.

WHY THIS EXISTS
---------------
Sentinel watched exactly one directory. Changing it went through `pivot_room`,
which calls `observer.unschedule_all()` and then schedules the new path — a
DESTRUCTIVE swap.

That breaks the moment two Claude sessions work on different projects against
one shared Sentinel. Session B pivots; session A's monitoring stops with no
error, and session A carries on confirming its changes against a project it is
no longer watching. A verification tool reporting confidently about the wrong
directory is worse than one that is simply switched off.

So watches are ADDITIVE here. Adding one never removes another, and removal is
always explicit — a session must never silently unwatch a directory another
session depends on.

The storage layout does not change. Each watched project keeps its own
`<base_audit_dir>/<ProjectName>/sentinel.db`, exactly as before, so existing
audit history stays valid and the dashboard reads it unchanged.

ROUTING
-------
The event handler emits `AuditEvent`s with no idea which watch produced them —
previously it did not need to know, because there was only one store. With
several watches, every event must be attributed to the right project, which is
a LONGEST-PREFIX match on the path: watching both `C:\\work` and
`C:\\work\\api` must send an event under `api` to `api`, not to `work`.

Dependencies are injected (`make_store`, `make_builder`) so this module can be
tested without SQLite, watchdog, or touching a disk.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

logger = logging.getLogger("sc.sentinel.watch")


def safe_project_name(path: Path) -> str:
    """Directory name for a watched path's audit folder.

    Mirrors config._safe_dir_name so a project keeps the SAME audit directory
    it had under the single-watch design. Changing this would orphan every
    existing sentinel.db.
    """
    import re

    name = path.name or path.drive.replace(":", "").replace("\\", "")
    return re.sub(r"[^\w\-]", "-", name).strip("-") or "default"


@dataclass
class WatchEntry:
    """One watched project: where it is, where its audit data goes."""

    path: Path
    project_name: str
    audit_dir: Path
    store: object = None
    builder: object = None
    # watchdog's ObservedWatch handle, needed to unschedule precisely rather
    # than calling unschedule_all and taking every other watch down with it.
    handle: object = None
    _key: str = field(default="", repr=False)


class WatchRegistry:
    """The set of currently watched projects.

    Thread-safe: watchdog delivers events from its own thread while MCP tools
    add and remove watches from another.
    """

    def __init__(
        self,
        base_audit_dir: Path,
        make_store: Callable[[Path, str], object],
        make_builder: Callable[[Path], object],
    ) -> None:
        self._base = Path(base_audit_dir)
        self._make_store = make_store
        self._make_builder = make_builder
        self._entries: Dict[str, WatchEntry] = {}
        self._lock = threading.RLock()

    # ── keys ────────────────────────────────────────────────────────────────
    @staticmethod
    def _key_for(path: Path) -> str:
        """Case-insensitive on Windows, where C:\\Work and C:\\work are one
        directory. Adding both must not create two watches over one tree."""
        return str(path).rstrip("\\/").casefold()

    # ── mutation ────────────────────────────────────────────────────────────
    def add(self, path: Path) -> WatchEntry:
        """Register a watch. Idempotent — re-adding returns the existing entry.

        Idempotence matters because a session may call this on every startup
        without knowing whether another session already did.
        """
        path = Path(path).resolve()
        key = self._key_for(path)

        with self._lock:
            existing = self._entries.get(key)
            if existing is not None:
                return existing

            project = safe_project_name(path)
            audit_dir = self._base / project
            audit_dir.mkdir(parents=True, exist_ok=True)

            entry = WatchEntry(
                path=path,
                project_name=project,
                audit_dir=audit_dir,
                store=self._make_store(audit_dir / "sentinel.db", str(path)),
                builder=self._make_builder(audit_dir),
                _key=key,
            )
            self._entries[key] = entry
            logger.info("Watching %s (project=%s)", path, project)
            return entry

    def remove(self, path: Path) -> Optional[WatchEntry]:
        """Unregister a watch and close its store. None if it was not watched.

        Never called implicitly. Another session may be relying on this watch,
        and there is no way to know from here.
        """
        key = self._key_for(Path(path).resolve())
        with self._lock:
            entry = self._entries.pop(key, None)

        if entry is None:
            return None

        close = getattr(entry.store, "close", None)
        if callable(close):
            try:
                close()
            except Exception as exc:  # noqa: BLE001 — a bad close must not
                logger.warning("Error closing store for %s: %s", entry.path, exc)
        logger.info("Stopped watching %s", entry.path)
        return entry

    def close_all(self) -> None:
        with self._lock:
            entries = list(self._entries.values())
            self._entries.clear()
        for entry in entries:
            close = getattr(entry.store, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001 — shutdown is best effort
                    pass

    # ── lookup ──────────────────────────────────────────────────────────────
    def get(self, path: Path) -> Optional[WatchEntry]:
        with self._lock:
            return self._entries.get(self._key_for(Path(path).resolve()))

    def entries(self) -> List[WatchEntry]:
        with self._lock:
            return sorted(self._entries.values(), key=lambda e: str(e.path).casefold())

    def paths(self) -> List[str]:
        return [str(e.path) for e in self.entries()]

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    # ── routing ─────────────────────────────────────────────────────────────
    def route(self, event_path) -> Optional[WatchEntry]:
        """Which watch owns this path? Longest prefix wins.

        With `C:\\work` and `C:\\work\\api` both watched, a file under `api`
        belongs to `api`. Shortest-prefix or first-match would file every
        nested project's events under its parent.

        Compared component-wise, not as strings: a plain startswith would
        match `C:\\work-old` against a `C:\\work` watch.
        """
        try:
            target = Path(event_path).resolve()
        except (OSError, ValueError):
            return None

        target_parts = [p.casefold() for p in target.parts]

        best: Optional[WatchEntry] = None
        best_depth = -1

        with self._lock:
            candidates = list(self._entries.values())

        for entry in candidates:
            root_parts = [p.casefold() for p in entry.path.parts]
            depth = len(root_parts)
            if depth <= best_depth:
                continue
            if target_parts[:depth] == root_parts:
                best = entry
                best_depth = depth

        return best
