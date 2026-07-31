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

    if settings.dashboard_enabled:

        def get_stats():
            mem = state.handler.recent_events(settings.max_memory_events)
            alerts = state.handler.recent_alerts()
            return {
                "watch_dir": str(state.primary),
                "watching": state.registry.paths(),
                "project_name": settings.project_name,
                "audit_dir": str(settings.audit_dir),
                "version": settings.mcp_server_version,
                "total_db_events": state.store.total_count() if state.store else 0,
                "memory_events": len(mem),
                "recent_alerts": len(alerts),
                "alerts_detail": alerts,
                "ignore_patterns": len(settings.ignore_patterns),
                "patterns_list": settings.ignore_patterns,
                "as_of": datetime.now(tz=timezone.utc).isoformat(),
            }

        def get_events():
            return [
                {
                    "ts": e.timestamp.isoformat(),
                    "kind": e.kind.value,
                    "src_path": str(e.src_path),
                    "dest_path": str(e.dest_path) if e.dest_path else None,
                    "sha256": e.sha256,
                }
                for e in state.handler.recent_events(200)
            ]

        def get_snapshots():
            return [a.name for a in state.builder.list_artifacts()
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

        def do_audit(label: str, mode: str) -> dict:
            if mode == "disk":
                snap = state.builder.build_disk_snapshot(label)
            else:
                snap = state.builder.build_snapshot(state.handler.recent_events(200), label)
            return {"status": "ok", "snapshot_uri": snap.name}

        def do_ignore(pattern: str) -> dict:
            if not pattern:
                return {"status": "error", "message": "No pattern"}
            settings.add_ignore_pattern(pattern)
            return {"status": "ok", "pattern": pattern}

        start_dashboard(
            settings.dashboard_port,
            get_stats, get_events, get_snapshots,
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
            "total_events": state.store.total_count() if state.store else 0,
            "as_of": datetime.now(timezone.utc).isoformat(),
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
