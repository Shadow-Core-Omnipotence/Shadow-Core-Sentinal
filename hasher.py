import hashlib
import time
from pathlib import Path
from typing import Optional

_CHUNK = 1 << 20  # 1 MiB

def sha256_of(path: Path, max_retries: int = 3) -> Optional[str]:
    """
    Computes SHA-256 with exponential backoff to handle transient 
    OS file locks during rapid IDE write/build cycles.
    """
    backoff = 0.1

    for attempt in range(max_retries + 1):
        try:
            h = hashlib.sha256()
            with path.open("rb") as fh:
                for chunk in iter(lambda: fh.read(_CHUNK), b""):
                    h.update(chunk)
            return h.hexdigest()
            
        except PermissionError:
            if attempt < max_retries:
                time.sleep(backoff)
                backoff *= 2
                continue
            return None
            
        except OSError:
            return None 

    return None