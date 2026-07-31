Shadow-Core Sentinel: MCP Filesystem Telemetry

Shadow-Core Sentinel is a high-performance, event-driven filesystem monitoring service built for the Model Context Protocol (MCP). It provides AI agents with a real-time, cryptographically verified audit trail of all file activity.

Key Features

Non-Blocking Architecture: Offloads hashing to a thread pool to prevent OS event drops.

Cryptographic Verification: Every event is recorded with a SHA-256 hash for integrity.

Context Optimized: Reduces LLM token costs by providing structured event logs instead of raw file dumps.

Dual-Mode Audits: Supports both chronological event streams and full point-in-time disk snapshots.

Global Silence Guard: Automatically ignores high-noise directories (node_modules, .git, etc.) to prevent token blowout.

Multi-Project Watches: Watches several projects at once, each with its own database and audit directory. The dashboard (http://127.0.0.1:7654) has a tab per project; selecting one is a client-side view change, so two browser windows can view two projects without disturbing each other or another session.

Idle Suspension With Gap Recovery: A project with no activity suspends itself rather than being watched forever. Suspension is not removal -- history stays intact and the next prompt resumes it. Changes made while suspended are reconstructed from a SHA-256 comparison and written into the trail marked as detected-on-resume, so the period Sentinel was not watching is visible rather than silently missing.

Atomic-Write Aware: Editors that write via a temp file and rename record as a single MODIFIED of the real path, with no phantom DELETE of the file they just replaced.

Installation

Clone the repository.

Install dependencies: pip install -r requirements.txt

Add to your MCP Config (e.g., Claude Desktop or VS Code):

{
  "mcpServers": {
    "shadow-core-sentinel": {
      "command": "python",
      "args": ["path/to/main.py"]
    }
  }
}
