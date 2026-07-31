import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from config import settings
from hasher import sha256_of
from models import AuditEvent, EventKind

logger = logging.getLogger(__name__)

_EMOJI: Dict[EventKind, str] = {
    EventKind.CREATED:  "🟢",
    EventKind.MODIFIED: "🟡",
    EventKind.DELETED:  "🔴",
    EventKind.MOVED:    "🔵",
}

class ReportBuilder:
    """Writes one project's audit artifacts.

    `watch_path` is passed in rather than read from `settings.watch_dir`.
    A builder is created per watched project and writes into that project's
    audit_dir, but it used to take the directory to inventory — and the path
    printed in every report header — from the global. With two projects
    watched, a disk snapshot requested for project B walked whichever tree
    happened to be primary and filed the result under B, producing an audit
    artifact that is confidently about the wrong project. Falls back to the
    global when not supplied, so single-watch callers are unaffected.
    """

    def __init__(self, audit_dir: Path, watch_path: Optional[Path] = None) -> None:
        self._audit_dir = audit_dir
        self._watch_path = Path(watch_path) if watch_path else None
        self._lock = threading.Lock()

    @property
    def watch_path(self) -> Path:
        return self._watch_path if self._watch_path is not None else settings.watch_dir

    def append_event(self, evt: AuditEvent) -> Path:
        date_key = evt.date_key()
        path = self._audit_dir / f"audit-{date_key}.md"

        with self._lock:
            if not path.exists():
                self._initialise_file(path, date_key)
            self._append_row(path, evt)
        return path

    def build_snapshot(self, events: List[AuditEvent], label: str = "events") -> Path:
        ts = datetime.now(tz=timezone.utc)
        filename = f"snapshot-{ts.strftime('%Y%m%dT%H%M%SZ')}-{label}.md"
        path = self._audit_dir / filename

        lines = [
            f"# Audit Snapshot — {label}",
            "",
            f"> **Generated:** {ts.isoformat(timespec='milliseconds')}Z",
            f"> **Events captured:** {len(events)}",
            f"> **Watched directory:** `{self.watch_path}`",
            "",
            "## Events",
            "",
            "| Timestamp (UTC) | Event | Path / Detail |",
            "| --- | --- | --- |",
        ]

        for evt in events:
            lines.append(self._format_row(evt))

        if not events:
            lines.append("| — | — | *No events recorded* |")

        with self._lock:
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def build_disk_snapshot(self, label: str = "disk") -> Path:
        entries: List[tuple[Path, Optional[str]]] = []
        for root, dirs, files in os.walk(self.watch_path):
            dirs[:] = [d for d in dirs if not settings.is_ignored(Path(root) / d)]
            for name in sorted(files):
                fpath = Path(root) / name
                if not settings.is_ignored(fpath):
                    entries.append((fpath, sha256_of(fpath)))
                
        ts = datetime.now(tz=timezone.utc)
        filename = f"snapshot-{ts.strftime('%Y%m%dT%H%M%SZ')}-{label}.md"
        path = self._audit_dir / filename
        
        lines = [
            f"# Disk State Snapshot — {label}",
            "",
            f"> **Generated:** {ts.isoformat(timespec='milliseconds')}Z",
            f"> **Watched directory:** `{self.watch_path}`",
            f"> **Files inventoried:** {len(entries)}",
            "",
            "## File Inventory",
            "",
            "| Path | SHA-256 |",
            "| --- | --- |",
        ]
        
        for fpath, digest in entries:
            sha = f"`{digest}`" if digest else "*unreadable*"
            lines.append(f"| `{fpath}` | {sha} |")
            
        with self._lock:
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def list_artifacts(self) -> List[Path]:
        # No lock needed: read-only glob + stat. The lock guards concurrent file writes.
        return sorted(self._audit_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)

    def read_artifact(self, date_key: str) -> Optional[str]:
        path = self._audit_dir / f"audit-{date_key}.md"
        with self._lock:
            return path.read_text(encoding="utf-8") if path.exists() else None

    def read_artifact_by_path(self, path: Path) -> Optional[str]:
        with self._lock:
            return path.read_text(encoding="utf-8") if path.exists() else None

    def _initialise_file(self, path: Path, date_key: str) -> None:
        header = "\n".join([
            f"# Folder Audit Log — {date_key}",
            "",
            f"> **Watched directory:** `{self.watch_path}`",
            f"> **Report generated:** {datetime.now(tz=timezone.utc).isoformat(timespec='milliseconds')}Z",
            "",
            "## Events",
            "",
            "| Timestamp (UTC) | Event | Path / Detail |",
            "| --- | --- | --- |",
            ""
        ])
        path.write_text(header, encoding="utf-8")

    def _format_row(self, evt: AuditEvent) -> str:
        emoji = _EMOJI.get(evt.kind, "⚪")
        dest = f" → `{evt.dest_path}`" if evt.dest_path else ""
        sha = f"<br/>SHA-256: `{evt.sha256}`" if evt.sha256 else ""
        return f"| {evt.iso_ts()} | {emoji} `{evt.kind.value}` | `{evt.src_path}`{dest}{sha} |"

    def _append_row(self, path: Path, evt: AuditEvent) -> None:
        row = self._format_row(evt) + "\n"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(row)