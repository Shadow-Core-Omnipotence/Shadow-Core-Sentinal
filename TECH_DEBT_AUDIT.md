# Shadow-Core Sentinel — Technical Debt Audit

**Audit date:** 2026-05-17
**Auditor:** Claude (post-SSE migration session)
**Repo root:** `E:\AI Backup Projects\Shadow-Core Sentinel\`
**Status at audit:** SSE rebuild verified building successfully; port-binding (7702) verification cut off before completion.

---

## Executive Summary

Sentinel is functional but carries notable architectural debt accumulated during the rapid stdio → SSE transport migration. The most severe issue (a FastMCP/raw-Server API mismatch causing silent port-bind failure) was fixed this session, but a cluster of related smells around the mixed `fastmcp` + `mcp.server` package usage, dual virtualenvs, and silent-failure modes will keep biting until addressed.

**Risk level: MEDIUM.** Single-process server, all data is reproducible from the watched filesystem, but observability is poor and several failure modes produce no diagnostic output.

---

## Critical Issues Found This Session

### 1. FastMCP / raw-Server API mismatch (FIXED, verify after restart)

**Location:** `mcp_server.py:147` and `main.py:189-196`

**Problem:** `build_mcp_server()` returned a `fastmcp.FastMCP` instance, but `main.py`'s SSE handler invoked it as if it were a raw `mcp.server.lowlevel.Server`:

```python
async with _sse.connect_sse(...) as (read_stream, write_stream):
    await mcp_server.run(
        read_stream, write_stream,
        mcp_server.create_initialization_options()
    )
```

`FastMCP.run()` signature is `(transport, show_banner, **transport_kwargs)` — passing positional read/write streams is silently incompatible. Result: server process stayed alive (the dashboard thread kept it from exiting), but no MCP port was ever bound. **Zero stderr output** — extremely hard to diagnose.

**Fix applied:** `return mcp._mcp_server` (expose underlying raw `Server`) at `mcp_server.py:147`.

**Verification gap:** Did not confirm port 7702 actually binds after the rebuild — cut off before the final check.

**Action:** Confirm 7702 listening on first run tomorrow; if not, capture stderr via `[System.Diagnostics.Process]::Start` with `RedirectStandardError=true` rather than `Start-Process`.

---

## Architectural Debt

### 2. Two different MCP libraries used in one process

`mcp_server.py` imports `from fastmcp import FastMCP` (standalone fastmcp package, currently 3.2.4)
`main.py` imports `from mcp.server.sse import SseServerTransport` (MCP SDK's bundled server module)

These are sibling implementations of the same protocol with **divergent APIs and config models**. Our session-ending bug came directly from this split — we wrote SSE handler code against MCP SDK conventions but pointed it at a FastMCP object.

**Recommendation:** Pick one. Either:
- (a) Use `fastmcp` end-to-end and call `mcp.run(transport="sse", host=..., port=...)` like Knowledge does — simpler, deletes ~30 lines of main.py
- (b) Use `mcp.server.lowlevel.Server` directly and remove the `fastmcp` dependency

Option (a) is the easier path and aligns Sentinel with Knowledge.

### 3. Dual virtualenvs (`.venv` and `.venv311`)

- `.venv` — missing uvicorn + starlette (incomplete for SSE)
- `.venv311` — has the full stack but `Scripts\pyinstaller.exe` is **broken** (exits 1 even on `--version`)

The rebuild script works around this with conditional logic:
```powershell
if ($b.Pip -like "*python.exe") { & $b.Pip -m PyInstaller $b.Spec ... }
```

**Recommendation:** Delete `.venv`, repair pyinstaller install in `.venv311` (`pip install --force-reinstall pyinstaller`), simplify the rebuild script. Document Python version requirement (3.11) in `requirements.txt` or `pyproject.toml`.

### 4. Dashboard runs on a separate HTTP server in a daemon thread

`dashboard.py:911-921` spins up `http.server.HTTPServer` in a daemon thread on port 7654 (configurable). This is independent of the MCP SSE server on 7702 — different stack, different port, different lifecycle.

Two issues:
- Daemon-thread design means the dashboard masks main-thread exits. When `uvicorn.run()` crashes (as happened this session), the process appears alive because the dashboard kept running — silent failure.
- Two HTTP servers in one process complicates port-conflict diagnosis.

**Recommendation:** Either mount the dashboard as a Starlette route on the same uvicorn app (port 7702 with `/dashboard` path), or move it to a separate sidecar process. Either way, drop the `http.server` stdlib dependency.

### 5. AmbientNotifier silent-degrade design

`ambient_notifier.py:215-244` has a `_trigger_recovery()` path: if the SentenceTransformer model fails to load (no `sentence-transformers` installed, or chromadb unreachable), it logs a warning and degrades the notifier to no-op with an exponential-backoff recovery thread.

This is well-intentioned but brittle:
- The recovery thread runs forever (no max attempts)
- "Degraded mode" only logs at WARN level, easy to miss in production
- No health endpoint exposes whether the notifier is OK / degraded / recovering
- If chromadb's GLOBAL_SYNAPSE_PATH is on a network drive that goes offline, this thread will spin

**Recommendation:** Add a `/health` route to the dashboard exposing `{sentinel: ok, ambient: degraded, observer: ok}`. Cap recovery attempts. Make degraded mode user-visible.

---

## Build / Packaging Debt

### 6. PyInstaller spec has weird `pathex`

`shadow-core-sentinel.spec:19`:
```python
pathex=['E:/AI Backup Projects/Shadow-Core Engineer'],
```

Hardcoded absolute path to a **different project's directory**. Likely intentional — Sentinel's `mcp_server.py:18-21` inserts the Engineer dir into sys.path to import shared `telemetry` module. But baking the absolute path into the spec breaks portability and fails on any other machine.

**Recommendation:** Resolve relative to `SPECPATH` in the spec, like Knowledge does:
```python
_HERE = os.path.dirname(os.path.abspath(SPEC))
_ENGINEER_DIR = os.path.abspath(os.path.join(_HERE, '..', 'Shadow-Core Engineer'))
pathex=[_ENGINEER_DIR]
```

### 7. `hiddenimports = ['watchdog']` is incomplete

Only `watchdog` and `telemetry` are listed. `fastmcp`, `mcp`, `starlette`, `uvicorn`, etc. are pulled in via `collect_all('mcp')` / `collect_all('fastmcp')` which works but is fragile — if a new transitive import is added to `main.py` or `mcp_server.py`, build may succeed but exe will fail at runtime with `ModuleNotFoundError`.

**Recommendation:** Audit the dependency graph and pin known-needed modules explicitly. Add `chromadb`, `sentence_transformers`, `numpy` if AmbientNotifier is to work in the frozen exe.

### 8. Log files committed to repo root

`sentinel_err.log` and `sentinel_out.log` sit in repo root and were both empty at audit time. If anything ever did write to them, they'd accumulate forever. Also `test_event.tmp` is committed and clearly a leftover.

**Recommendation:** Add to `.gitignore`: `*.log`, `*.tmp`, `audit_logs/`, `build/`, `dist/`, `__pycache__/`. Delete the existing files.

---

## Runtime / Operational Debt

### 9. PyInstaller bundles produce no console output when started via `Start-Process -WindowStyle Hidden`

The spec sets `console=True` so a console should be created, but when launched hidden the stdout/stderr streams have nowhere to go and Python's print/logger calls go to the void. This made debugging the FastMCP API mismatch nearly impossible — we eventually had to switch to `[System.Diagnostics.Process]::Start` with explicit stream redirection.

**Recommendation:** In `main.py`, add file-based logging at the top of `main()` so a log file is always written regardless of how the exe is launched:
```python
logging.basicConfig(
    handlers=[logging.FileHandler(settings.audit_dir / "sentinel.log"),
              logging.StreamHandler(sys.stderr)],
    ...
)
```

### 10. Elevated-process kill problem

When Sentinel is started by Task Scheduler at logon (the configured startup path), the process runs at elevated integrity. A non-elevated `taskkill /F /PID <pid>` returns "Access denied." This made every rebuild attempt this session require manual Task Manager intervention.

**Recommendation:** Either:
- Run the startup script under a normal user trigger (not "highest privileges" in Task Scheduler), OR
- Add a `--stop` flag to a wrapper script that signals via a named pipe or an HTTP endpoint (`POST /admin/shutdown` on the dashboard with a local-only auth check)

### 11. ZMQ port hardcoded to 5557 (related to Memory server)

Note: Sentinel itself doesn't use ZMQ, but the Memory server it interoperates with does (`memory_server/server.py:184`). If anything else on the user's machine grabs port 5557, Memory's ZMQ listener thread retries with backoff but never aborts. Not Sentinel's bug, but worth a cross-team fix.

### 12. `bootloader_ignore_signals` not set in Sentinel spec

Unlike Knowledge (which sets `bootloader_ignore_signals=True`), Sentinel's spec leaves it at default (False). This means SIGINT/SIGTERM goes to the PyInstaller bootloader, which may not propagate to the Python child cleanly. Mixed signal-handling design between servers.

**Recommendation:** Standardize on one approach across all 7 servers, document the choice.

---

## Code Smells (Lower Priority)

### 13. `main.py` is doing too much

`main()` is 175 lines and does: argument parsing, settings mutation, logger config, EventStore init, ReportBuilder init, AuditEventHandler wiring, AmbientNotifier wiring, observer startup, dashboard config (with 8 closures defined inline), MCP server build, SSE handler definition, Starlette app construction, uvicorn launch, and cleanup. Hard to test any piece in isolation.

**Recommendation:** Extract dashboard wiring into `dashboard_wiring.py`, MCP transport setup into `sse_app.py`. Aim for a `main()` under 40 lines.

### 14. Mutable-container pattern for pivot

`store_ref = [EventStore(...)]` / `builder_ref = [ReportBuilder(...)]` — using single-element lists as mutable boxes so closures can swap them on `pivot_room`. Works but is non-obvious and a maintenance hazard.

**Recommendation:** Wrap in a small `class SentinelState: def __init__(self): self.store = ...; self.builder = ...` and capture the state object in closures. Reassigning `state.store` is clearer than `store_ref[0]`.

### 15. Test suite status unknown

No `tests/` directory visible at repo root. `pytest` is excluded from PyInstaller build (smart for size), but is there even a test runner config? If tests exist, they should at minimum cover: EventStore round-trip, observer event filtering, AmbientNotifier scoring (with model mocked), pivot_room state transitions.

**Recommendation:** Establish baseline test coverage before any further refactor.

---

## Recommended Sprint Priorities

| # | Item | Effort | Risk if not done |
|---|------|--------|------------------|
| 1 | Verify port 7702 binds after this session's fix | 5 min | Server actually broken |
| 2 | File-based logging in `main()` so failures are visible | 30 min | Continued blind debugging |
| 3 | Pick one MCP library (Issue #2) — convert to pure fastmcp | 2-4 hrs | Future API drift breaks again |
| 4 | Health endpoint for AmbientNotifier degradation | 1 hr | Silent degradation in prod |
| 5 | Spec `pathex` portability fix | 15 min | Anyone else cloning fails |
| 6 | Clean elevated-kill problem (admin shutdown route) | 1 hr | Every redeploy is painful |
| 7 | Delete `.venv`, document `.venv311` only | 15 min | Confusion |
| 8 | `.gitignore` cleanup + remove tracked logs/tmp | 15 min | Repo bloat |
| 9 | Extract `main()` (Issue #13) | 2-3 hrs | Future changes risky |
| 10 | Test suite baseline | 1 day | No regression safety |

---

## Files Modified This Session

- `mcp_server.py` — line 147: `return mcp` → `return mcp._mcp_server`
- `main.py` — entire `main()` body changed from stdio to SSE/uvicorn (in prior session)

## Files Untouched But Reviewed

- `dashboard.py`, `observer.py`, `ambient_notifier.py`, `config.py`, `models.py`, `storage.py`, `report_builder.py`, `differ.py`, `hasher.py`

---

*End of audit. See companion `TECH_DEBT_AUDIT.md` in Shadow-Core Global Knowledge for the Knowledge server review.*
