# mcp_server.py — Shadow-Core Sentinel (FastMCP rewrite)
# Migrated from raw mcp.server.Server (P-007) to FastMCP for unified
# observability, TraceMiddleware injection, and Obsidian structured logging.
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Dict

from fastmcp import FastMCP

from config import settings
from differ import diff_strings

# NOTE (2026-07-31): this module used to reach into a SIBLING PROJECT --
# `sys.path.insert(0, '../Shadow-Core Engineer')` followed by
# `from telemetry import TraceMiddleware` -- to add tracing middleware.
#
# That import had been FAILING silently. TraceMiddleware was deleted from
# Engineer's telemetry.py during its Phase-1 Bare SDK migration; the note left
# in that file says per-request tracing moved to a `@traced` decorator instead.
# Sentinel's try/except caught the ImportError, logged one warning, and ran
# without middleware. Verified before removal: the import raises
# "cannot import name 'TraceMiddleware' from 'telemetry'".
#
# So the coupling bought nothing and cost portability -- it is why the
# PyInstaller spec hardcoded an absolute path to another project's directory
# (TECH_DEBT_AUDIT.md #6), which breaks on any other machine.
#
# If tracing is wanted again, Engineer's `traced` decorator is the current
# surface. Vendor it or depend on it deliberately; do not resurrect a
# relative-path sys.path hack.

logger = logging.getLogger("sc.sentinel.mcp")

# ── Module-level FastMCP instance (replaces Server factory) ───────────────
mcp = FastMCP("shadow-core-sentinel")


def build_mcp_server(state) -> FastMCP:
    """
    Wire live dependencies into the FastMCP tool closures and return the
    configured server. Called once from main.py at startup.

    TASK-S03 — Returns the FastMCP instance directly (was `mcp._mcp_server`
    when main.py used raw SseServerTransport). main.py now calls
    `mcp_server.run(transport="sse", ...)` directly.

    TASK-S12 — Takes a single `state` object (SentinelState dataclass) instead
    of (handler, builder, observer, store) positional args. Tools reference
    `state.store` / `state.builder` etc., so pivot/rollback that swap state
    fields are visible to all tools (was a latent bug — old tool closures
    captured the original store/builder objects forever).
    """

    # ── Resources ─────────────────────────────────────────────────────────

    @mcp.resource("audit://logs/{date_key}")
    async def read_log(date_key: str) -> str:
        """Read a daily audit log by date key (YYYY-MM-DD)."""
        content = state.builder.read_artifact(date_key)
        if not content:
            return f"No audit log found for {date_key}"
        return content

    @mcp.resource("audit://snapshots/{snap_name}")
    async def read_snapshot(snap_name: str) -> str:
        """Read an audit snapshot by filename."""
        content = state.builder.read_artifact_by_path(settings.audit_dir / snap_name)
        if not content:
            return f"No snapshot found: {snap_name}"
        return content

    # ── Tools ──────────────────────────────────────────────────────────────

    @mcp.tool()
    async def list_audit_dates() -> Dict:
        """List all available audit log dates (YYYY-MM-DD)."""
        artifacts = state.builder.list_artifacts()
        dates = []
        for artifact in artifacts:
            name = artifact.stem
            if name.startswith("audit-"):
                dates.append(name[6:])
        return {"status": "ok", "dates": sorted(dates, reverse=True)}

    @mcp.tool()
    async def get_daily_report(date_key: str) -> Dict:
        """
        Retrieve the full audit report for a specific date.

        Args:
            date_key: Date in YYYY-MM-DD format (e.g. '2026-05-06').
        """
        content = state.builder.read_artifact(date_key)
        if not content:
            return {"status": "error", "message": f"No report for {date_key}"}
        return {"status": "ok", "date": date_key, "content": content}

    @mcp.tool()
    async def get_today_report() -> Dict:
        """Retrieve today's audit report (UTC date)."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return await get_daily_report(today)

    @mcp.tool()
    async def query_events(date_key: str) -> Dict:
        """
        Query raw filesystem events for a specific date.

        Args:
            date_key: Date in YYYY-MM-DD format.
        """
        events = state.store.query_by_date(date_key)
        return {
            "status": "ok",
            "date": date_key,
            "count": len(events),
            "events": events,
        }

    @mcp.tool()
    async def sentinel_status() -> Dict:
        """Return the Sentinel's current operational status."""
        return {
            "status": "ok",
            "watch_paths": settings.watch_paths if hasattr(settings, "watch_paths") else [],
            "total_events": state.store.total_count(),
            "observer_alive": state.observer.is_alive() if hasattr(state.observer, "is_alive") else "unknown",
            "ambient_state": state.notifier.state.name.lower() if hasattr(state.notifier, "state") else "unknown",
        }

    @mcp.tool()
    async def diff_snapshot(snapshot_a: str, snapshot_b: str) -> Dict:
        """
        Produce a unified diff between two named snapshots.

        Args:
            snapshot_a: First snapshot filename.
            snapshot_b: Second snapshot filename.
        """
        content_a = state.builder.read_artifact_by_path(settings.audit_dir / snapshot_a)
        content_b = state.builder.read_artifact_by_path(settings.audit_dir / snapshot_b)
        if content_a is None:
            return {"status": "error", "message": f"Snapshot not found: {snapshot_a}"}
        if content_b is None:
            return {"status": "error", "message": f"Snapshot not found: {snapshot_b}"}
        diff = diff_strings(content_a, content_b)
        return {
            "status": "ok",
            "snapshot_a": snapshot_a,
            "snapshot_b": snapshot_b,
            "diff": diff,
        }

    logger.info("Sentinel FastMCP server configured — 6 tools registered (5 + sentinel_status with ambient_state)")
    return mcp