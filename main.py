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
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config import settings
from dashboard import start_dashboard
from dashboard_wiring import DashboardWiring
from http_routes import register_routes
from lease import LeaseSweeper, WatchLifecycle
from mcp_server import build_mcp_server
from observer import AuditEventHandler, add_watch, remove_watch, start_bare_observer
from report_builder import ReportBuilder
from retention import flush_if_configured
from storage import EventStore
from watch_registry import WatchRegistry


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
        if not self.registry:
            return None
        if self.primary is None:
            # One watched project is not ambiguous, so an unset primary is no
            # reason to report idle. Read-side tools used to return
            # "no project is being watched" to a session that had just watched
            # one successfully, because only the dashboard's pivot path ever
            # assigned `primary`. Falling back here means the read path can
            # never be blind while exactly one project is recording.
            entries = self.registry.entries()
            return entries[0] if len(entries) == 1 else None
        return self.registry.get(self.primary)

    @property
    def store(self):
        entry = self._primary_entry()
        return entry.store if entry else None

    @property
    def builder(self):
        entry = self._primary_entry()
        return entry.builder if entry else None

    @property
    def primary_path(self):
        """The project the read side is actually answering about, or None.

        Reported instead of `self.primary` so responses cannot disagree with
        the data beside them: when `primary` is unset and one project is
        watched, `store` resolves to that project, and this names it. Callers
        must serialise this as JSON null when it is None — `str(None)` yields
        the four-character string "None", which was being emitted as a project
        path.
        """
        entry = self._primary_entry()
        return entry.path if entry else None


def _shutdown(state, join_timeout: float | None = 2.0) -> None:
    """Stop the observer, drain the hashing pool, close every store.

    ONE teardown, used by both exits. The `finally` around `mcp.run()` and the
    `/admin/shutdown` route each had their own copy, which is how they came to
    differ — the route joined with a timeout and the finally block joined
    forever, and only one of them logged. A cleanup path that runs in two
    versions is one nobody has verified.
    """
    if state.observer:
        state.observer.stop()
        state.observer.join(timeout=join_timeout) if join_timeout \
            else state.observer.join()
    state.handler.shutdown()
    state.registry.close_all()


def parse_args():
    parser = argparse.ArgumentParser(description="Shadow-Core Sentinel MCP Server")
    parser.add_argument("--watch", default=None)
    parser.add_argument("--dashboard-port", type=int, default=None,
                        help="Port for the HTML dashboard (default 7654).")
    # `--port` named neither of the two ports it could have meant, and moved
    # the dashboard. Kept as a deprecated alias so an existing launcher — the
    # scheduled task, a shortcut — does not start failing on an unknown flag.
    parser.add_argument("--port", type=int, default=None,
                        dest="deprecated_port",
                        help="Deprecated alias for --dashboard-port.")
    parser.add_argument("--mcp-port", type=int, default=None,
                        help="Port for the MCP SSE endpoint (default 7702).")
    parser.add_argument("--mcp-host", default=None,
                        help="Bind address for the MCP SSE endpoint "
                             "(default 127.0.0.1; loopback is deliberate).")
    parser.add_argument("--no-dashboard", action="store_true")
    parser.add_argument("--log-level", default=None,
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser.parse_args()


def apply_args(args) -> None:
    """Fold command-line overrides into settings. Flags beat environment."""
    if args.watch:
        settings.update_watch_dir(Path(args.watch))
    if args.dashboard_port:
        settings.dashboard_port = args.dashboard_port
    elif args.deprecated_port:
        settings.dashboard_port = args.deprecated_port
    if args.mcp_port:
        settings.mcp_port = args.mcp_port
    if args.mcp_host:
        settings.mcp_host = args.mcp_host
    if args.no_dashboard:
        settings.dashboard_enabled = False
    if args.log_level:
        settings.log_level = args.log_level


def configure_logging() -> logging.Logger:
    """File-based logging, and stdio redirected into a file.

    TASK-S02 — a log file is written however the exe was launched, so a failure
    is visible rather than inferred.

    TASK-S03 follow-up — when launched via `Start-Process -WindowStyle Hidden`
    the exe has no console, and native stdout/stderr writes (uvicorn, the
    FastMCP banner, any stray print) DEADLOCK on the pipe. Redirecting
    Python-level sys.stdout/stderr to a file means nothing blocks. This must
    happen BEFORE mcp.run(), or FastMCP/uvicorn hang while starting the HTTP
    server — which is the silent failure that made this service so hard to
    diagnose in the first place.
    """
    log_path = settings.audit_dir / "sentinel.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    stdio_log = open(log_path.with_suffix(".stdio.log"), "a",
                     encoding="utf-8", buffering=1)
    sys.stdout = stdio_log
    sys.stderr = stdio_log

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
    logger.info(f"Audit dir: {settings.base_audit_dir}")
    return logger


def main():
    apply_args(parse_args())

    # BEFORE logging is configured. Sentinel's own log file lives inside the
    # directory being flushed, so opening it first would mean deleting a file
    # already held open — on Windows that fails outright. The summary is logged
    # a few lines below, once there is somewhere to log it to.
    flush_summary = flush_if_configured(settings)
    settings._refresh_project_paths()   # recreate what the flush removed

    logger = configure_logging()
    if flush_summary["removed"]:
        logger.warning(
            "Flushed %d audit item(s) at startup, freeing %.1f MB — history "
            "before this run is gone (SENTINEL_FLUSH_ON_START=false to keep it): %s",
            len(flush_summary["removed"]),
            flush_summary["bytes_freed"] / 1048576,
            ", ".join(flush_summary["removed"][:8])
            + (" …" if len(flush_summary["removed"]) > 8 else ""),
        )
    for problem in flush_summary["errors"]:
        logger.error("Startup flush: %s", problem)

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
        # The markdown trail is written either way. A failed DB row means the
        # two records of this period disagree, and the markdown one is then the
        # only evidence the event happened at all — dropping it too would turn a
        # partial record into no record. The disagreement is made visible via
        # failed_writes rather than papered over.
        if not entry.store.insert(event):
            logger.warning(
                "Event NOT persisted to %s (%d failed writes for this project) — "
                "the audit trail is incomplete: %s",
                entry.project_name, entry.store.failed_writes, event.src_path)
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
        wiring = DashboardWiring(state, settings, add_watch, logger)
        start_dashboard(settings.dashboard_port, *wiring.as_callbacks())
        logger.info(f"Dashboard → http://127.0.0.1:{settings.dashboard_port}")

    # TASK-S03 — Wire MCP tools via FastMCP (no more raw SseServerTransport).
    mcp_server = build_mcp_server(state)
    register_routes(mcp_server, settings=settings, state=state,
                    shutdown_hook=lambda: _shutdown(state))

    logger.info("Custom routes registered (/health, /admin/shutdown). "
                "Starting MCP SSE server on %s:%s...",
                settings.mcp_host, settings.mcp_port)
    try:
        # TASK-S03 — Pure FastMCP. host/port flow through to fastmcp.run_http_async.
        # This is the single entry point for HTTP + SSE; replaces the old
        # SseServerTransport/Starlette/uvicorn manual wiring.
        mcp_server.run(transport="sse", host=settings.mcp_host,
                       port=settings.mcp_port, show_banner=False)
    finally:
        _shutdown(state, join_timeout=None)
        logger.info("Sentinel shut down cleanly.")


if __name__ == "__main__":
    main()
