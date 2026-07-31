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
from observer import AuditEventHandler, start_observer
from report_builder import ReportBuilder
from storage import EventStore
from ambient_notifier import AmbientNotifier


# TASK-S12 — Single mutable state object. Replaces the _ref = [obj] pattern.
@dataclass
class SentinelState:
    store: EventStore
    builder: ReportBuilder
    handler: AuditEventHandler
    notifier: AmbientNotifier
    observer: Any = None  # set after start_observer()


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
    state = SentinelState(
        store=EventStore(settings.db_path, str(settings.watch_dir)),
        builder=ReportBuilder(settings.audit_dir),
        handler=AuditEventHandler(),
        notifier=AmbientNotifier(str(settings.watch_dir)),
    )

    state.handler.subscribe(lambda e: state.store.insert(e))
    state.handler.subscribe(lambda e: state.builder.append_event(e))
    state.handler.subscribe(state.notifier.on_event)

    state.observer = start_observer(state.handler)
    logger.info("File observer started")
    logger.info("AmbientNotifier active — signals → %s", Path.home() / ".shadow_core" / "projects")

    if settings.dashboard_enabled:

        def get_stats():
            mem = state.handler.recent_events(settings.max_memory_events)
            alerts = state.handler.recent_alerts()
            return {
                "watch_dir": str(settings.watch_dir),
                "project_name": settings.project_name,
                "audit_dir": str(settings.audit_dir),
                "version": settings.mcp_server_version,
                "total_db_events": state.store.total_count(),
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
            if not path_str:
                return {"status": "error", "message": "No path provided"}
            p = Path(path_str).resolve()
            if not p.exists() or not p.is_dir():
                return {"status": "error", "message": f"Not a directory: {path_str}"}
            try:
                # Pivot observer
                state.observer.unschedule_all()
                settings.update_watch_dir(p)
                state.handler._watch = state.observer.schedule(
                    state.handler, str(p), recursive=settings.recursive)

                # Swap store to new project DB
                old_store = state.store
                state.store = EventStore(settings.db_path, str(p))
                old_store.close()

                # Swap builder to new project audit dir
                state.builder = ReportBuilder(settings.audit_dir)
                state.store.set_watch_path(str(p))

                logger.info(f"Pivoted to project: {settings.project_name} → {p}")
                return {
                    "status": "ok",
                    "new_watch_path": str(p),
                    "project_name": settings.project_name,
                    "audit_dir": str(settings.audit_dir),
                }
            except Exception as e:
                return {"status": "error", "message": str(e)}

        def do_rollback() -> dict:
            prev = settings.rollback_watch_dir()
            if prev is None:
                return {"status": "error", "message": "No previous path"}
            try:
                state.observer.unschedule_all()
                state.handler._watch = state.observer.schedule(
                    state.handler, str(prev), recursive=settings.recursive)
                old_store = state.store
                state.store = EventStore(settings.db_path, str(prev))
                old_store.close()
                state.builder = ReportBuilder(settings.audit_dir)
                return {
                    "status": "ok",
                    "restored_path": str(prev),
                    "project_name": settings.project_name,
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
        ambient_state_name = state.notifier.state.name.lower() if hasattr(state.notifier, "state") else "unknown"
        recovery_active = getattr(state.notifier, "_recovery_active", False)
        return JSONResponse({
            "sentinel": "ok",
            "version": settings.mcp_server_version,
            "observer": "ok" if observer_alive else "down",
            "ambient": ambient_state_name,
            "ambient_recovery_active": recovery_active,
            "dashboard": "ok" if settings.dashboard_enabled else "disabled",
            "watch_dir": str(settings.watch_dir),
            "total_events": state.store.total_count(),
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
                state.notifier.shutdown()
                state.store.close()
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
        state.notifier.shutdown()
        state.store.close()
        logger.info("Sentinel shut down cleanly.")


if __name__ == "__main__":
    main()
