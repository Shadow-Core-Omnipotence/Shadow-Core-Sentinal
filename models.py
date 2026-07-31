from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

class EventKind(str, Enum):
    CREATED = "CREATED"
    MODIFIED = "MODIFIED"
    DELETED = "DELETED"
    MOVED = "MOVED"

class SentinelState(str, Enum):
    OK = "OK"
    DEGRADED = "DEGRADED"

@dataclass(frozen=True)
class AuditEvent:
    kind: EventKind
    src_path: Path
    timestamp: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))
    dest_path: Optional[Path] = None
    sha256: Optional[str] = None

    def iso_ts(self) -> str:
        return self.timestamp.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    def date_key(self) -> str:
        return self.timestamp.strftime("%Y-%m-%d")