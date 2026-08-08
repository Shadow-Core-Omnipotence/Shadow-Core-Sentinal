# Shadow-Core Sentinel — Technical Debt Audit

**Last audit:** 2026-08-03
**Previous audit:** 2026-05-17 (superseded — see *History* below)

---

## Status

All items from the 2026-08-03 audit are resolved. The suite is 199 tests
(`pytest -m "not slow"`), `ruff check .` is clean, and the service has been
verified running end-to-end: `/health`, the dashboard, pivot, live event
recording, disk snapshot, and graceful shutdown.

---

## Resolved 2026-08-03

### Architectural

| # | Issue | Resolution |
| --- | --- | --- |
| 1 | `state.primary` was assigned only by the dashboard's pivot/rollback, never by `watch_project` — so a session driven purely over MCP left every read tool answering "no project is being watched" while recording worked. Confirmed live: 2 projects watched, `"primary": "None"`, `recent_changes` idle. | `watch_project` sets `primary`; `_primary_entry()` falls back to the sole watched project. `tests/test_primary_selection.py` |
| 2 | `_auto_ignore_audit_dir()` appended `rel.parts[0]` — a bare directory name — to the process-global `ignore_patterns` on every `update_watch_dir`. Measured: one pivot made `is_ignored(.../Shadow-Core Sentinel/main.py)` return True, silently blinding Sentinel to its own repo and any project sharing that path component. | Deleted. Audit output is excluded by containment, which already covered every project. `tests/test_config_ignores.py` |
| 3 | `read_snapshot`/`diff_snapshot` resolved artifacts against `settings.audit_dir` — the global that `watch_project` never updates — while the names came from the per-project builder. | `ReportBuilder.read_artifact_by_name` owns the join and refuses non-filename references. `mcp_server` no longer imports `config`. |
| 4 | Two dashboards: `dashboard.py` (shipped) and `dashboard-rs/` (never built or launched by anything). | `dashboard-rs/` deleted. Its premise — that the daemon serving thread masked a crashed main thread — was verified false: a `daemon=True` thread does not keep the interpreter alive. |
| 5 | `main()` was ~420 lines with ten inline closures and three route handlers, none importable or testable. | Extracted `dashboard_wiring.py` and `http_routes.py`; unified two divergent teardown paths into `_shutdown()`. `main()` is 107 lines. |

### Code-level

| # | Issue | Resolution |
| --- | --- | --- |
| 6 | `config._safe_dir_name` and `watch_registry.safe_project_name` were byte-identical, with a comment warning that divergence would orphan every `sentinel.db`. | Single definition in `watch_registry`; `config` imports it. |
| 7 | The ignore-pruning tree walk existed twice, with different ignore sources. | `build_disk_snapshot` calls `lease.scan_tree`. Snapshot paths are now relative, matching gap reports. |
| 8 | Six unreachable definitions, one of which (`models.SentinelState`) shadowed the live dataclass in `main.py`. | All deleted. |
| 9 | MCP host/port hardcoded at the call site; `--port` silently moved the *dashboard*. | `Settings.mcp_host`/`mcp_port` with env overrides; `--mcp-port`/`--mcp-host`/`--dashboard-port`, with `--port` kept as a deprecated alias. |
| 10 | `EventStore.insert` swallowed every exception and returned None; callers treated it as success and wrote the markdown row anyway, leaving two records that disagree with no signal. | Returns bool, counts `failed_writes`, surfaced on `/health` and `sentinel_status`. |
| 11 | One commit — one fsync — per filesystem event. | WAL + `synchronous=NORMAL`; `insert_many` for gap reconstruction, degrading to per-row on failure. |
| 12 | Four builder-backed tools dereferenced `state.builder` unguarded and raised `AttributeError` in the normal idle state, while two others returned a clean result. | One `IDLE_RESULT`, applied uniformly. |
| 13 | Process-wide rate alerts rendered inside a per-project tab with no label. | `alerts_scope: "process"` in the payload and "all projects" in the UI. Per-project attribution was rejected deliberately: alerting fires before routing, and moving it behind the hashing pool would delay burst detection. |
| 14 | `do_POST` parsed the body before route dispatch with no guard; malformed JSON dropped the connection with no response. | Explicit 400/413 with a bounded drain. `tests/test_dashboard_api.py` |
| 15 | Seven function-local imports, two of which hid a real `mcp_server` → `observer` dependency. | Hoisted to module scope. |
| 16 | `str(state.primary)` emitted the string `"None"` for an unset primary. | `state.primary_path`, serialised as JSON null. |

### Retention — added 2026-08-05

The trail had no retention policy of any kind and had reached 252 MB, 38% of it
a dead project created by a typo in a watched path. `retention.py` now flushes
all recorded audit data at startup (`SENTINEL_FLUSH_ON_START`, default true),
which bounds disk to a single run. The trade is explicit and one-way:
cross-restart forensics is gone, so `list_audit_dates`, `get_daily_report` and
`query_events` can only answer about the current run.

Deletion is guarded, because it acts on a path that comes from an environment
variable: only directories holding a `sentinel.db` or Sentinel's own markdown
(or empty project directories) are removed, an audit root within three path
components of a filesystem root is refused outright, and everything else is left
alone and logged. 24 tests, most of them proving it refuses.

Two defects found while building it:

| Issue | Resolution |
| --- | --- |
| The flush's own dry run showed loose `audit-*.md`/`snapshot-*.md` at the audit root — the ORIGINAL single-watch layout — were not matched, so 13 files would have survived every flush forever and "flush everything" would have been quietly false. | Widened the loose-file patterns, including the `-wal`/`-shm` sidecars WAL introduced. 37 items, 0 skipped. |
| The orchestrator's new health check used a 15s window. A PyInstaller onefile extracts ~50 MB per launch (~12s warm, slower cold), so a healthy server was reported as failed. A false alarm on a startup check is as bad as no check — it teaches you to ignore it. | Window raised to 90s; success still returns as soon as `/health` answers, and the wait is now reported. |

### Found during the work, not in the report

| Issue | Resolution |
| --- | --- |
| **The schema migration was dead code.** `CREATE INDEX ... ON events (watch_path)` ran *before* `_migrate()` added that column, so opening any pre-migration database raised `no such column: watch_path` and the advertised upgrade path could never execute. | Split into table → migrate → indexes. `tests/test_storage.py` |
| The 413 response replied without draining the request body, resetting the client mid-send — the caller got `ConnectionAborted` instead of the error explaining the problem. | Bounded `_drain` before responding. |

### Testing and packaging

| # | Issue | Resolution |
| --- | --- | --- |
| 17 | No test imported `mcp_server`, `main` or `dashboard`; `Settings`, `EventStore._migrate`, `ReportBuilder` and `hasher`'s retry branch were uncovered. | 65 → 199 tests. New: `test_primary_selection`, `test_config_ignores`, `test_storage`, `test_report_builder`, `test_dashboard_api`, `test_dashboard_wiring`, `test_hasher`. |
| 18 | `pytest` was required by every test file and declared in no manifest; no `pyproject.toml`; Python version recorded only in the name of a virtualenv directory. | `pyproject.toml` with `requires-python = ">=3.11"` and a `[dev]` extra. `pytest.ini` folded in. |
| 19 | `dashboard-rs/` carried a second dependency tree (axum, tokio, rusqlite-bundled) plus a Rust and C toolchain requirement, for a binary nothing built. | Deleted with the component. |
| 20 | README told users to register a stdio `"command"`/`"args"` server, which hangs — `mcp_config.json` existed specifically to say so. | Rewritten: SSE config, flags, environment variables, endpoints. |
| 21 | Nothing in the repo installed or started the service, though `mcp_config.json` and `SENTINEL.md` both assert it starts at logon. | `INSTALL.md`, `install.ps1`, `uninstall.ps1`. The existing mechanism was found during the work and is now documented: an external scheduled task running an external startup script outside this repo. `install.ps1` is for a standalone install and must NOT be used alongside it — see the warning at the top of INSTALL.md. |

---

## Open

Nothing outstanding from this audit.

Known and accepted:

- **One default project across sessions.** `watch_project` moves the shared
  `primary`, so two sessions on two projects share one default view. This cannot
  be resolved without per-session identity, which MCP does not provide. Writes
  are unaffected — every project records to its own database regardless.
- **Rate alerts are process-wide**, by design (see #13). Labelled as such.

---

## History

The 2026-05-17 audit has been superseded. It described `ambient_notifier.py`
(deleted in `1ff0277`), a `.venv` alongside `.venv311` (only the latter exists),
a hardcoded sibling-project `pathex` in the spec (fixed), a mixed
`fastmcp`/`mcp.server` split (fixed), and stated "No `tests/` directory visible
at repo root". Its issue numbers were cited as live justification by
`dashboard-rs`, which is how a false premise outlived the document that carried
it — the reason this file is now rewritten rather than appended to. It remains
in git history.
