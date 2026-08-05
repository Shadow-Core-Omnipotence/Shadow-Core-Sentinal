# Shadow-Core Sentinel — MCP Filesystem Telemetry

Sentinel records every file change under a watched directory, with a SHA-256 of
each file, so a change can be **confirmed against what actually happened on
disk** rather than assumed.

It records **what** changed, never whether the change is **correct**. It is a
filesystem oracle, not a semantic one — it will not catch a deleted function, a
wrong value, or a broken test. Linters and tests remain the tools for that.

## Key features

- **Non-blocking** — hashing is offloaded to a thread pool so OS events are not
  dropped while a large file is read.
- **Cryptographically verified** — every event carries a SHA-256, so "did this
  file revert to its original state?" is answerable.
- **Context-optimised** — `recent_changes` answers "did my edit land?" in tens
  of rows. `query_events` takes a whole date, which on a busy project is tens of
  thousands (measured: 19,936 in one day).
- **Multi-project** — several projects watched at once, each with its own
  database and audit directory. Adding a watch never removes another, so two
  sessions cannot silently stop each other's monitoring.
- **Idle suspension with gap recovery** — an inactive project suspends rather
  than being watched forever. Suspension is not removal: history stays intact
  and the next prompt resumes it. Changes made while suspended are reconstructed
  from a SHA-256 comparison and written into the trail marked as
  detected-on-resume, so the unwatched period is *visible* rather than missing.
- **Atomic-write aware** — editors that write via a temp file and rename record
  as a single `MODIFIED` of the real path, with no phantom `DELETE` of the file
  they just replaced.
- **Noise guard** — high-churn directories (`node_modules`, `.git`, `venv`,
  build output) are ignored by default.

## Requirements

Python **3.11**. The pinned versions in `requirements.txt` are verified against
it.

## Install

```bash
pip install -r requirements.txt
```

For development — this is what you need to run the test suite, which
`requirements.txt` alone does not provide:

```bash
pip install -e ".[dev]"
```

## Run

```bash
python main.py
```

Sentinel boots **watching nothing** and stays idle until a session calls
`watch_project`. That is deliberate: it starts with the machine, and coming up
recording a directory nobody asked about means CPU spent hashing and an audit
trail nobody reads.

To start it automatically at logon, see [INSTALL.md](INSTALL.md).

| Flag | Default | Meaning |
| --- | --- | --- |
| `--mcp-port` | `7702` | MCP SSE endpoint |
| `--mcp-host` | `127.0.0.1` | Bind address — loopback is deliberate |
| `--dashboard-port` | `7654` | HTML dashboard |
| `--no-dashboard` | off | Run without the dashboard |
| `--watch PATH` | none | Watch a directory at startup |
| `--log-level` | `INFO` | `DEBUG`/`INFO`/`WARNING`/`ERROR` |

`--port` is a deprecated alias for `--dashboard-port`.

### Environment variables

| Variable | Default | Meaning |
| --- | --- | --- |
| `MCP_PORT` / `MCP_HOST` | `7702` / `127.0.0.1` | MCP SSE endpoint |
| `DASHBOARD_PORT` | `7654` | Dashboard port |
| `DASHBOARD_ENABLED` | `true` | Set `false` to disable the dashboard |
| `AUDIT_DIR` | `./audit_logs` | Where per-project audit data is written |
| `WATCH_DIR` | `./watched` | Startup watch directory |
| `SENTINEL_FLUSH_ON_START` | `true` | **Deletes all recorded audit data at startup.** See below |
| `WATCH_IDLE_TTL_SECONDS` | `3600` | Idle time before a watch suspends; `0` disables |
| `WATCH_SWEEP_SECONDS` | `60` | How often idle watches are checked |
| `LOG_LEVEL` | `INFO` | Logging level |

## Flush on start

`SENTINEL_FLUSH_ON_START` defaults to **true**: every start deletes all
recorded audit data, so a run begins with no history. This completes what
Sentinel already did — it boots watching nothing, and its in-RAM ring starts
empty — and it bounds disk, which nothing else did. Before this the trail had
reached 252 MB with no retention policy at all, 38% of it a dead project
created by a typo in a watched path.

**What you lose, stated plainly:** cross-restart forensics. `list_audit_dates`,
`get_daily_report` and `query_events` can only answer about the current run,
and "what changed while I wasn't looking" — the one question git cannot answer
— is unanswerable across a restart, because the evidence is deleted first. Gap
reconstruction still works, but only across a suspend/resume inside one run.

Set `SENTINEL_FLUSH_ON_START=false` to keep history.

The flush only removes things it recognises as its own: a directory holding a
`sentinel.db` or Sentinel's markdown artifacts, an empty project directory, or
a loose `sentinel.*`/`audit-*.md`/`snapshot-*.md`/`gap-*.md` at the audit root.
Anything else is left alone and logged, and an `AUDIT_DIR` closer than three
path components to a filesystem root is refused outright — a mis-set
`AUDIT_DIR` must not be able to delete source.

## MCP client configuration

Sentinel speaks MCP over **SSE**, not stdio. It is **not spawned by the
client** — it must already be running, and the client connects to it:

```json
{
  "mcpServers": {
    "shadow-core-sentinel": {
      "type": "sse",
      "url": "http://127.0.0.1:7702/sse"
    }
  }
}
```

A `"command"`/`"args"` entry — the stdio spawn form — does **not** work here.
The client launches the process, waits for stdio that never comes, and hangs,
because `main.py` runs `mcp.run(transport="sse", ...)` and serves HTTP instead.

Verify it is up:

```bash
curl http://127.0.0.1:7702/health
```

## Using it

At the start of a session:

```
watch_project(path="<absolute path of the working directory>")
```

Additive and idempotent — it never stops another session's watch, and
re-calling it for an already-watched directory only renews its lease.

Before reporting that a change is complete:

```
recent_changes(minutes=15)
```

Compare *files you intended to change* against *what the filesystem recorded*.
This catches an edit that silently did not land, and files changed that were not
meant to be touched. It is worth most after a build, install, or generated-file
step, where an exit code of 0 is not evidence that a file was written.

### Endpoints

| Endpoint | Purpose |
| --- | --- |
| `GET /health` | Liveness, watch list, and `failed_writes` — non-zero means the trail is incomplete |
| `POST /api/touch` | Keepalive; renews and resumes the watch for a path (localhost only) |
| `POST /admin/shutdown` | Graceful stop without elevated `taskkill` (localhost only) |
| `http://127.0.0.1:7654` | Dashboard, one tab per watched project |

Selecting a dashboard tab is a client-side view change: it does not move the
server's default project or affect another session.

## Tests

```bash
python -m pytest -m "not slow"
```

## Layout

| File | Responsibility |
| --- | --- |
| `main.py` | Startup: build state, wire components, run |
| `mcp_server.py` | The nine MCP tools and two resources |
| `dashboard_wiring.py` | Which project a dashboard request is answered from |
| `dashboard.py` | Dashboard HTTP layer and template |
| `http_routes.py` | `/health`, `/api/touch`, `/admin/shutdown` |
| `observer.py` | watchdog handler: ignore, debounce, atomic-write handling |
| `watch_registry.py` | The watched projects and longest-prefix event routing |
| `lease.py` | Idle suspension, resume, and gap reconstruction |
| `storage.py` | SQLite event store, one per project |
| `report_builder.py` | Markdown audit logs and snapshots |
| `config.py` | Settings and the ignore filter |
