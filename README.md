Shadow-Core Sentinel: MCP Filesystem Telemetry

Shadow-Core Sentinel is a high-performance, event-driven filesystem monitoring service built for the Model Context Protocol (MCP). It provides AI agents with a real-time, cryptographically verified audit trail of all file activity.

Key Features

Non-Blocking Architecture: Offloads hashing to a thread pool to prevent OS event drops.

Cryptographic Verification: Every event is recorded with a SHA-256 hash for integrity.

Context Optimized: Reduces LLM token costs by providing structured event logs instead of raw file dumps.

Dual-Mode Audits: Supports both chronological event streams and full point-in-time disk snapshots.

Global Silence Guard: Automatically ignores high-noise directories (node_modules, .git, etc.) to prevent token blowout.

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
