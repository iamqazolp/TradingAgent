# ta-plugin

Standalone Vietnamese stock technical-analysis MCP server.
Drop this folder into any project to get TA capabilities via MCP.

## Quick start

```bash
# 1. Install dependencies
uv sync

# 2. Seed the database (fetches from VietinBank API)
uv run python -m data.ingest --tickers HPG,VNM,FPT --days 500

# 3. Run the MCP server
uv run python -m mcp_server.server
```

## Integrate with your AI agent

Copy the contents of `mcp_config.json` into your agent's MCP configuration.
Adjust paths to match your setup:

```json
{
  "mcpServers": {
    "ta-agent": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "python", "-m", "mcp_server.server"],
      "cwd": "/path/to/ta-plugin",
      "env": {
        "TA_AGENT_DB": "/path/to/ta-plugin/var/ta.sqlite"
      }
    }
  }
}
```

## Attach the reasoning skill

The `skills/technical-analysis/SKILL.md` file teaches your AI agent how to
interpret the JSON output from the MCP tools and render Vietnamese reports.
Load it as a skill/system prompt in your agent framework.

## What's included

| Directory | Purpose |
|---|---|
| `data/` | SQLite store + VietinBank ingestion |
| `indicators/` | Pure computation engine (trend, momentum, volatility, flow, levels, strategies) |
| `mcp_server/` | MCP stdio server wrapping the engine |
| `skills/` | Reasoning skill for the AI agent |

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `TA_AGENT_DB` | `var/ta.sqlite` | Path to the SQLite database |
| `TA_AGENT_AUDIT_LOG` | `logs/tool_calls.jsonl` | Audit log path |
| `TA_AGENT_LOG_LEVEL` | `INFO` | Logging level |
