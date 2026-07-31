# Shadow-Core Sentinel — Sprint Task List

**Created:** 2026-05-17
**Source:** `TECH_DEBT_AUDIT.md` (companion file)
**Status legend:** `[ ]` open · `[~]` in progress · `[x]` done · `[!]` blocked

---

## P0 — Must Ship This Sprint

### `[x]` TASK-S01 · Verify port 7702 binds after SSE rebuild  *(done 2026-05-18)*
- **Done:** Port 7702 confirmed LISTENING. `/health` route returns full JSON snapshot:
  ```json
  {"sentinel":"ok","version":"1.2.1","observer":"ok","ambient":"ok",
   "ambient_recovery_active":false,"dashboard":"disabled",
   "watch_dir":"E:\\AI Backup Projects\\Shadow-Core Engineer","total_events":17}
  ```
- **Required fixes during verification (3 hidden issues):**
  1. **`fastmcp` missing from `.venv311`** — installed via `pip install fastmcp==3.2.4`.
  2. **fastmcp 3.3.1 doesn't bundle cleanly with PyInstaller** — downgraded to 3.2.4 (matches Knowledge's working version).
  3. **stdout/stderr deadlock on hidden-window exes** — `mcp_server.run()` hung when uvicorn/FastMCP tried to write to non-existent console. **Fix in main.py:** redirect `sys.stdout`/`sys.stderr` to a `*.stdio.log` file BEFORE any other startup (lines added at top of `main()`). Added `show_banner=False` to `mcp_server.run()` as a belt-and-suspenders measure.
- **Spec hidden imports added** (`shadow-core-sentinel.spec`): explicit `anyio._backends._asyncio`, `uvicorn.loops.*`, `uvicorn.protocols.*`, `uvicorn.lifespan.*`, plus `collect_all('uvicorn')` and `collect_all('anyio')`. Defensive — symptom was "process alive but no port bind, no output".

### `[x]` TASK-S02 · Add file-based logging to `main()`  *(done 2026-05-17)*
- **Done:** Replaced `stream=sys.stderr` `basicConfig` at `main.py:45-49` with `handlers=[FileHandler(settings.audit_dir / "sentinel.log"), StreamHandler(sys.stderr)]`. Log file written with utf-8 encoding so non-ASCII chars (em-dashes) don't crash.
- **Verify after rebuild:** Kill Sentinel, delete `audit_logs/sentinel.log`, start it, log appears within 5s.

---

## P1 — High Value, Plan This Sprint

### `[x]` TASK-S03 · Standardize on one MCP library  *(done 2026-05-17)*
- **Done:** Picked option (a) — pure `fastmcp`. Removed `mcp.server.sse.SseServerTransport`, Starlette routing wiring, and direct `uvicorn.run` from `main.py`. Replaced with `mcp_server.run(transport="sse", host="127.0.0.1", port=7702)`. Reverted `mcp_server.py:147` to `return mcp` (the FastMCP instance). The mixed-library bug class is now impossible.
- **Verify after rebuild:** All 5 (now 6 with extended `sentinel_status`) MCP tools work from Claude Code; port 7702 binds.

### `[x]` TASK-S04 · Add `/health` endpoint exposing component states  *(done 2026-05-17)*
- **Done:** Added via FastMCP's `@mcp_server.custom_route("/health", methods=["GET"])` decorator after `build_mcp_server(state)`. Reports `sentinel`, `version`, `observer`, `ambient` (state name lowercased: `ok`/`degraded`), `ambient_recovery_active`, `dashboard`, `watch_dir`, `total_events`, `as_of`.
- **Verify after rebuild:** `curl http://127.0.0.1:7702/health` returns the JSON shape.

### `[~]` TASK-S05 · Solve elevated-process kill problem  *(code side done 2026-05-17; Task Scheduler config still owed)*
- **Done (code side):** Added `POST /admin/shutdown` localhost-only route. Verifies `request.client.host in ("127.0.0.1", "::1", "localhost")`, returns 403 otherwise. On success spawns a daemon thread that sleeps 500ms (to flush the HTTP response), runs cleanup (`observer.stop()`, `notifier.shutdown()`, `store.close()`), then `os._exit(0)`.
- **Remaining (manual):** Drop "Run with highest privileges" in Task Scheduler's "Shadow Core MCP Servers" task properties so processes start non-elevated. Without this, `taskkill /F` still requires admin for the auto-started exes, even though the new shutdown route handles graceful stops cleanly.
- **Verify after rebuild:** `Invoke-RestMethod -Method POST http://127.0.0.1:7702/admin/shutdown` exits the process cleanly without admin.

### `[x]` TASK-S06 · Fix spec `pathex` portability  *(done 2026-05-17)*
- **Done:** `shadow-core-sentinel.spec` now resolves Engineer dir from `SPEC` location. Raises a clear `FileNotFoundError` at spec-parse time if the expected sibling directory is missing.
- **Verify after rebuild:** `pyinstaller shadow-core-sentinel.spec --noconfirm` succeeds with no hardcoded path edits required.

---

## P2 — Cleanup, Schedule Opportunistically

### `[x]` TASK-S07 · Delete `.venv`, document `.venv311` as canonical  *(done 2026-05-17)*
- **Done:**
  - Deleted `.venv` (was missing uvicorn/starlette anyway).
  - Force-reinstalled pyinstaller in `.venv311` — `pyinstaller.exe --version` now returns `6.20.0` cleanly.
  - Updated `E:\AI Backup Projects\rebuild-shadow-core-mcp.ps1` to point Sentinel at `.venv311\Scripts\pyinstaller.exe` directly (dropped the `python -m PyInstaller` workaround).
- **Documentation note for future cloners:** `requirements.txt` does not yet declare Python 3.11 requirement explicitly. Add a comment header next session.

### `[x]` TASK-S08 · `.gitignore` and tracked-cruft cleanup  *(done 2026-05-17)*
- **Done:** Created `.gitignore` covering build artefacts, venvs, logs, audit_logs, watched/, scratch/, IDE/OS metadata. Deleted tracked cruft: `sentinel_err.log`, `sentinel_out.log`, `test_event.tmp`.
- **Reminder for tomorrow:** Run `git rm --cached` for any of those files if they were committed before this cleanup. Verify `git status` is clean.

### `[x]` TASK-S09 · Standardize signal handling across all 7 servers  *(done 2026-05-17)*
- **Done:** Sentinel spec already had `bootloader_ignore_signals=False` explicit (verified). Knowledge spec changed from `True` → `False` with a comment referencing TASK-K11. Other 5 servers not yet audited but the standard now is **False** (let Python handle signals). Next sprint: spot-check Engineer/Telemetry/DayDream/Ambient/Memory specs to enforce the same.

### `[ ]` TASK-S10 · Move dashboard onto the main uvicorn app
- **Effort:** 2 hours
- **Owner:** _unassigned_
- **Context:** Audit Issue #4. Dashboard runs `http.server.HTTPServer` in a daemon thread on port 7654. Two HTTP servers in one process. The daemon thread is also why a crashed main thread looks "alive."
- **Approach:** Convert `dashboard.py` handlers to Starlette routes mounted at `/dashboard` on the same uvicorn app on port 7702.
- **Acceptance:** Port 7654 no longer bound. `http://127.0.0.1:7702/dashboard` serves the same UI. `DASHBOARD_PORT` env var deprecated.

### `[ ]` TASK-S11 · Refactor `main()` into smaller modules
- **Effort:** 2–3 hours
- **Owner:** _unassigned_
- **Context:** Audit Issue #13. `main()` is 175 lines with 8+ closures defined inline.
- **Extract:**
  - `dashboard_wiring.py` — `get_stats`, `get_events`, `get_snapshots`, `do_pivot`, etc.
  - `sse_app.py` — `SseServerTransport` and Starlette app construction
  - `lifecycle.py` — startup/shutdown sequence
- **Acceptance:** `main()` under 40 lines and only orchestrates.

### `[x]` TASK-S12 · Replace mutable-container pattern with state object  *(done 2026-05-17)*
- **Done:** Introduced `SentinelState` dataclass at top of `main.py`. Replaced `store_ref = [EventStore(...)]` / `builder_ref = [ReportBuilder(...)]` with a single `state = SentinelState(...)` instance. All dashboard closures and the MCP tools now reference `state.store` / `state.builder` etc.
- **Side benefit:** **Fixed a latent bug.** Previously the MCP tools captured the *original* store/builder objects at `build_mcp_server(handler, builder_ref[0], observer, store_ref[0])` call time — meaning `pivot_room` would swap `store_ref[0]` but the tools kept using the old reference forever. Now the tools see pivots immediately because they dereference `state.store` on each call. Updated `build_mcp_server(state)` signature accordingly.
- **Acceptance:** No `_ref[0]` patterns remain in `main.py`.

### `[ ]` TASK-S13 · Audit and pin all hidden imports in spec
- **Effort:** 1 hour
- **Owner:** _unassigned_
- **Context:** Audit Issue #7. `hiddenimports=['watchdog']` is too thin — relies on `collect_all` to find everything else, which silently fails on edge cases.
- **Approach:** Run the built exe through every tool path, watch for `ModuleNotFoundError`, add to `hiddenimports`. Especially audit chromadb / sentence_transformers paths used by AmbientNotifier in its degraded-recovery branch.
- **Acceptance:** Exe runs all 5 tools end-to-end without import errors.

---

## P3 — Long-Term

### `[ ]` TASK-S14 · Establish baseline test suite
- **Effort:** 1 day
- **Owner:** _unassigned_
- **Context:** Audit Issue #15. No `tests/` directory.
- **Minimum coverage:**
  - EventStore: insert + query round-trip
  - AuditEventHandler: event filtering, debounce window
  - AmbientNotifier: scoring path with mocked model
  - pivot_room: state transition correctness
  - ReportBuilder: snapshot generation
- **Acceptance:** `pytest tests/` runs green; coverage >= 40% on `storage.py`, `observer.py`, `ambient_notifier.py`.

### `[x]` TASK-S15 · Cross-team: fix Memory ZMQ port-5557 collision risk  *(done 2026-05-17)*
- **Done:** Added `_MAX_RETRIES = 10` cap in Memory's `_zmq_listener` thread (`server.py:174-205`). After ~30s of backoff retries the thread logs a clear "disabling ZMQ listener. SSE/MCP transport remains active" message to stderr and exits the thread cleanly. The main SSE server is unaffected — Memory still serves MCP on 7701 even if ZMQ on 5557 is unavailable.
- **Verify after Memory rebuild:** Block port 5557 with another listener; start Memory; confirm log message appears within ~30s and 7701 stays bound.

---

## Done This Session (Pre-Sprint)

- `[x]` Discovered FastMCP/raw-Server API mismatch — applied fix `return mcp._mcp_server` in `mcp_server.py:147`
- `[x]` Rebuilt Sentinel exe via `python -m PyInstaller` workaround
- `[x]` Verified exe survives 6-second startup smoke test without crashing
- `[x]` Wrote `TECH_DEBT_AUDIT.md` and this `TASKS.md`

## Done Tonight (After Audit, Before Rebuild)

**First wave (low-risk additive):**
- `[x]` TASK-S02 — File logging added to `main.py`. Writes to `<audit_dir>/sentinel.log` *plus* stderr. UTF-8 encoded.
- `[x]` TASK-S06 — Spec `pathex` now resolves Engineer dir from `SPEC` location (no hardcoded `E:/` path). Raises `FileNotFoundError` at spec-parse if sibling dir is missing.
- `[x]` TASK-S08 — Created `.gitignore`; deleted `sentinel_err.log`, `sentinel_out.log`, `test_event.tmp`.

**Second wave (refactors + cleanups):**
- `[x]` TASK-S03 — **Pure FastMCP.** Removed mixed `mcp.server.sse` + `fastmcp` library code. `main.py` now calls `mcp_server.run(transport="sse", host=..., port=...)` directly. Reverted `mcp_server.py` to `return mcp` (FastMCP instance).
- `[x]` TASK-S04 — `/health` route added via `@mcp_server.custom_route("/health", methods=["GET"])`. Exposes sentinel/observer/ambient/dashboard/watch_dir/total_events.
- `[~]` TASK-S05 — Code side done: `POST /admin/shutdown` (localhost-only) route runs cleanup + `os._exit(0)`. Manual side still owed: drop "highest privileges" in Task Scheduler.
- `[x]` TASK-S07 — Deleted `.venv`; force-reinstalled pyinstaller in `.venv311` (now `6.20.0` working); updated `rebuild-shadow-core-mcp.ps1` to use `.venv311\Scripts\pyinstaller.exe` directly.
- `[x]` TASK-S09 — Sentinel spec already had `bootloader_ignore_signals=False`; Knowledge spec changed True→False (matches Sentinel).
- `[x]` TASK-S12 — Introduced `SentinelState` dataclass. Replaced `_ref = [obj]` pattern. **Fixed latent pivot bug** where MCP tools captured original store/builder forever.
- `[x]` TASK-S15 — Memory ZMQ retries now capped at 10 (~30s); after that, the ZMQ listener thread logs and exits cleanly. SSE/MCP transport on 7701 unaffected.

## Done 2026-05-18 (Day-After Verification)

- `[x]` **TASK-S01** — Sentinel on port 7702 LISTENING. `/health` route returns full component snapshot. Memory on 7701 + 6 other servers also bound. ALL 7 PORTS GREEN.
- Required mid-flight fixes (all baked in to source + spec):
  - Installed `fastmcp==3.2.4` in `.venv311` (was missing; latest 3.3.1 wouldn't bundle).
  - **Major:** stdio redirect at top of `main()` — hidden-window exes had no console; uvicorn/FastMCP banner deadlocked writing to broken stdout. New `sentinel.stdio.log` companion file captures everything.
  - Spec: added explicit `anyio._backends._asyncio`, `uvicorn.loops.*`, `uvicorn.protocols.*`, `uvicorn.lifespan.*`, plus `collect_all('uvicorn')` + `collect_all('anyio')`.

---

## Sprint Burn-Down

**Total open tasks:** 4 (started at 15; closed 11 fully + 1 partial across both sessions)
**P1:** 0 · **P2:** 3 (S10, S11, S13) · **P3:** 1 (S14) · partial: S05 (code done, Task Scheduler config still owed)

**Remaining open:**
- `S05` partial — Manual Task Scheduler config change (drop "highest privileges")
- `S10` — dashboard consolidation onto main uvicorn (2 hrs)
- `S11` — `main()` refactor into smaller modules (2–3 hrs)
- `S13` — partial credit: anyio/uvicorn hidden imports done during verification. Still owed: chromadb/sentence_transformers paths for AmbientNotifier degraded-recovery.
- `S14` — baseline test suite (1 day)

**Recommended next session:** Tackle TASK-S10 (dashboard consolidation) — fold 7654 dashboard onto Sentinel's main uvicorn at `/dashboard`. Frees one port and eliminates daemon-thread-keeps-process-alive false-positive that hid early bugs.
