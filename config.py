import fnmatch
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

# ONE implementation of the audit-folder naming rule, not two.
#
# `config._safe_dir_name` and `watch_registry.safe_project_name` were
# byte-identical, and watch_registry's docstring warned that letting them
# diverge would orphan every existing sentinel.db — while nothing stopped that
# happening, and only one of the two was covered by a test. The registry's copy
# is the one that decides where a watched project's database actually lives, so
# it is the one that survives. No cycle: watch_registry imports nothing from
# this module.
from watch_registry import safe_project_name


@dataclass
class Settings:
    watch_dir: Path = field(
        default_factory=lambda: Path(os.environ.get("WATCH_DIR", "./watched"))
    )
    # Base audit dir — project subdirs are created under here
    base_audit_dir: Path = field(
        default_factory=lambda: Path(os.environ.get("AUDIT_DIR", "./audit_logs"))
    )
    mcp_server_name: str = "shadow-core-sentinel"
    mcp_server_version: str = "1.2.1"
    recursive: bool = True
    max_memory_events: int = 500

    alert_event_count: int = int(os.environ.get("ALERT_EVENT_COUNT", "100"))
    alert_window_seconds: int = int(os.environ.get("ALERT_WINDOW_SECONDS", "10"))

    dashboard_enabled: bool = os.environ.get("DASHBOARD_ENABLED", "true").lower() == "true"
    dashboard_port: int = int(os.environ.get("DASHBOARD_PORT", "7654"))

    # The MCP SSE endpoint. These were literals at the `mcp.run(...)` call site,
    # repeated in the log line beside it, and unreachable from configuration —
    # while `--port`, the only port flag, moved the DASHBOARD. Loopback is the
    # default and should stay it: this serves a filesystem audit trail, which
    # has no business being reachable off-machine.
    mcp_host: str = os.environ.get("MCP_HOST", "127.0.0.1")
    mcp_port: int = int(os.environ.get("MCP_PORT", "7702"))

    log_level: str = os.environ.get("LOG_LEVEL", "INFO")

    # Delete all recorded audit data at startup, so a run begins with no
    # history. Sentinel already discards its watches and its in-RAM ring on
    # restart; this completes that.
    #
    # The trade is deliberate and one-way: cross-restart forensics — "what
    # changed while I wasn't looking" — becomes unanswerable, because the
    # evidence is gone before the question can be asked. What it buys is
    # bounded disk. Without it the trail had reached 252 MB with no retention
    # policy of any kind, 38% of that a dead project born from a typo.
    # Set SENTINEL_FLUSH_ON_START=false to keep history across restarts.
    flush_on_start: bool = os.environ.get(
        "SENTINEL_FLUSH_ON_START", "true").lower() == "true"

    # Idle watches suspend themselves after this long with no events and no
    # prompt (see lease.py). Suspension is not removal — the entry, its store
    # and its history survive, and the next prompt in that project resumes it.
    # 0 disables suspension entirely.
    watch_idle_ttl_seconds: int = int(os.environ.get("WATCH_IDLE_TTL_SECONDS", "3600"))
    watch_sweep_seconds: int = int(os.environ.get("WATCH_SWEEP_SECONDS", "60"))

    ignore_patterns: List[str] = field(
        default_factory=lambda: [
            # NOTE: plain "venv" as well as ".venv". Measured 2026-07-31 while
            # watching a real project: Scriptweaver keeps its interpreter at
            # backend/venv (no dot), so python.exe, pyvenv.cfg and
            # distutils-precedence.pth were being recorded as project activity.
            # 1.3 GB of virtualenv churn drowns the signal the trail exists for.
            "node_modules", ".git", ".venv", ".venv311", "venv", "env",
            "__pycache__", "site-packages",
            # "*.tmp.*" as well as "*.tmp". Measured 2026-07-31: an atomic
            # editor write leaves `main.py.tmp.15328.5bb47c1b481e`, which the
            # plain "*.tmp" glob does not match. One edit was producing four
            # rows, three of them about a scratch file that no longer exists.
            # "build_*"/"dist_*" as well as the bare names. Measured
            # 2026-07-31: a staged PyInstaller rebuild into build_new/ and
            # dist_new/ put ~30 rows of DELETED bootloader artifacts into the
            # trail when the staging dirs were cleaned up. Deliberately NOT
            # "build*"/"dist*" — that glob matches on any path component, so it
            # would silently swallow real source directories called
            # "distributed", "distribution" or "builder".
            "*.tmp", "*.tmp.*", ".DS_Store", "target", "dist", "build",
            "build_*", "dist_*",
            "*~", "*.swp", "*.swx", "*.orig", "*.rej",
            "*.pyc", "*.pyo", ".mypy_cache", ".pytest_cache",
            "sentinel.db", "sentinel.db-journal",
            ".idea", ".vscode", "*.log",
        ]
    )

    _prev_watch_dir: Optional[Path] = field(default=None, init=False, repr=False)

    # Derived — set automatically, do not set manually
    audit_dir: Path = field(init=False)
    db_path: Path = field(init=False)

    def __post_init__(self) -> None:
        self.watch_dir = Path(self.watch_dir).resolve()
        self.base_audit_dir = Path(self.base_audit_dir).resolve()
        self.base_audit_dir.mkdir(parents=True, exist_ok=True)
        self._refresh_project_paths()

    def _refresh_project_paths(self) -> None:
        """Recompute audit_dir and db_path from the current watch_dir."""
        project_name = safe_project_name(self.watch_dir)
        self.audit_dir = self.base_audit_dir / project_name
        self.audit_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.audit_dir / "sentinel.db"

    def is_ignored(self, path: Path) -> bool:
        # Audit output is skipped by CONTAINMENT, not by pattern.
        #
        # There used to be an `_auto_ignore_audit_dir()` that ran on every
        # `update_watch_dir` and appended three entries to the shared
        # `ignore_patterns` list: the audit dir, the base audit dir, and —
        # the damaging one — `rel.parts[0]`, the first path component between
        # the watched root and the audit dir. That last one is a BARE DIRECTORY
        # NAME, and the loop below matches bare names against every component of
        # every path in every watched project.
        #
        # Measured: pivoting to `E:\AI Backup Projects` derived the anchor
        # `Shadow-Core Sentinel` and appended it, after which
        # `is_ignored(...\Shadow-Core Sentinel\main.py)` returned True — Sentinel
        # silently stopped recording its own repository, and would stop recording
        # any other project with a component of that name, for the life of the
        # process. One dashboard pivot could blind an unrelated session's watch.
        # The list also grew monotonically with absolute paths that were never
        # pruned.
        #
        # All three entries were redundant: the two containment checks below
        # already cover the audit tree completely, and cover it for EVERY
        # project at once, because every project's audit dir lives under
        # `base_audit_dir`. Deleting the pattern-writing left the behaviour that
        # was wanted and removed the one that was not.
        for root in (self.audit_dir, self.base_audit_dir):
            try:
                path.relative_to(root)
                return True
            except ValueError:
                pass

        parts = path.parts
        for pattern in self.ignore_patterns:
            if any(fnmatch.fnmatch(part, pattern) for part in parts):
                return True
            if fnmatch.fnmatch(str(path), pattern):
                return True
        return False

    def add_ignore_pattern(self, pattern: str) -> None:
        if pattern not in self.ignore_patterns:
            self.ignore_patterns.append(pattern)

    def update_watch_dir(self, new_path: Path) -> None:
        self._prev_watch_dir = self.watch_dir
        self.watch_dir = new_path.resolve()
        self._refresh_project_paths()

    def rollback_watch_dir(self) -> Optional[Path]:
        if self._prev_watch_dir is None:
            return None
        self.watch_dir, self._prev_watch_dir = self._prev_watch_dir, self.watch_dir
        self._refresh_project_paths()
        return self.watch_dir

    @property
    def project_name(self) -> str:
        return safe_project_name(self.watch_dir)


settings = Settings()
