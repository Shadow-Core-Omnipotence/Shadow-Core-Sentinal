import logging
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Deque, Dict, List, Optional, Tuple

from watchdog.events import (
    FileCreatedEvent,
    FileDeletedEvent,
    FileModifiedEvent,
    FileMovedEvent,
    FileSystemEventHandler,
)
from watchdog.observers import Observer

from config import settings
from hasher import sha256_of
from models import AuditEvent, EventKind

logger = logging.getLogger(__name__)

# Debounce window in seconds — suppress duplicate MODIFIED events for same file
DEBOUNCE_SECONDS = 0.5


class AlertManager:
    """Fires a callback if event rate exceeds threshold."""

    def __init__(self, max_events: int, window_seconds: int, on_alert: Callable[[int, int], None]) -> None:
        self._max = max_events
        self._window = window_seconds
        self._on_alert = on_alert
        self._timestamps: Deque[float] = deque()
        self._lock = threading.Lock()

    def record(self) -> None:
        now = time.monotonic()
        with self._lock:
            self._timestamps.append(now)
            cutoff = now - self._window
            while self._timestamps and self._timestamps[0] < cutoff:
                self._timestamps.popleft()
            count = len(self._timestamps)
        if count >= self._max:
            try:
                self._on_alert(count, self._window)
            except Exception as e:
                logger.error(f"Alert callback error: {e}")


class AuditEventHandler(FileSystemEventHandler):
    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self._ring: Deque[AuditEvent] = deque(maxlen=settings.max_memory_events)
        self._subscribers: List[Callable[[AuditEvent], None]] = []
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="hasher")
        self._alert_manager = AlertManager(
            max_events=settings.alert_event_count,
            window_seconds=settings.alert_window_seconds,
            on_alert=self._on_alert,
        )
        self._alerts: Deque[dict] = deque(maxlen=50)
        self._watch = None

        # FIX: debounce tracker — path -> last seen time
        self._debounce: Dict[str, float] = {}
        self._debounce_lock = threading.Lock()

    def _is_debounced(self, path: str) -> bool:
        """Returns True if this path was seen within the debounce window."""
        now = time.monotonic()
        with self._debounce_lock:
            last = self._debounce.get(path, 0.0)
            if now - last < DEBOUNCE_SECONDS:
                return True
            self._debounce[path] = now
            # Prune old entries to prevent unbounded growth
            if len(self._debounce) > 1000:
                cutoff = now - 60.0
                self._debounce = {k: v for k, v in self._debounce.items() if v > cutoff}
            return False

    def _on_alert(self, count: int, window: int) -> None:
        msg = f"ALERT: {count} events in {window}s — possible runaway process."
        logger.warning(msg)
        with self._lock:
            self._alerts.append({"count": count, "window": window, "ts": time.time()})

    def recent_alerts(self) -> List[dict]:
        with self._lock:
            return list(self._alerts)

    def subscribe(self, cb: Callable[[AuditEvent], None]) -> None:
        with self._lock:
            self._subscribers.append(cb)

    def recent_events(self, n: int = 50) -> List[AuditEvent]:
        with self._lock:
            return list(self._ring)[-n:]

    def events_for_date(self, date_key: str) -> List[AuditEvent]:
        with self._lock:
            return [e for e in self._ring if e.date_key() == date_key]

    def on_created(self, event: FileCreatedEvent) -> None:
        if event.is_directory:
            return
        path = Path(event.src_path)
        # FIX: early ignore check before touching thread pool
        if settings.is_ignored(path):
            return
        self._alert_manager.record()
        self._executor.submit(self._process, EventKind.CREATED, path)

    def on_modified(self, event: FileModifiedEvent) -> None:
        if event.is_directory:
            return
        path = Path(event.src_path)
        # FIX: early ignore check
        if settings.is_ignored(path):
            return
        # FIX: debounce — drop duplicate MODIFIED events within window
        if self._is_debounced(str(path)):
            return
        self._alert_manager.record()
        self._executor.submit(self._process, EventKind.MODIFIED, path)

    def on_deleted(self, event: FileDeletedEvent) -> None:
        if event.is_directory:
            return
        src = Path(event.src_path)
        if settings.is_ignored(src):
            return
        self._alert_manager.record()
        self._executor.submit(self._process, EventKind.DELETED, src)

    def on_moved(self, event: FileMovedEvent) -> None:
        if event.is_directory:
            return
        src = Path(event.src_path)
        if settings.is_ignored(src):
            return
        self._alert_manager.record()
        self._executor.submit(self._process, EventKind.MOVED, src, Path(event.dest_path))

    def _process(self, kind: EventKind, src: Path, dest: Optional[Path] = None) -> None:
        target = dest if dest else src
        if settings.is_ignored(target):
            return
        self._emit(AuditEvent(kind=kind, src_path=src, dest_path=dest, sha256=sha256_of(target)))

    def _emit(self, event: AuditEvent) -> None:
        with self._lock:
            self._ring.append(event)
            cbs = list(self._subscribers)
        for cb in cbs:
            try:
                cb(event)
            except Exception as e:
                logger.error(f"Subscriber error: {e}")

    def shutdown(self) -> None:
        self._executor.shutdown(wait=True)


def start_observer(handler: AuditEventHandler) -> Observer:
    obs = Observer()
    handler._watch = obs.schedule(handler, str(settings.watch_dir), recursive=settings.recursive)
    obs.start()
    return obs


def start_bare_observer() -> Observer:
    """A running observer with NO watches scheduled yet.

    Multi-watch startup schedules each project through add_watch() instead of
    baking one path in at construction, so the first watch is not special.
    """
    obs = Observer()
    obs.start()
    return obs


def add_watch(observer: Observer, handler: AuditEventHandler, path) -> object:
    """Schedule one more directory. Returns the handle needed to remove it.

    Additive: watchdog keeps every existing watch. The handle matters because
    removal must be precise — `unschedule_all()` would take down every other
    session's project along with this one.
    """
    return observer.schedule(handler, str(path), recursive=settings.recursive)


def remove_watch(observer: Observer, handle) -> bool:
    """Unschedule exactly one watch. False if it was already gone."""
    if handle is None:
        return False
    try:
        observer.unschedule(handle)
        return True
    except Exception as exc:  # noqa: BLE001 — already-removed is not an error
        logger.debug("unschedule failed (likely already removed): %s", exc)
        return False