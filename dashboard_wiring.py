"""What the dashboard's HTTP layer calls into.

WHY THIS EXISTS
---------------
These ten functions were closures defined inside `main()`, capturing `state`,
`settings` and `logger` from the enclosing scope. That made them unreachable
from a test: importing them meant running `main()`, which starts an observer,
a lease sweeper, an HTTP server and an SSE server.

So the wiring between "what the browser asked for" and "which project answers"
was the least tested code in the service — and the `state.primary` defect, where
every read tool reported idle while recording worked, lived exactly in that
seam. `dashboard.py` owns HTTP; this owns the question of WHICH PROJECT a
request is about; `main()` now only connects them.

The dependencies are passed in rather than imported so this module can be
exercised against a registry of fakes.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


class DashboardWiring:
    """Read and write callbacks for one running Sentinel.

    `add_watch` is injected because scheduling a directory is the observer's
    job, not this module's, and injecting it keeps watchdog out of the tests.
    """

    def __init__(self, state, settings, add_watch: Callable,
                 logger: Optional[logging.Logger] = None) -> None:
        self._state = state
        self._settings = settings
        self._add_watch = add_watch
        self._log = logger or logging.getLogger("sc.sentinel.dashboard")

    # ── which project is this request about? ─────────────────────────────────

    def resolve(self, project: Optional[str]):
        """Which watched project a dashboard request is asking about.

        The dashboard is a VIEW, so naming a project here selects what to READ
        and deliberately does not move `state.primary`. Two browser tabs must be
        able to sit on two different projects at once, and a session relying on
        the default must not have it changed by someone clicking around in a
        browser. Only pivot moves `primary`.

        Falls back to the primary so existing callers — and a dashboard opened
        before any tab is chosen — keep working unchanged.
        """
        if project:
            entry = self._state.registry.get(Path(project))
            if entry is not None:
                return entry
        return self._state._primary_entry()

    def events_for(self, entry, n: int = 200) -> List[Any]:
        """The in-RAM ring, narrowed to one project.

        The ring is shared by every watch — the handler is one object with one
        buffer — so an unfiltered feed interleaves projects and shows you edits
        from a directory you are not working in. Attribution goes through
        `registry.route`, the same longest-prefix match the writer uses, so with
        a project nested inside another the event lands in the same place the
        feed shows it. Comparing entries by identity is safe: both sides come
        out of the registry's own dict.
        """
        if entry is None:
            return []
        owned = [
            e for e in self._state.handler.recent_events(
                self._settings.max_memory_events)
            if self._state.registry.route(e.src_path) is entry
        ]
        return owned[-n:]

    # ── read ─────────────────────────────────────────────────────────────────

    def get_projects(self) -> List[Dict]:
        """The tab strip: every watched project, newest counts each poll."""
        state, settings = self._state, self._settings
        primary = str(state.primary) if state.primary else None
        return [
            {
                "key": str(e.path),
                "name": e.project_name,
                "path": str(e.path),
                "db_events": e.store.total_count() if e.store else 0,
                "live_events": len(self.events_for(e, settings.max_memory_events)),
                "is_primary": str(e.path) == primary,
                # Shown greyed rather than hidden: a project that vanished from
                # the tabs would look unwatched, when in fact its history is
                # intact and the next prompt re-arms it.
                "suspended": e.suspended,
                "idle_seconds": round(e.idle_seconds()),
            }
            for e in state.registry.entries()
        ]

    def get_stats(self, project: Optional[str] = None) -> Dict:
        state, settings = self._state, self._settings
        entry = self.resolve(project)
        alerts = state.handler.recent_alerts()
        return {
            "watch_dir": str(entry.path) if entry else None,
            "watching": state.registry.paths(),
            "project_name": entry.project_name if entry else None,
            "audit_dir": str(entry.audit_dir) if entry else None,
            "version": settings.mcp_server_version,
            "total_db_events": entry.store.total_count() if entry and entry.store else 0,
            "memory_events": len(self.events_for(entry, settings.max_memory_events)),
            "recent_alerts": len(alerts),
            "alerts_detail": alerts,
            # PROCESS-WIDE, not scoped to `project` like everything else in this
            # response. Rate alerting sits in the observer's handler, which is
            # one object shared by every watch, and it fires before an event has
            # been routed to a project — deliberately, so a runaway process is
            # caught without waiting on the hashing pool. Stated explicitly
            # because every other field here IS scoped, and an unlabelled alert
            # count inside a project's tab reads as belonging to that project.
            "alerts_scope": "process",
            # Ignore patterns are process-global, not per-project. Labelled as
            # such in the UI so a pattern added while viewing one project is not
            # mistaken for applying only to that one.
            "ignore_patterns": len(settings.ignore_patterns),
            "patterns_list": settings.ignore_patterns,
            "as_of": datetime.now(tz=timezone.utc).isoformat(),
        }

    def get_events(self, project: Optional[str] = None) -> List[Dict]:
        return [
            {
                "ts": e.timestamp.isoformat(),
                "kind": e.kind.value,
                "src_path": str(e.src_path),
                "dest_path": str(e.dest_path) if e.dest_path else None,
                "sha256": e.sha256,
            }
            for e in self.events_for(self.resolve(project))
        ]

    def get_snapshots(self, project: Optional[str] = None) -> List[str]:
        entry = self.resolve(project)
        if entry is None or entry.builder is None:
            return []
        return [a.name for a in entry.builder.list_artifacts()
                if not a.name.startswith("audit-")]

    # ── write ────────────────────────────────────────────────────────────────

    def do_pivot(self, path_str: str) -> Dict:
        """Watch another project. ADDITIVE — nothing is unwatched.

        This used to call observer.unschedule_all() and swap the single store.
        That silently stopped every other session's monitoring: the other
        session kept confirming its changes against a directory Sentinel was no
        longer looking at. Pivot now means "also watch this, and make it the
        default view".
        """
        state, settings = self._state, self._settings
        if not path_str:
            return {"status": "error", "message": "No path provided"}
        p = Path(path_str).resolve()
        if not p.exists() or not p.is_dir():
            return {"status": "error", "message": f"Not a directory: {path_str}"}
        try:
            entry = state.registry.get(p)
            if entry is None:
                entry = state.registry.add(p)
                entry.handle = self._add_watch(state.observer, state.handler,
                                               entry.path)
                self._log.info("Now also watching %s (project=%s)",
                               p, entry.project_name)

            state.primary = entry.path
            settings.update_watch_dir(p)   # keeps derived config in step

            return {
                "status": "ok",
                "new_watch_path": str(p),
                "project_name": entry.project_name,
                "audit_dir": str(entry.audit_dir),
                "watching": state.registry.paths(),
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def do_rollback(self) -> Dict:
        """Return the default view to the previously watched project.

        Now that watches are additive, this does NOT re-schedule anything in the
        common case — the previous project is usually still watched, so rollback
        only moves `primary` back. The watch is re-added only if it was
        explicitly removed in the meantime.
        """
        state, settings = self._state, self._settings
        prev = settings.rollback_watch_dir()
        if prev is None:
            return {"status": "error", "message": "No previous path"}
        try:
            prev = Path(prev).resolve()
            entry = state.registry.get(prev)
            if entry is None:
                if not prev.is_dir():
                    return {"status": "error",
                            "message": f"Previous path no longer exists: {prev}"}
                entry = state.registry.add(prev)
                entry.handle = self._add_watch(state.observer, state.handler,
                                               entry.path)
            state.primary = entry.path
            return {
                "status": "ok",
                "restored_path": str(prev),
                "project_name": entry.project_name,
                "watching": state.registry.paths(),
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def do_audit(self, label: str, mode: str,
                 project: Optional[str] = None) -> Dict:
        """Snapshot the project being VIEWED, not the primary.

        Both the tree walked and the events captured come from `entry`, so a
        snapshot taken from a project's tab describes that project.
        """
        entry = self.resolve(project)
        if entry is None or entry.builder is None:
            return {"status": "error", "message": "No project selected"}
        if mode == "disk":
            snap = entry.builder.build_disk_snapshot(label)
        else:
            snap = entry.builder.build_snapshot(self.events_for(entry), label)
        return {
            "status": "ok",
            "snapshot_uri": snap.name,
            "project_name": entry.project_name,
        }

    def do_ignore(self, pattern: str) -> Dict:
        if not pattern:
            return {"status": "error", "message": "No pattern"}
        self._settings.add_ignore_pattern(pattern)
        return {"status": "ok", "pattern": pattern}

    # ── the argument list dashboard.start_dashboard expects ──────────────────

    def as_callbacks(self) -> tuple:
        """Positional callbacks, in `start_dashboard`'s parameter order."""
        return (
            self.get_stats, self.get_events, self.get_snapshots,
            self.get_projects, self.do_pivot, self.do_rollback,
            self.do_audit, self.do_ignore,
        )
