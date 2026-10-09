# Integration Guide

The agent already exists and runs locally. This project supplies two things:
the `ta-agent` MCP server (data + computation) and `skills/technical-analysis/SKILL.md`
(how to reason over the tool output). Wiring them together is one command.

## 1. Connect the MCP server

Add this to the agent's MCP config. Adjust the three absolute paths.

```json
{
  "mcpServers": {
    "ta-agent": {
      "type": "stdio",
      "command": "/abs/path/to/TAOpenHarness/.venv/bin/python",
      "args": ["-m", "mcp_server.server"],
      "cwd": "/abs/path/to/TAOpenHarness",
      "env": {
        "TA_AGENT_DB": "/abs/path/to/TAOpenHarness/var/ta.sqlite",
        "TA_AGENT_AUDIT_LOG": "/abs/path/to/TAOpenHarness/logs/tool_calls.jsonl",
        "TA_AGENT_LOG_LEVEL": "INFO"
      }
    }
  }
}
```

Rules:

- Use the absolute `.venv/bin/python`, not `uv run`. `uv run` spawns a resolver
  per call, which can time out under a local agent's request budget.
- `cwd` must point at the repo root so `mcp_server` and `indicators` import.
- `TA_AGENT_DB` defaults to `var/ta.sqlite` relative to `cwd`; set it absolute
  if the agent starts the server from elsewhere.
- `TA_AGENT_AUDIT_LOG` is optional but recommended: every number the agent quotes
  should be traceable to one entry. A number with no entry is a hallucination.

Verify before wiring the skill — expect 9 tools:

```bash
cd /abs/path/to/TAOpenHarness
uv run python scripts/mcp_smoke.py
# connected: ta-agent 0.1.0
# tools: get_price_data, compute_indicators, get_flow_summary,
#        analyze_multi_horizon, compute_weekly_indicators,
#        get_market_breadth, compare_tickers, screen_and_rank,
#        scan_foreign_flow
```

## 2. Load the skill

Copy the skill directory wherever the agent looks for skills:

```bash
cp -r skills/technical-analysis ~/.config/your-agent/skills/
```

| Agent convention | Where skills go |
|---|---|
| Claude Code | `~/.claude/skills/` or `.claude/skills/` in the project |
| OpenAI Codex | `~/.codex/skills/` |
| OpenHarness | `~/.openharness/skills/` (`uv run python scripts/install_skill.py` symlinks it) |
| Ollama / llama.cpp system prompt | paste `SKILL.md` as the system message |
| Anything else | point the skill loader at the file |

`SKILL.md` is self-contained: it names tools by bare name
(`analyze_multi_horizon`, not `mcp__ta-agent__analyze_multi_horizon`), so it
works under any host's tool prefix. The frontmatter `name` is
`trading_statistics`; the directory can stay `technical-analysis`.

## 3. Tool selection (what the skill will route to)

| User asks | Tool call | Payload vs `full` |
|---|---|---|
| Full / multi-horizon analysis | `analyze_multi_horizon(ticker, scope="full")` | 100% (~14k tokens) |
| Short-term only | `analyze_multi_horizon(scope="short_term")` | 43% |
| Mid-term only | `analyze_multi_horizon(scope="mid_term")` | 63% |
| Long-term only | `analyze_multi_horizon(scope="long_term")` | 57% |
| Support / resistance | `analyze_multi_horizon(scope="levels")` | 23% (~3.3k) |
| 2–5 tickers head to head | `compare_tickers(tickers, detail="compact")` | 32% smaller |
| Single indicator | `compute_indicators(groups=[...])` | cheapest |
| 5-day flow only | `get_flow_summary(ticker, window=5)` | cheap |
| Raw price rows | `get_price_data(ticker, lookback_days=N)` | rows only |
| Weekly bars | `compute_weekly_indicators(ticker)` | weekly groups |
| Market breadth | `get_market_breadth(exchange=...)` | index data |
| Momentum screener | `screen_and_rank(strategy=..., top_n=N)` | rank + top-1 details |
| Foreign flow scan | `scan_foreign_flow(window_days=..., top_n=N)` | rank only |

The skill enforces one call per question (a second call only for a different
ticker, a historical `as_of`, or a scope listed in `sections_omitted`).

## 4. Data

Bundled data is synthetic (VNM, HPG, TNG, 520 days to 2026-01-22). For the
live feed:

```bash
export TA_AGENT_API_URL='https://.../GetTradingStatistics'
export TA_AGENT_API_TOKEN='...'                                   # if bearer auth
export TA_AGENT_API_PARAMS='{"symbol":"stockCode","fromDate":"from","toDate":"to"}'
uv run python -m data.ingest --ticker VNM --start 2024-01-01 --end 2026-01-22
```

Re-ingest is idempotent (upsert on ticker + date). Without a live endpoint,
`scripts/mock_feed.py` serves the documented schema over HTTP for testing.

## 5. Standalone copy (`ta-plugin/`)

`ta-plugin/` is a snapshot of `data/`, `indicators/`, `mcp_server/`, `skills/`
for dropping into another repo without this checkout. It lags the root by
design; re-copy after pulling. In this repo the root is the source of truth.

## 6. Quality check (needs the real agent)

`docs/acceptance_prompts.md` has 10 prompts with the expected tool and expected
values (±0.5%) for the fixture window. It checks tool routing, number fidelity
against `logs/tool_calls.jsonl`, conflict naming, and refusals (ATR, gap, P/E,
news). `docs/going_live.md` documents the two plug-in seams and how each was
proved without the real thing.
