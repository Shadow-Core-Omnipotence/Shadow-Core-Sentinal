import logging
import sqlite3
import threading
from pathlib import Path
from typing import List, Optional

from models import AuditEvent, EventKind

logger = logging.getLogger(__name__)

CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    date_key   TEXT NOT NULL,
    kind       TEXT NOT NULL,
    src_path   TEXT NOT NULL,
    dest_path  TEXT,
    sha256     TEXT,
    watch_path TEXT
);
CREATE INDEX IF NOT EXISTS idx_date_key   ON events (date_key);
CREATE INDEX IF NOT EXISTS idx_watch_path ON events (watch_path);
"""

# Migration: add watch_path column to existing databases that don't have it
MIGRATE_SQL = """
ALTER TABLE events ADD COLUMN watch_path TEXT;
"""


class EventStore:
    def __init__(self, db_path: Path, watch_path: Optional[str] = None) -> None:
        self._path = db_path
        self._watch_path = watch_path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.executescript(CREATE_TABLE)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """Add watch_path column if missing (upgrade from older DB)."""
        try:
            self._conn.execute("SELECT watch_path FROM events LIMIT 1")
        except sqlite3.OperationalError:
            try:
                self._conn.execute(MIGRATE_SQL)
                self._conn.commit()
                logger.info("Migrated DB: added watch_path column")
            except Exception as e:
                logger.error(f"Migration error: {e}")

    def set_watch_path(self, watch_path: str) -> None:
        """Update the watch path tag applied to new events."""
        self._watch_path = watch_path

    def insert(self, event: AuditEvent) -> None:
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO events "
                    "(ts, date_key, kind, src_path, dest_path, sha256, watch_path) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (
                        event.timestamp.isoformat(),
                        event.date_key(),
                        event.kind.value,
                        str(event.src_path),
                        str(event.dest_path) if event.dest_path else None,
                        event.sha256,
                        self._watch_path,
                    ),
                )
                self._conn.commit()
            except Exception as e:
                logger.error(f"DB insert error: {e}")

    def query_by_date(self, date_key: str) -> List[dict]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT ts, kind, src_path, dest_path, sha256, watch_path "
                "FROM events WHERE date_key=? ORDER BY id",
                (date_key,),
            )
            return [
                {
                    "ts": r[0], "kind": r[1], "src_path": r[2],
                    "dest_path": r[3], "sha256": r[4], "watch_path": r[5]
                }
                for r in cur.fetchall()
            ]

    def query_since(self, since_iso: str, limit: int = 200,
                    include_hashes: bool = True) -> List[dict]:
        """Events newer than `since_iso`, newest first.

        WHY THIS EXISTS: query_by_date returns a whole DAY. On a busy project
        that is ~20,000 rows for a single date — measured 19,936 on 2026-06-02
        — which is unusable as an answer to "did my edit land?". A task-sized
        window is tens of rows, not tens of thousands.

        Timestamps are stored as `datetime.isoformat()`, which sorts
        lexicographically PROVIDED every row shares an offset. They do: events
        are written with UTC timestamps. A plain string comparison is therefore
        both correct and index-friendly here.

        `include_hashes=False` drops the SHA from the response. It is 64 chars
        per row and is rarely needed to answer "what changed" — the kind and
        path already say that. It stays in the DATABASE either way; this only
        controls what is handed back.
        """
        cols = "ts, kind, src_path, dest_path, watch_path"
        with self._lock:
            cur = self._conn.execute(
                f"SELECT {cols}, sha256 FROM events "
                "WHERE ts > ? ORDER BY id DESC LIMIT ?",
                (since_iso, int(limit)),
            )
            out = []
            for r in cur.fetchall():
                row = {
                    "ts": r[0], "kind": r[1], "src_path": r[2],
                    "dest_path": r[3], "watch_path": r[4],
                }
                if include_hashes:
                    row["sha256"] = r[5]
                out.append(row)
            return out

    def count_since(self, since_iso: str) -> int:
        """How many events since `since_iso` — the cheap question to ask first."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT COUNT(*) FROM events WHERE ts > ?", (since_iso,))
            return cur.fetchone()[0]

    def total_count(self) -> int:
        with self._lock:
            cur = self._conn.execute("SELECT COUNT(*) FROM events")
            return cur.fetchone()[0]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
