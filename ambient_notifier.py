"""
ambient_notifier.py — Shadow-Core Sentinel plugin
Subscribes to Sentinel's audit event stream, scores changed files
against the DayDream Global Synapse, and writes signals to the
per-project ambient inbox.json for DayDream to push to the IDE.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING
from models import SentinelState

if TYPE_CHECKING:
    from models import AuditEvent

log = logging.getLogger("sentinel.ambient")

# ─── Config (inherits DayDream env vars if both share the same env) ───────────

GLOBAL_SYNAPSE_PATH  = os.getenv("GLOBAL_SYNAPSE_PATH",  str(Path.home() / ".shadow_core" / "global_synapse"))
AMBIENT_SCORE_THRESH = float(os.getenv("AMBIENT_SCORE_THRESH", "0.55"))
IDLE_DEBOUNCE_SEC    = float(os.getenv("IDLE_DEBOUNCE_SEC",    "5.0"))
IDLE_COOLDOWN_SEC    = float(os.getenv("IDLE_COOLDOWN_SEC",    "20.0"))

# ─── Shared path helpers (mirrors shadow_paths.py in DayDream) ────────────────

def _project_hash(watch_path: str) -> str:
    return hashlib.sha256(os.path.abspath(watch_path).encode()).hexdigest()[:12]

def _shadow_dir(watch_path: str) -> Path:
    base = Path.home() / ".shadow_core" / "projects" / _project_hash(watch_path)
    base.mkdir(parents=True, exist_ok=True)
    return base

def _inbox_path(watch_path: str) -> Path:
    return _shadow_dir(watch_path) / "inbox.json"

def _agent_state_path(watch_path: str) -> Path:
    return _shadow_dir(watch_path) / "agent_state.json"

def _hold_buffer_path(watch_path: str) -> Path:
    return _shadow_dir(watch_path) / "hold_buffer.json"

# ─── Synapse scorer (read-only ChromaDB query) ────────────────────────────────

_embed_model   = None
_embed_lock    = threading.Lock()
_synapse_cache = None
_synapse_lock  = threading.Lock()


def _get_embed_model():
    global _embed_model
    with _embed_lock:
        if _embed_model is None:
            try:
                from sentence_transformers import SentenceTransformer
                _embed_model = SentenceTransformer("all-MiniLM-L6-v2")
                log.info("Ambient: embedding model loaded")
            except ImportError:
                log.warning(
                    "sentence-transformers not installed. "
                    "Ambient scoring disabled — all signals will use baseline score. "
                    "pip install sentence-transformers chromadb"
                )
        return _embed_model


def _get_synapse():
    global _synapse_cache
    with _synapse_lock:
        if _synapse_cache is None:
            try:
                import chromadb
                from chromadb.utils import embedding_functions
                client = chromadb.PersistentClient(path=GLOBAL_SYNAPSE_PATH)
                ef = embedding_functions.SentenceTransformerEmbeddingFunction(
                    model_name="all-MiniLM-L6-v2"
                )
                _synapse_cache = client.get_or_create_collection(
                    "global_synapse", embedding_function=ef
                )
                log.info("Ambient: Global Synapse connected (%d skills)", _synapse_cache.count())
            except Exception as e:
                log.warning("Ambient: Synapse unavailable (%s) — using baseline scores", e)
        return _synapse_cache


def _score_content(content: str) -> tuple[float, list[str]]:
    """
    Score file content against Global Synapse.
    Returns (score, [relevant_skill_previews]).
    """
    if not content.strip():
        return 0.0, []

    model = _get_embed_model()
    if model is None:
        raise RuntimeError("Embedding model unavailable")

    synapse = _get_synapse()
    if synapse is None or synapse.count() == 0:
        return 0.0, []
    try:
        import numpy as np
        content_vec = model.encode(content).tolist()

        results = synapse.query(
            query_embeddings=[content_vec],
            n_results=min(3, synapse.count()),
            include=["documents", "embeddings"],
        )
        docs = results.get("documents", [[]])[0]
        embs = results.get("embeddings", [[]])[0]

        if not embs:
            return 0.30, []

        def cosine(a, b):
            va, vb = np.array(a, dtype=float), np.array(b, dtype=float)
            na, nb = np.linalg.norm(va), np.linalg.norm(vb)
            return float(np.dot(va, vb) / (na * nb)) if na > 0 and nb > 0 else 0.0

        best_score = max(cosine(content_vec, e) for e in embs)

        # Extract titles from top matching skills
        titles = []
        for doc in docs[:2]:
            for line in doc.splitlines():
                if line.startswith("Title:"):
                    titles.append(line.replace("Title:", "").strip())
                    break

        return round(min(1.0, best_score), 3), titles

    except Exception as e:
        log.warning("Ambient: scoring error: %s", e)
        return 0.0, []

def _read_file_content(path: Path, max_bytes: int = 8192) -> str:
    """Read file content safely, capped at max_bytes."""
    try:
        return path.read_bytes()[:max_bytes].decode("utf-8", errors="replace")
    except Exception:
        return ""


# ─── Inbox writer ─────────────────────────────────────────────────────────────

def _is_agent_busy(watch_path: str) -> bool:
    state = _agent_state_path(watch_path)
    if not state.exists():
        return False
    try:
        return json.loads(state.read_text()).get("status") == "executing"
    except Exception:
        return False


def _write_inbox(watch_path: str, signal: dict):
    """Write signal to inbox. Buffers if agent is busy."""
    inbox  = _inbox_path(watch_path)

    if _is_agent_busy(watch_path):
        buf_path = _hold_buffer_path(watch_path)
        try:
            existing = json.loads(buf_path.read_text()) if buf_path.exists() else []
            buf_path.write_text(json.dumps((existing + [signal])[-3:], indent=2))
            log.info("Ambient: signal buffered (agent busy)")
        except Exception as e:
            log.error("Ambient: buffer write failed: %s", e)
        return

    try:
        inbox.write_text(json.dumps({
            "schema_version": 1,
            "generated_at": time.time(),
            "agent_consumed_at": None,
            "source": "sentinel",
            "signals": [signal],
        }, indent=2))
        log.info("Ambient: signal written → %s [score=%.2f]",
                 signal["id"], signal["semantic_score"])
    except Exception as e:
        log.error("Ambient: inbox write failed: %s", e)


# ─── Batching debounce (Sentinel already debounces at 0.5s — this is the ──────
#     higher-level 5s idle gate before Synapse scoring is triggered) ─────────

class AmbientNotifier:
    """
    Attaches to Sentinel's subscriber chain.
    Batches events over IDLE_DEBOUNCE_SEC, then scores the changed
    files against the Global Synapse. Writes inbox.json if relevant.
    """

    def __init__(self, watch_path: str):
        self.watch_path = watch_path
        self._lock       = threading.Lock()
        self._batch: dict[str, "AuditEvent"] = {}  # path → latest event
        self._timer      = None
        self._last_fire  = 0.0
        self.state       = SentinelState.OK
        self._recovery_active = False
        log.info("AmbientNotifier ready for %s", watch_path)

    def _trigger_recovery(self):
        with self._lock:
            if self._recovery_active:
                return
            self._recovery_active = True
            self.state = SentinelState.DEGRADED
            self._batch.clear()

        log.warning("Sentinel: embedding model unavailable — ambient loop paused, starting recovery thread")

        def recovery_worker():
            backoff = 2.0
            while self._recovery_active:
                time.sleep(backoff)
                try:
                    # Force model reload
                    global _embed_model
                    with _embed_lock:
                        _embed_model = None
                    if _get_embed_model() is not None:
                        log.info("Ambient: Model recovered. Resuming ambient loop.")
                        with self._lock:
                            self.state = SentinelState.OK
                            self._recovery_active = False
                        break
                except Exception as e:
                    log.debug("Ambient: Recovery attempt failed: %s", e)
                backoff = min(backoff * 2.0, 60.0)

        threading.Thread(target=recovery_worker, daemon=True, name="AmbientRecovery").start()

    def on_event(self, event: "AuditEvent") -> None:
        """Called by Sentinel's _emit for every audit event."""
        if self.state == SentinelState.DEGRADED:
            return

        path_str = str(event.src_path)
        with self._lock:
            self._batch[path_str] = event
            if self._timer:
                self._timer.cancel()
            self._timer = threading.Timer(IDLE_DEBOUNCE_SEC, self._dispatch)
            self._timer.start()

    def _dispatch(self):
        with self._lock:
            now = time.time()
            if now - self._last_fire < IDLE_COOLDOWN_SEC:
                self._batch.clear()
                return
            batch = dict(self._batch)
            self._batch.clear()
            self._last_fire = now

        if not batch:
            return

        # Score the most recently changed meaningful file
        scored: list[tuple[float, str, "AuditEvent", list[str]]] = []
        for path_str, event in batch.items():
            src = Path(path_str)
            # Skip binary / build artefacts quickly
            suffix = src.suffix.lower()
            if suffix in {".pyc", ".pyo", ".exe", ".dll", ".so", ".obj", ".o",
                          ".db", ".sqlite", ".lock", ".cache"}:
                continue
            if not src.exists():
                continue

            content = _read_file_content(src)
            if not content.strip():
                continue

            # Fail-Closed check: if embedding model failed
            if _get_embed_model() is None:
                self._trigger_recovery()
                return

            try:
                score, skill_titles = _score_content(content)
            except RuntimeError:
                self._trigger_recovery()
                return

            scored.append((score, path_str, event, skill_titles))

        if not scored:
            return

        # Pick highest scoring file
        scored.sort(key=lambda x: x[0], reverse=True)
        best_score, best_path, best_event, skill_titles = scored[0]

        log.info("Ambient: batch scored — best=%.2f (%s files) path=%s",
                 best_score, len(batch), Path(best_path).name)

        if best_score < AMBIENT_SCORE_THRESH:
            log.info("Ambient: below threshold (%.2f < %.2f), staying silent",
                     best_score, AMBIENT_SCORE_THRESH)
            return

        # Build the suggestion message
        if skill_titles:
            skill_ref = f"I found {len(skill_titles)} relevant pattern(s): **{', '.join(skill_titles)}**."
        else:
            skill_ref = "I found a relevant pattern in the Global Synapse."

        files_changed = len(batch)
        file_noun     = "file" if files_changed == 1 else "files"

        message = (
            f"Detected changes in `{Path(best_path).name}` "
            f"({files_changed} {file_noun} total, hash verified). "
            f"{skill_ref} Should I apply it here?"
        )

        signal = {
            "id":                   f"sentinel_{int(time.time())}",
            "priority":             "suggest",
            "trigger":              f"SENTINEL_{best_event.kind.value}",
            "path":                 best_path,
            "sha256":               best_event.sha256,
            "semantic_score":       best_score,
            "branch_mode":          "SENTINEL",
            "effective_confidence": best_score,
            "files_in_batch":       files_changed,
            "skill_matches":        skill_titles,
            "suggested_message":    message,
            "created_at":           time.time(),
        }

        _write_inbox(self.watch_path, signal)

    def shutdown(self):
        with self._lock:
            if self._timer:
                self._timer.cancel()
        log.info("AmbientNotifier shutdown")
