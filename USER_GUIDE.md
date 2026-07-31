# Shadow-Core Sentinel: User & Integration Guide

Shadow-Core Sentinel is a high-performance filesystem telemetry service for AI agents. This guide covers setup, compilation, and IDE integration.

## 1. Portable Compilation
To create a standalone binary that doesn't require a Python environment:

1. **Setup Environment**:
   ```powershell
   py -3.11 -m venv .venv
   .\.venv\Scripts\activate
   pip install watchdog mcp pyinstaller
   ```

2. **Build Binary**:
   ```powershell
   .\.venv\Scripts\pyinstaller --onefile --name shadow-core-sentinel main.py
   ```
   The executable will be generated at `dist\shadow-core-sentinel.exe`.

## 2. IDE Integration (MCP)
Add the following configuration to your IDE's MCP settings. Replace paths with your actual absolute paths.

### Google Antigravity
**Config Location**: `.antigravity/mcp.json` (workspace) or `%APPDATA%\Google\Antigravity\config.json` (global).

```json
{
  "mcpServers": {
    "shadow-core-sentinel": {
      "command": "E:\\AI Backup Projects\\Shadow-Core Sentinal\\dist\\shadow-core-sentinel.exe",
      "args": [],
      "env": {
        "WATCH_DIR": "E:\\AI Backup Projects\\Your-Project-Path",
        "AUDIT_DIR": "E:\\AI Backup Projects\\Your-Project-Path\\audit_logs"
      }
    }
  }
}
```

### Cursor / Windsurf / VS Code (Cline)
Use the same JSON structure in their respective MCP configuration files:
- **Cursor**: Features -> MCP or `.cursor/mcp.json`.
- **Windsurf**: `~/.codeium/windsurf/mcp_config.json`.
- **VS Code (Cline)**: `cline_mcp_settings.json`.

## 3. Usage Rules (System Prompt)
Inject these rules into your AI agent's system prompt to enable cryptographic verification:

```text
SYSTEM RULE: FILE VERIFICATION
You are equipped with the Shadow-Core Sentinel MCP.

1. At start of task, call `summarise_activity` for recent edit context.
2. After any file write, call `trigger_audit` with `mode="events"`.
3. Verify changes via the returned SHA-256 hashes before proceeding.
```

## 4. Tools Reference
- `list_audit_logs`: Lists all available audit reports.
- `trigger_audit`: Generates a point-in-time disk snapshot or event log.
- `summarise_activity`: Provides high-level event counts for a specific date.
