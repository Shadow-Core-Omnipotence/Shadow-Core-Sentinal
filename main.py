"""
main.py — Shadow-Core Sentinel entry point.

Post-audit refactor (2026-05-17):
- TASK-S03 — Removed mixed `mcp.server.sse` + `fastmcp` libraries. Now pure
  fastmcp end-to-end. `mcp.run(transport="sse", host, port)` handles the HTTP
  server; no manual SseServerTransport/Starlette/uvicorn wiring.
- TASK-S04 — `/health` custom route exposes per-component state for monitoring.
- TASK-S05 — `/admin/shutdown` (localhost-only) lets scripts trigger a clean
  shutdown without needing elevated `taskkill`.
- TASK-S12 — Replaced `store_ref = [...]` / `builder_ref = [...]` mutable-list
  pattern with a `SentinelState` dataclass. Side effect: fixes a latent bug
  where MCP tool closures captured the original store/builder and never saw
  pivot/rollback swaps.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse

from config import settings
from dashboard import start_dashboard
from lease import LeaseSweeper, WatchLifecycle
from mcp_server import build_mcp_server
from observer import AuditEventHandler, add_watch, remove_watch, start_bare_observer
from watch_registry import WatchRegistry
from report_builder import ReportBuilder
from storage import EventStore


# TASK-S12 — Single mutable state object. Replaces the _ref = [obj] pattern.
@dataclass
class SentinelState:
    handler: AuditEventHandler
    registry: Any = None          # WatchRegistry — the watched projects
    observer: Any = None          # set after the observer starts
    # The project that read-side callers mean when they do not name one. Every
    # project is watched simultaneously; this only decides the default view.
    primary: Any = None           # Path
    # Set by main(): resume(entry, reason) -> bool. Lets the MCP tools and the
    # touch route re-arm a suspended watch without importing the observer
    # wiring. See lease.py.
    resume: Any = None
    lifecycle: Any = None         # WatchLifecycle — suspend/resume + gap
    sweeper: Any = None           # LeaseSweeper

    # `store` and `builder` used to be plain fields swapped by pivot_room. They
    # now resolve through the registry so the dashboard, the MCP tools and the
    # /health route keep working unchanged while several projects are watched.
    # None when idle. Sentinel boots watching nothing, so every read-side
    # caller must cope with there being no current project at all — that is the
    # normal state between sessions, not an error.
    def _primary_entry(self):
        if not self.registry or self.primary is None:
            return None
        return self.registry.get(self.primary)

    @property
    def store(self):
        entry = self._primary_entry()
        return entry.store if entry else None

    @property
    def builder(self):
        entry = self._primary_entry()
        return entry.builder if entry else None


def parse_args():
    parser = argparse.ArgumentParser(description="Shadow-Core Sentinel MCP Server")
    parser.add_argument("--watch", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--no-dashboard", action="store_true")
    parser.add_argument("--log-level", default=None,
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser.parse_args()


def main():
    args = parse_args()

    if args.watch:
        settings.update_watch_dir(Path(args.watch))
    if args.port:
        settings.dashboard_port = args.port
    if args.no_dashboard:
        settings.dashboard_enabled = False
    if args.log_level:
        settings.log_level = args.log_level

    # TASK-S02 — File-based logging so failures are visible even when launched hidden.
    log_path = settings.audit_dir / "sentinel.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    # TASK-S03 follow-up — When launched via `Start-Process -WindowStyle Hidden`,
    # the exe has no console; native stdout/stderr writes (uvicorn, FastMCP banner,
    # print() calls) DEADLOCK on the pipe. Redirect Python-level sys.stdout/stderr
    # to the log file so nothing blocks. This must happen BEFORE mcp.run() —
    # otherwise FastMCP/uvicorn hang during HTTP server startup.
    _stdio_log = open(log_path.with_suffix(".stdio.log"), "a", encoding="utf-8", buffering=1)
    sys.stdout = _stdio_log
    sys.stderr = _stdio_log

    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(sys.stderr),  # now safely the file
        ],
    )
    logger = logging.getLogger("sentinel")
    logger.info(f"Sentinel v{settings.mcp_server_version} starting")
    logger.info(f"Watching: {settings.watch_dir}")
    logger.info(f"Project: {settings.project_name}")
    logger.info(f"Audit dir: {settings.audit_dir}")

    # TASK-S12 — Build state object; closures and tools reference state.* so
    # pivots/rollbacks that swap fields are visible everywhere.
    registry = WatchRegistry(
        base_audit_dir=settings.base_audit_dir,
        make_store=EventStore,
        make_builder=ReportBuilder,
    )

    state = SentinelState(
        handler=AuditEventHandler(),
        registry=registry,
        primary=None,          # nothing watched until asked — see below
    )

    # Every event is ROUTED to the project that owns its path, rather than
    # going to one global store. With several watches, an event filed under the
    # wrong project silently corrupts that project's audit trail — which is the
    # one thing this service exists to be trusted about.
    def _record(event) -> None:
        entry = state.registry.route(event.src_path)
        if entry is None:
            # Outside every watch. Reachable when a watch is removed while its
            # events are still in flight through the hashing pool.
            logger.debug("Event outside all watches, dropped: %s", event.src_path)
            return
        # Activity renews the lease, so a project being actively worked on never
        # suspends — including during a long stretch where the user says nothing.
        entry.touch()
        entry.store.insert(event)
        entry.builder.append_event(event)

    state.handler.subscribe(_record)


    # IDLE ON BOOT. Sentinel starts with the PC and watches NOTHING until a
    # session calls watch_project.
    #
    # It used to schedule whatever directory was watched last, which meant the
    # service came up recording a project nobody had asked about — the running
    # instance was found watching Shadow-Core Engineer purely because that was
    # the last pivot, quietly accumulating events into an audit trail no one
    # was reading. Idling also means no CPU spent hashing, and no growth in
    # audit_logs, until monitoring is actually wanted.
    #
    # Watches are deliberately NOT persisted across restarts: "idle on boot"
    # would be a lie if the previous session's watches came back.
    state.observer = start_bare_observer()
    logger.info(
        "File observer started — IDLE, watching nothing. "
        "Call the watch_project tool to begin monitoring a directory."
    )

    # ── idle suspension ─────────────────────────────────────────────────────
    # Suspend/resume and the gap reconstruction live in lease.py so they can be
    # tested against a real observer rather than only read here.
    state.lifecycle = WatchLifecycle(
        observer=state.observer,
        handler=state.handler,
        is_ignored=settings.is_ignored,
        add_watch=add_watch,
        remove_watch=remove_watch,
    )
    state.resume = state.lifecycle.resume

    state.sweeper = LeaseSweeper(
        registry=state.registry,
        suspend=state.lifecycle.suspend,
        ttl_seconds=settings.watch_idle_ttl_seconds,
        interval_seconds=settings.watch_sweep_seconds,
    )
    state.sweeper.start()

    if settings.dashboard_enabled:

        def _resolve(project: str | None):
            """Which watched project a dashboard request is asking about.

            The dashboard is a VIEW, so naming a project here selects what to
            READ and deliberately does not move `state.primary`. Two browser
            tabs must be able to sit on two different projects at once, and a
            session relying on the default must not have it changed by someone
            clicking around in a browser. Only pivot moves `primary`.

            Falls back to the primary so existing callers — and a dashboard
            opened before any tab is chosen — keep working unchanged.
            """
            if project:
                entry = state.registry.get(Path(project))
                if entry is not None:
                    return entry
            return state._primary_entry()

        def _events_for(entry, n: int = 200):
            """The in-RAM ring, narrowed to one project.

            The ring is shared by every watch — the handler is one object with
            one buffer — so an unfiltered feed interleaves projects and shows
            you edits from a directory you are not working in. Attribution goes
            through `registry.route`, the same longest-prefix match the writer
            uses, so with a project nested inside another the event lands in the
            same place the feed shows it. Comparing entries by identity is safe:
            both sides come out of the registry's own dict.
            """
            if entry is None:
                return []
            owned = [
                e for e in state.handler.recent_events(settings.max_memory_events)
                if state.registry.route(e.src_path) is entry
            ]
            return owned[-n:]

        def get_projects():
            """The tab strip: every watched project, newest counts each poll."""
            primary = str(state.primary) if state.primary else None
            return [
                {
                    "key": str(e.path),
                    "name": e.project_name,
                    "path": str(e.path),
                    "db_events": e.store.total_count() if e.store else 0,
                    "live_events": len(_events_for(e, settings.max_memory_events)),
                    "is_primary": str(e.path) == primary,
                    # Shown greyed rather than hidden: a project that vanished
                    # from the tabs would look unwatched, when in fact its
                    # history is intact and the next prompt re-arms it.
                    "suspended": e.suspended,
                    "idle_seconds": round(e.idle_seconds()),
                }
                for e in state.registry.entries()
            ]

        def get_stats(project: str | None = None):
            entry = _resolve(project)
            alerts = state.handler.recent_alerts()
            return {
                "watch_dir": str(entry.path) if entry else None,
                "watching": state.registry.paths(),
                "project_name": entry.project_name if entry else None,
                "audit_dir": str(entry.audit_dir) if entry else None,
                "version": settings.mcp_server_version,
                "total_db_events": entry.store.total_count() if entry and entry.store else 0,
                "memory_events": len(_events_for(entry, settings.max_memory_events)),
                "recent_alerts": len(alerts),
                "alerts_detail": alerts,
                # Ignore patterns are process-global, not per-project. Labelled
                # as such in the UI so a pattern added while viewing one project
                # is not mistaken for applying only to that one.
                "ignore_patterns": len(settings.ignore_patterns),
                "patterns_list": settings.ignore_patterns,
                "as_of": datetime.now(tz=timezone.utc).isoformat(),
            }

        def get_events(project: str | None = None):
            return [
                {
                    "ts": e.timestamp.isoformat(),
                    "kind": e.kind.value,
                    "src_path": str(e.src_path),
                    "dest_path": str(e.dest_path) if e.dest_path else None,
                    "sha256": e.sha256,
                }
                for e in _events_for(_resolve(project))
            ]

        def get_snapshots(project: str | None = None):
            entry = _resolve(project)
            if entry is None or entry.builder is None:
                return []
            return [a.name for a in entry.builder.list_artifacts()
                    if not a.name.startswith("audit-")]

        def do_pivot(path_str: str) -> dict:
            """Watch another project. ADDITIVE — nothing is unwatched.

            This used to call observer.unschedule_all() and swap the single
            store. That silently stopped every other session's monitoring: the
            other session kept confirming its changes against a directory
            Sentinel was no longer looking at. Pivot now means "also watch
            this, and make it the default view".
            """
            if not path_str:
                return {"status": "error", "message": "No path provided"}
            p = Path(path_str).resolve()
            if not p.exists() or not p.is_dir():
                return {"status": "error", "message": f"Not a directory: {path_str}"}
            try:
                entry = state.registry.get(p)
                if entry is None:
                    entry = state.registry.add(p)
                    entry.handle = add_watch(state.observer, state.handler, entry.path)
                    logger.info("Now also watching %s (project=%s)", p, entry.project_name)

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

        def do_rollback() -> dict:
            """Return the default view to the previously watched project.

            Now that watches are additive, this does NOT re-schedule anything in
            the common case — the previous project is usually still watched, so
            rollback only moves `primary` back. The watch is re-added only if it
            was explicitly removed in the meantime.
            """
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
                    entry.handle = add_watch(state.observer, state.handler, entry.path)
                state.primary = entry.path
                return {
                    "status": "ok",
                    "restored_path": str(prev),
                    "project_name": entry.project_name,
                    "watching": state.registry.paths(),
                }
            except Exception as e:
                return {"status": "error", "message": str(e)}

        def do_audit(label: str, mode: str, project: str | None = None) -> dict:
            """Snapshot the project being VIEWED, not the primary.

            Both the tree walked and the events captured come from `entry`, so
            a snapshot taken from a project's tab describes that project.
            """
            entry = _resolve(project)
            if entry is None or entry.builder is None:
                return {"status": "error", "message": "No project selected"}
            if mode == "disk":
                snap = entry.builder.build_disk_snapshot(label)
            else:
                snap = entry.builder.build_snapshot(_events_for(entry), label)
            return {
                "status": "ok",
                "snapshot_uri": snap.name,
                "project_name": entry.project_name,
            }

        def do_ignore(pattern: str) -> dict:
            if not pattern:
                return {"status": "error", "message": "No pattern"}
            settings.add_ignore_pattern(pattern)
            return {"status": "ok", "pattern": pattern}

        start_dashboard(
            settings.dashboard_port,
            get_stats, get_events, get_snapshots, get_projects,
            do_pivot, do_rollback, do_audit, do_ignore,
        )
        logger.info(f"Dashboard → http://127.0.0.1:{settings.dashboard_port}")

    # TASK-S03 — Wire MCP tools via FastMCP (no more raw SseServerTransport).
    mcp_server = build_mcp_server(state)

    # TASK-S04 — /health route on the same FastMCP HTTP server (port 7702).
    @mcp_server.custom_route("/health", methods=["GET"])
    async def health(request: Request) -> JSONResponse:
        observer_alive = bool(state.observer and state.observer.is_alive())
        return JSONResponse({
            "sentinel": "ok",
            "version": settings.mcp_server_version,
            "observer": "ok" if observer_alive else "down",
            "dashboard": "ok" if settings.dashboard_enabled else "disabled",
            "watch_dir": str(state.primary),
            "watching": state.registry.paths(),
            "watch_count": len(state.registry),
            "suspended": [str(e.path) for e in state.registry.suspended_entries()],
            "total_events": state.store.total_count() if state.store else 0,
            "as_of": datetime.now(timezone.utc).isoformat(),
        })

    # The keepalive the UserPromptSubmit hook calls. Renews the lease for the
    # project the prompt was typed in, and resumes it if it had gone idle — so
    # a suspended watch is back up before the session's first tool call.
    #
    # Lives on the MCP port rather than the dashboard's because the dashboard
    # can be disabled, and a hook that silently stops working would let projects
    # suspend with nothing able to wake them.
    @mcp_server.custom_route("/api/touch", methods=["POST"])
    async def touch(request: Request) -> JSONResponse:
        client_host = request.client.host if request.client else None
        if client_host not in ("127.0.0.1", "::1", "localhost"):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        # JSON or form-encoded. The hook is a one-line shell command, and
        # hand-building JSON around a Windows path there means escaping
        # backslashes in the shell — fragile, and it fails silently by posting
        # a malformed body that parses to {}. `curl --data-urlencode` does the
        # quoting correctly for any path, so form bodies are accepted too.
        raw = ""
        try:
            body = await request.json()
            raw = (body or {}).get("path") or ""
        except Exception:  # noqa: BLE001 — not JSON, try a form body
            try:
                form = await request.form()
                raw = form.get("path") or ""
            except Exception:  # noqa: BLE001 — neither; treated as no path
                raw = ""
        if not raw:
            return JSONResponse({"status": "error", "message": "No path provided"},
                                status_code=400)
        try:
            p = Path(raw).expanduser().resolve()
        except OSError as exc:
            return JSONResponse({"status": "error", "message": f"Bad path: {exc}"},
                                status_code=400)

        # Longest-prefix, not exact match: a session's cwd is often a
        # subdirectory of the watched root, and an exact-match keepalive would
        # let the project suspend underneath an actively-used session.
        entry = state.registry.route(p) or state.registry.get(p)
        if entry is None:
            return JSONResponse({"status": "ok", "watched": False, "path": str(p)})

        was_suspended = entry.suspended
        if was_suspended and state.resume:
            state.resume(entry, reason="prompt")
        else:
            entry.touch()
        return JSONResponse({
            "status": "ok",
            "watched": True,
            "resumed": was_suspended,
            "project_name": entry.project_name,
            "path": str(entry.path),
        })

    # TASK-S05 — Localhost-only graceful shutdown route. Allows
    # `Invoke-RestMethod -Method POST http://127.0.0.1:7702/admin/shutdown`
    # to stop the server without needing elevated taskkill.
    @mcp_server.custom_route("/admin/shutdown", methods=["POST"])
    async def admin_shutdown(request: Request) -> JSONResponse:
        client_host = request.client.host if request.client else None
        if client_host not in ("127.0.0.1", "::1", "localhost"):
            return JSONResponse({"error": "forbidden", "client": client_host}, status_code=403)

        def _exit_soon():
            time.sleep(0.5)  # let the response flush
            try:
                if state.observer:
                    state.observer.stop()
                    state.observer.join(timeout=2.0)
                state.handler.shutdown()
                state.registry.close_all()
                logger.info("Sentinel shut down via /admin/shutdown.")
            except Exception as e:
                logger.warning(f"Shutdown cleanup error: {e}")
            os._exit(0)

        threading.Thread(target=_exit_soon, daemon=True, name="AdminShutdown").start()
        return JSONResponse({"status": "shutting down", "pid": os.getpid()})

    logger.info("Custom routes registered (/health, /admin/shutdown). Starting MCP SSE server on 127.0.0.1:7702...")
    try:
        # TASK-S03 — Pure FastMCP. host/port flow through to fastmcp.run_http_async.
        # This is the single entry point for HTTP + SSE; replaces the old
        # SseServerTransport/Starlette/uvicorn manual wiring.
        mcp_server.run(transport="sse", host="127.0.0.1", port=7702, show_banner=False)
    finally:
        if state.observer:
            state.observer.stop()
            state.observer.join()
        state.handler.shutdown()
        state.registry.close_all()
        logger.info("Sentinel shut down cleanly.")


if __name__ == "__main__":
    main()
