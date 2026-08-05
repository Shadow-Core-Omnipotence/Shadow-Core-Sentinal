import logging
import sqlite3
import threading
from pathlib import Path
from typing import List, Optional

from models import AuditEvent

logger = logging.getLogger(__name__)

# Schema setup runs in THREE ordered steps, and the order is load-bearing.
#
# These used to be one script: the table plus both indexes, executed before
# `_migrate()`. On a database predating the `watch_path` column that is fatal —
# `CREATE TABLE IF NOT EXISTS` is a no-op because the table is already there,
# then `CREATE INDEX ... ON events (watch_path)` raises
# "no such column: watch_path" and the constructor dies before the migration
# that would have added it ever runs. Every pre-migration database was therefore
# unopenable, and the upgrade path advertised by `_migrate` could not execute.
# (dashboard-rs reads such files read-only and adapts to the older schema, so
# they demonstrably still exist.)
#
# Table, then column, then indexes.
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
"""

CREATE_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_date_key   ON events (date_key);
CREATE INDEX IF NOT EXISTS idx_watch_path ON events (watch_path);
CREATE INDEX IF NOT EXISTS idx_ts         ON events (ts);
"""

# Migration: add watch_path column to existing databases that don't have it
MIGRATE_SQL = """
ALTER TABLE events ADD COLUMN watch_path TEXT;
"""


class EventStore:
    def __init__(self, db_path: Path, watch_path: Optional[str] = None) -> None:
        self._path = db_path
        self._watch_path = watch_path
        self._failed_writes = 0
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._configure()
        self._conn.executescript(CREATE_TABLE)
        self._migrate()
        self._conn.executescript(CREATE_INDEXES)
        self._conn.commit()

    def _configure(self) -> None:
        """WAL, so a reader never blocks the writer and a commit is cheaper.

        Every event costs a commit, and a busy project records tens of thousands
        a day (measured: 19,936 on one date). Under the default rollback journal
        that is one full fsync each, and it also means the read-only dashboard
        can be locked out mid-write.

        `synchronous=NORMAL` is the honest setting for WAL here: durable against
        a process crash, and on an OS crash the most that can be lost is the
        tail of the log. That loss is recoverable in a way a missing row is not
        — the tree is still on disk, and a suspend/resume rescan reconstructs
        the difference. Both pragmas are best-effort: an older SQLite or a
        filesystem that will not take WAL falls back to the previous behaviour
        rather than refusing to open the store.
        """
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.Error as exc:
            logger.warning("Could not apply WAL pragmas to %s: %s", self._path, exc)

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
                raise

    @staticmethod
    def _row(event: AuditEvent, watch_path: Optional[str]) -> tuple:
        return (
            event.timestamp.isoformat(),
            event.date_key(),
            event.kind.value,
            str(event.src_path),
            str(event.dest_path) if event.dest_path else None,
            event.sha256,
            watch_path,
        )

    def insert(self, event: AuditEvent) -> bool:
        """Record one event. Returns False if the row did not land.

        This used to swallow the exception and return None, so a disk-full,
        locked-database or schema error was invisible: the caller went on to
        append the same event to the markdown trail, leaving two records of the
        same period that disagree, with nothing anywhere saying which is short.
        For a tool whose entire claim is "this is what actually happened on
        disk", a silently dropped row is the worst possible failure.

        The exception is still caught — one bad row must not kill the observer
        thread and stop monitoring altogether — but it is now COUNTED, and the
        count is surfaced by `sentinel_status` and `/health`.
        """
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO events "
                    "(ts, date_key, kind, src_path, dest_path, sha256, watch_path) "
                    "VALUES (?,?,?,?,?,?,?)",
                    self._row(event, self._watch_path),
                )
                self._conn.commit()
                return True
            except Exception as e:
                self._failed_writes += 1
                logger.error("DB insert error (%d failed so far) for %s: %s",
                             self._failed_writes, event.src_path, e)
                return False

    def insert_many(self, events: List[AuditEvent]) -> int:
        """Record a batch in ONE transaction. Returns how many landed.

        Gap reconstruction replays an entire suspension's worth of changes in a
        tight loop; at one commit per row that is one fsync per file on a diff
        that can cover a whole tree. The batch is atomic — either the resumed
        window is recorded or it is not, which is the right granularity for a
        report that claims to describe exactly that window.

        Falls back to per-row inserts if the batch fails, so one unserialisable
        event costs its own row rather than the entire reconstruction.
        """
        if not events:
            return 0
        with self._lock:
            try:
                self._conn.executemany(
                    "INSERT INTO events "
                    "(ts, date_key, kind, src_path, dest_path, sha256, watch_path) "
                    "VALUES (?,?,?,?,?,?,?)",
                    [self._row(e, self._watch_path) for e in events],
                )
                self._conn.commit()
                return len(events)
            except Exception as exc:
                self._conn.rollback()
                logger.warning(
                    "Batch insert of %d event(s) failed (%s) — retrying row by row",
                    len(events), exc)

        written = 0
        for event in events:
            if self.insert(event):
                written += 1
        return written

    @property
    def failed_writes(self) -> int:
        """Rows this store was asked to write and could not.

        Non-zero means the audit trail is INCOMPLETE. It is reported rather
        than reset, because the gap it describes does not go away.
        """
        return self._failed_writes

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
