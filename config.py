import fnmatch
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional
import re


def _safe_dir_name(path: Path) -> str:
    """Convert a path to a safe directory name using the watched folder name."""
    name = path.name or path.drive.replace(":", "").replace("\\", "")
    # Replace unsafe chars with hyphens
    return re.sub(r'[^\w\-]', '-', name).strip('-') or "default"


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

    log_level: str = os.environ.get("LOG_LEVEL", "INFO")

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
        project_name = _safe_dir_name(self.watch_dir)
        self.audit_dir = self.base_audit_dir / project_name
        self.audit_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.audit_dir / "sentinel.db"
        self._auto_ignore_audit_dir()

    def _auto_ignore_audit_dir(self) -> None:
        """Always ignore the audit_dir, whether inside or outside watch_dir."""
        try:
            rel = self.audit_dir.relative_to(self.watch_dir)
            anchor = rel.parts[0]
            if anchor not in self.ignore_patterns:
                self.ignore_patterns.append(anchor)
        except ValueError:
            pass
        audit_str = str(self.audit_dir)
        if audit_str not in self.ignore_patterns:
            self.ignore_patterns.append(audit_str)
        # Also ignore the base audit dir
        base_str = str(self.base_audit_dir)
        if base_str not in self.ignore_patterns:
            self.ignore_patterns.append(base_str)

    def is_ignored(self, path: Path) -> bool:
        # Explicit audit dir checks first
        try:
            path.relative_to(self.audit_dir)
            return True
        except ValueError:
            pass
        try:
            path.relative_to(self.base_audit_dir)
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
        return _safe_dir_name(self.watch_dir)


settings = Settings()