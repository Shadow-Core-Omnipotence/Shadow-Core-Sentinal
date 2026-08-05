"""The plain-HTTP routes served alongside MCP on the SSE port.

WHY THIS EXISTS
---------------
`/health`, `/api/touch` and `/admin/shutdown` were defined inline in `main()`
as decorated closures over `state`, `settings` and `logger`. Three route
handlers with real logic in them — a localhost check, a two-format body parse,
a longest-prefix registry lookup, a deferred shutdown — none of it reachable
without starting the whole service.

They live on the MCP port rather than the dashboard's on purpose: the dashboard
can be disabled, and a keepalive hook that silently stopped working would let
every project suspend with nothing able to wake it.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger("sc.sentinel.routes")

# Loopback spellings. These endpoints expose and control a filesystem audit
# trail; nothing off-machine has any business reaching them.
_LOCAL_HOSTS = ("127.0.0.1", "::1", "localhost")


def _is_local(request: Request) -> bool:
    return bool(request.client) and request.client.host in _LOCAL_HOSTS


def total_failed_writes(state) -> int:
    """Audit rows dropped across every watched project.

    Non-zero means the trail is INCOMPLETE. Summed across watches, because a
    failure on any project is a reason to stop trusting what this service says.
    """
    if not state.registry:
        return 0
    return sum(getattr(e.store, "failed_writes", 0) or 0
               for e in state.registry.entries() if e.store)


async def _read_path(request: Request) -> str:
    """The `path` field from a JSON or form-encoded body.

    Both are accepted because the caller is a one-line shell hook, and
    hand-building JSON around a Windows path there means escaping backslashes in
    the shell — fragile, and it fails SILENTLY by posting a malformed body that
    parses to {}. `curl --data-urlencode` quotes any path correctly, so a form
    body has to work too.
    """
    try:
        body = await request.json()
        return (body or {}).get("path") or ""
    except Exception:  # noqa: BLE001 — not JSON, try a form body
        try:
            form = await request.form()
            return form.get("path") or ""
        except Exception:  # noqa: BLE001 — neither; treated as no path
            return ""


def register_routes(mcp_server, state, settings,
                    shutdown_hook: Optional[Callable] = None) -> None:
    """Attach /health, /api/touch and /admin/shutdown to the FastMCP server."""

    @mcp_server.custom_route("/health", methods=["GET"])
    async def health(request: Request) -> JSONResponse:
        observer_alive = bool(state.observer and state.observer.is_alive())
        return JSONResponse({
            "sentinel": "ok",
            "version": settings.mcp_server_version,
            "observer": "ok" if observer_alive else "down",
            "dashboard": "ok" if settings.dashboard_enabled else "disabled",
            "watch_dir": str(state.primary_path) if state.primary_path else None,
            "watching": state.registry.paths(),
            "watch_count": len(state.registry),
            "suspended": [str(e.path) for e in state.registry.suspended_entries()],
            "total_events": state.store.total_count() if state.store else 0,
            "failed_writes": total_failed_writes(state),
            "as_of": datetime.now(timezone.utc).isoformat(),
        })

    @mcp_server.custom_route("/api/touch", methods=["POST"])
    async def touch(request: Request) -> JSONResponse:
        """Keepalive from the UserPromptSubmit hook.

        Renews the lease for the project the prompt was typed in, and resumes it
        if it had gone idle — so a suspended watch is recording again before the
        session's first tool call.
        """
        if not _is_local(request):
            return JSONResponse({"error": "forbidden"}, status_code=403)

        raw = await _read_path(request)
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

    @mcp_server.custom_route("/admin/shutdown", methods=["POST"])
    async def admin_shutdown(request: Request) -> JSONResponse:
        """Graceful stop without elevated taskkill.

        Sentinel starts from a scheduled task; when that task ran elevated, a
        non-elevated `taskkill` returned "Access denied" and every rebuild
        needed Task Manager. This is the supported way to stop it.
        """
        if not _is_local(request):
            client = request.client.host if request.client else None
            return JSONResponse({"error": "forbidden", "client": client},
                                status_code=403)

        def _exit_soon():
            time.sleep(0.5)  # let the response flush
            try:
                if shutdown_hook:
                    shutdown_hook()
                logger.info("Sentinel shut down via /admin/shutdown.")
            except Exception as e:  # noqa: BLE001 — exiting regardless
                logger.warning(f"Shutdown cleanup error: {e}")
            os._exit(0)

        threading.Thread(target=_exit_soon, daemon=True,
                         name="AdminShutdown").start()
        return JSONResponse({"status": "shutting down", "pid": os.getpid()})
