# Installing Shadow-Core Sentinel

Sentinel is a background service. It starts with the machine, serves MCP over
SSE on `127.0.0.1:7702`, and boots **watching nothing** until a session calls
`watch_project`.

Until now nothing in this repository installed or started it, though
`mcp_config.json` and `SENTINEL.md` both describe it as starting at logon. That
step lived only in undocumented machine state. These scripts are it.

---

## Where startup lives

**Sentinel starts itself, from this repository.** `install.ps1` registers a
logon task named **"Shadow-Core Sentinel"** that runs `dist\shadow-core-sentinel.exe`
from this directory. That is the whole mechanism, and it is version-controlled
alongside the code it starts.

It did not used to be. Previously Sentinel was launched by an external startup script
living outside any repository — so the startup path and the code it started could
drift apart, and did: the script passed `--mcp-port` to an exe too old to accept
the flag, and Sentinel silently failed to start.

Sentinel has been removed from that script's server list. **Do not add it back**
while the logon task exists, or two tasks will race for port 7702 and the loser
will exit, leaving a task that looks installed while nothing new is running.

<details>
<summary>External Orchestrators / Legacy Startup</summary>

```
C:\path\to\start-mcp-servers.ps1
```

An external startup script outside any repository.
skipping any already running.


Worth knowing: of those six, **none is configured in `~/.claude.json`**, where
`shadow-core-sentinel` is the only registered MCP server. They start at every
logon and nothing connects to them.

While Sentinel was still in that list its wiring was reviewed and corrected on
2026-08-03. The fixes below stayed in the script and still apply to the
remaining servers:

- **`-Stop` now asks before killing.** It used to `Stop-Process -Force` every
  server. For Sentinel that is a force kill of an audit tool with events in
  flight, a hashing pool mid-work and SQLite stores open. It now POSTs
  `/admin/shutdown`, waits up to 5s, and only forces a process that has stopped
  answering.
- **`WATCH_DIR` removed.** It pointed at Shadow-Core Engineer. Sentinel boots
  idle so it started no watch, but it did set the derived project name, which is
  what the service log is filed under — the whole service's log was landing in
  `audit_logs\Shadow-Core-Engineer\`, a project nobody was watching. It now
  falls under the neutral default.
- **`MCP_PORT` / `MCP_HOST` set explicitly**, so the port `mcp_config.json`
  depends on is visible in the startup path rather than an implicit default.
- **Start is verified.** `Start-Process` returning meant "launched", not
  "serving" — and Sentinel's documented failure mode is a process that starts,
  never binds, and hangs silently. The script now polls `/health` and warns
  loudly if it does not answer within 15s.
- **Working directory pinned** to each exe's own folder; started from a
  scheduled task it was otherwise `System32`.

A timestamped backup of the original sits beside it as
`start-shadow-core-mcp.ps1.bak-*`.

> **Rebuilding the exe:** the script skips a server whose process is already
> running, so replacing `dist\shadow-core-sentinel.exe` does nothing until the
> old process stops. Stop it first (`-Stop`, or the shutdown endpoint), then
> replace the file, then start. Do **not** rename or overwrite the exe while it
> is running — it is a PyInstaller onefile binary and the running process dies
> abruptly, losing every active watch with no gap record.

**Do not run `install.ps1` on that machine.** It would register a second logon
task starting a second Sentinel on the same port; the loser fails to bind and
exits, and you get a task that looks installed while nothing new is running.

To pick up a new build there, just replace `dist\shadow-core-sentinel.exe` and
restart the process:

```powershell
Invoke-RestMethod -Method POST http://127.0.0.1:7702/admin/shutdown
powershell -File "C:\path\to\start-mcp-servers.ps1"
```

`install.ps1` below is for a **fresh machine, or Sentinel on its own** — a
checkout with no orchestrator in front of it. If you later want Sentinel managed
by this repo rather than an external script, remove it from your external startup
script first, then install.

---

## 1. Dependencies

Python **3.11**.

```bash
pip install -r requirements.txt
```

## 2. (Optional) Build a standalone exe

Not required — the installer runs from source if no exe is present. Build it if
you want Sentinel to run without depending on the checkout's virtualenv:

```bash
python -m PyInstaller shadow-core-sentinel.spec
```

Output: `dist\shadow-core-sentinel.exe`.

## 3. Install the logon task

From the repository root, in PowerShell:

```powershell
.\install.ps1
```

This registers a Scheduled Task named **Shadow-Core Sentinel** that runs at
logon. It prefers `dist\shadow-core-sentinel.exe` when present and falls back to
`pythonw.exe main.py`.

Useful switches:

```powershell
.\install.ps1 -Force                      # replace an existing task, no prompt
.\install.ps1 -McpPort 7702 -DashboardPort 7654
.\install.ps1 -Exe "C:\path\to\shadow-core-sentinel.exe"
.\install.ps1 -Python "C:\Python311\pythonw.exe"
```

### It is deliberately not elevated

The task runs at your normal integrity level, not "highest privileges".

Sentinel does not need elevation — it reads and hashes files you can already
read, and binds a loopback port. Running it elevated has a concrete cost that
was previously paid on every rebuild: a non-elevated `taskkill` against an
elevated process returns *Access denied*, so stopping it required Task Manager.
At normal integrity, combined with the shutdown endpoint below, restarting
Sentinel needs no administrator rights at all.

### `pythonw.exe`, not `python.exe`

A console window appearing at every logon for a background service is not
acceptable. Sentinel redirects its own stdout and stderr to
`audit_logs\<project>\sentinel.stdio.log`, so nothing is lost by having no
console — and a windowed host avoids a known deadlock where native writes block
on a pipe with no reader.

## 4. Start and verify

```powershell
Start-ScheduledTask -TaskName "Shadow-Core Sentinel"
```

```bash
curl http://127.0.0.1:7702/health
```

Expected:

```json
{"sentinel":"ok","observer":"ok","watching":[],"watch_count":0,"failed_writes":0}
```

`"watching": []` is correct on a fresh start — Sentinel is idle until asked.

Dashboard: <http://127.0.0.1:7654>

## 5. Register it with your MCP client

Sentinel is **not spawned by the client**. It must already be running; the
client connects to it:

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

A `"command"`/`"args"` entry hangs — the client waits for stdio that never
comes.

---

## Operating it

| Action | Command |
| --- | --- |
| Start | `Start-ScheduledTask -TaskName "Shadow-Core Sentinel"` |
| Stop | `Invoke-RestMethod -Method POST http://127.0.0.1:7702/admin/shutdown` |
| Restart | Stop, then Start |
| Health | `curl http://127.0.0.1:7702/health` |
| Logs | `audit_logs\<project>\sentinel.log` |
| Remove | `.\uninstall.ps1` |

Stop with the endpoint rather than `taskkill`: it stops the observer, drains the
hashing pool and closes every SQLite store, so no in-flight event is lost.

## Uninstalling

```powershell
.\uninstall.ps1
```

Requests a graceful shutdown, then unregisters the task. **Audit data is left in
place** and its location printed — deleting the trail is not an uninstaller's
decision.

---

## Troubleshooting

**Nothing listening on 7702.** Check `audit_logs\<project>\sentinel.log` and
`sentinel.stdio.log`. Historically the failure here was silent: the process
started, never bound the port, and the log was the only evidence.

**Port already in use.** Another Sentinel is probably running. Stop it via the
shutdown endpoint, or install on another port with
`-McpPort`. The MCP client config must match.

**Task exists but nothing runs.** Confirm the task's *Start in* directory is the
repository root. `AUDIT_DIR` and `WATCH_DIR` default to relative paths, so a
task started elsewhere writes its audit trail somewhere unexpected. `install.ps1`
sets this; a hand-made task may not.

**`failed_writes` is non-zero on `/health`.** Rows were dropped and the audit
trail is incomplete — it is no longer safe to read an absence of events as an
absence of changes. Check the log for the underlying SQLite error.

**A project shows as suspended.** Expected after an hour of inactivity. It is
still registered and its history is intact; the next prompt in that project
resumes it, and anything changed while it was down is reconstructed from a
SHA-256 comparison and written in marked as detected-on-resume.
