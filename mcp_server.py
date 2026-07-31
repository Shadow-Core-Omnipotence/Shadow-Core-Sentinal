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
    async def recent_changes(minutes: int = 15, limit: int = 100,
                             include_hashes: bool = False) -> Dict:
        """What actually changed on disk in the last N minutes. START HERE.

        This is the tool for confirming work: after editing files, call it to
        see what the filesystem really recorded, rather than trusting that an
        edit did what was intended. Comparing "files I meant to touch" against
        this catches both missed edits and unintended ones.

        Prefer this over query_events. query_events takes a whole DATE, which
        on a busy project is tens of thousands of rows (measured: 19,936 in one
        day) — far too many to read and not an answer to "did my change land?".
        A task-sized window is usually tens of rows.

        Hashes are omitted by default: the kind and path already say what
        changed, and a SHA is 64 characters per row. Ask for them only when
        verifying content identity.

        Args:
            minutes: How far back to look. Default 15.
            limit: Maximum rows returned. Default 100.
            include_hashes: Include the SHA-256 of each file. Default False.
        """
        from datetime import timedelta

        if state.store is None:
            return {"status": "idle",
                    "message": "No project is being watched. Call watch_project first.",
                    "count": 0, "events": []}

        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=max(1, minutes)))
        since_iso = cutoff.isoformat()
        total = state.store.count_since(since_iso)
        events = state.store.query_since(since_iso, limit=limit,
                                         include_hashes=include_hashes)
        return {
            "status": "ok",
            "project": str(state.primary),
            "window_minutes": minutes,
            "total_in_window": total,
            "returned": len(events),
            "truncated": total > len(events),
            "events": events,
        }

    @mcp.tool()
    async def query_events(date_key: str, limit: int = 500,
                           include_hashes: bool = False) -> Dict:
        """Filesystem events for a whole calendar date.

        Use recent_changes instead unless you specifically need a historical
        day. A busy project records tens of thousands of events per date, so
        this is capped by `limit` and will usually be truncated.

        Args:
            date_key: Date in YYYY-MM-DD format.
            limit: Maximum rows returned. Default 500.
            include_hashes: Include the SHA-256 of each file. Default False.
        """
        if state.store is None:
            return {"status": "idle",
                    "message": "No project is being watched. Call watch_project first.",
                    "count": 0, "events": []}

        events = state.store.query_by_date(date_key)
        total = len(events)
        events = events[:max(1, limit)]
        if not include_hashes:
            for e in events:
                e.pop("sha256", None)
        return {
            "status": "ok",
            "date": date_key,
            "total_on_date": total,
            "returned": len(events),
            "truncated": total > len(events),
            "events": events,
        }

    @mcp.tool()
    async def sentinel_status() -> Dict:
        """Return the Sentinel's current operational status."""
        return {
            "status": "ok",
            # Was `settings.watch_paths if hasattr(...) else []` — config only
            # ever had `watch_dir`, singular, so this always reported an empty
            # list. Someone had anticipated multi-watch; now it exists.
            "watch_paths": state.registry.paths() if state.registry else [],
            "primary_watch": str(state.primary) if state.primary else None,
            "total_events": state.store.total_count() if state.store else 0,
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

    # ── Watch control ──────────────────────────────────────────────────────
    # An MCP server has no idea what directory its client is working in, so an
    # assistant has to say so explicitly. Without these there was no way to
    # point Sentinel at anything from a session at all — watching could only be
    # changed from the dashboard.

    @mcp.tool()
    async def watch_project(path: str) -> Dict:
        """Start recording filesystem changes for a project directory.

        Call this once at the start of a session, with the absolute path of the
        directory being worked in. Sentinel then records every create, modify,
        delete and move under it, with a SHA-256 of each file, so later changes
        can be confirmed against what actually happened on disk rather than
        what was assumed.

        Additive and safe to repeat: other projects already being watched are
        NOT affected, and re-calling it for a directory already watched is a
        no-op. Several sessions can each watch their own project at once.

        Args:
            path: Absolute path of the project directory to watch.
        """
        from pathlib import Path as _Path

        if not path:
            return {"status": "error", "message": "No path provided"}
        p = _Path(path).expanduser()
        try:
            p = p.resolve()
        except OSError as exc:
            return {"status": "error", "message": f"Bad path: {exc}"}
        if not p.is_dir():
            return {"status": "error", "message": f"Not a directory: {p}"}

        already = state.registry.get(p) is not None
        entry = state.registry.add(p)
        if not already:
            from observer import add_watch
            entry.handle = add_watch(state.observer, state.handler, entry.path)

        return {
            "status": "ok",
            "already_watching": already,
            "path": str(entry.path),
            "project_name": entry.project_name,
            "watching": state.registry.paths(),
        }

    @mcp.tool()
    async def unwatch_project(path: str) -> Dict:
        """Stop recording changes for a project directory.

        Only call this when explicitly asked to. Another session may be relying
        on this watch, and stopping it silently would leave that session
        confirming its work against a directory nobody is recording. Watches
        cost very little to leave running.

        Removing the last watch is allowed: Sentinel returns to idle, which is
        also how it starts with the machine.

        Args:
            path: Absolute path of the project directory to stop watching.
        """
        from pathlib import Path as _Path

        if not path:
            return {"status": "error", "message": "No path provided"}
        p = _Path(path).expanduser().resolve()

        entry = state.registry.get(p)
        if entry is None:
            return {"status": "error", "message": f"Not being watched: {p}"}

        from observer import remove_watch
        remove_watch(state.observer, entry.handle)
        state.registry.remove(p)
        if state.primary == entry.path:
            # entries() is empty once the last watch goes, so index [0] would
            # raise. None is the correct answer: Sentinel is idle again.
            remaining = state.registry.entries()
            state.primary = remaining[0].path if remaining else None

        return {
            "status": "ok",
            "unwatched": str(p),
            "watching": state.registry.paths(),
            "idle": len(state.registry) == 0,
        }

    @mcp.tool()
    async def list_watched_projects() -> Dict:
        """List every project directory Sentinel is currently recording.

        Use this to check whether the directory being worked in is actually
        being watched before relying on Sentinel to confirm a change. A project
        that is not listed has no audit trail being written for it.
        """
        return {
            "status": "ok",
            "count": len(state.registry),
            "primary": str(state.primary),
            "projects": [
                {
                    "path": str(e.path),
                    "project_name": e.project_name,
                    "audit_dir": str(e.audit_dir),
                }
                for e in state.registry.entries()
            ],
        }

    logger.info("Sentinel FastMCP server configured — 9 tools registered "
                "(6 read + watch_project/unwatch_project/list_watched_projects)")
    return mcp