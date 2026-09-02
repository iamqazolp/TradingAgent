# Going Live: Connecting Agents & Market Data

Everything between market data and the technical analysis answer is built and mathematically verified. Two integration points remain for production deployment:
1. **Your AI Agent / MCP Client** (Claude Desktop, Antigravity, Cursor, Windsurf, Claude Code, etc.)
2. **Your Live Data Feed** (single endpoint or dual-source)

---

## 1. Connecting Your AI Agent / MCP Client

The `ta-agent` server communicates over standard stdio Model Context Protocol (MCP).

### Register the MCP Server

Configure your MCP host using `mcp_config.json`:

```json
{
  "mcpServers": {
    "ta-agent": {
      "command": "uv",
      "args": ["run", "--directory", "/absolute/path/to/TAOpenHarness", "python", "-m", "mcp_server.server"],
      "env": {
        "TA_AGENT_DB": "/absolute/path/to/TAOpenHarness/var/ta.sqlite",
        "TA_AGENT_AUDIT_LOG": "/absolute/path/to/TAOpenHarness/logs/tool_calls.jsonl",
        "TA_AGENT_LOG_LEVEL": "INFO"
      }
    }
  }
}
```

### Provide the Reasoning Skill

Supply `skills/technical-analysis/SKILL.md` as system instructions or prompt context. This ensures the model:
1. Calls the three MCP tools directly (`get_price_data`, `compute_indicators`, `get_flow_summary`).
2. Adheres to multi-timeframe confluence rules (1H/4H vs 1D/1W).
3. Quotes only tool-verified numeric readings.
4. Refuses unsupported metrics (fundamentals, news sentiment, tick VWAP).

### Verification

Run the stdio smoke test to verify MCP server tool dispatch:

```bash
uv run python scripts/mcp_smoke.py
```


## 2. The data endpoint

The bundled data is synthetic (see the README). Switching to the live feed is
as simple as configuring your `.env` file:

```bash
cp .env.example .env
```

And filling in the variables in `.env`:
```ini
TA_AGENT_API_URL=https://.../GetTradingStatistics
TA_AGENT_API_TOKEN=your_token_here                                # if it needs a bearer token
TA_AGENT_API_PARAMS={"symbol":"stockCode","fromDate":"from","toDate":"to"} # only if names differ
```

Then run ingestion:
```bash
uv run python -m data.ingest --ticker VNM --start 2024-01-01 --end 2026-01-22
```

Defaults, if you set nothing but the URL: query parameters `symbol`, `fromDate`,
`toDate`, and dates passed through as you typed them (`YYYY-MM-DD`). If the
endpoint wants a different date format, that is the one change that would need
code — everything else is remappable.

The response envelope does not need to be known in advance:
`data.ingest.extract_records` finds the record list inside `{"Data": [...]}`, a
bare list, or the double-encoded ASP.NET `{"d": "<json string>"}` form.

Re-ingesting the same window is a no-op: the store upserts on (ticker, date), so
a daily cron can safely overlap.

### How this was verified without the endpoint

`scripts/mock_feed.py` serves the documented `GetTradingStatistics` schema —
every numeric a string, some with thousands separators, `dd/mm/yyyy` dates — out
of the fixture, over real HTTP:

```bash
uv run python scripts/mock_feed.py --port 8765 --token secret &
TA_AGENT_API_URL=http://127.0.0.1:8765/GetTradingStatistics \
TA_AGENT_API_TOKEN=secret \
TA_AGENT_DB=/tmp/live_test.sqlite \
  uv run python -m data.ingest --ticker VNM --start 2025-12-01 --end 2026-01-22
# 38 rows written, 38 parsed, 0 rejected, 2 warnings, tickers=VNM
```

`tests/test_live_feed.py` (11 tests) drives that server from pytest and covers
the fetch, the bearer token, the parameter remapping, the ASP.NET envelope,
idempotent re-ingest, and the four failure messages you might actually meet:

| What went wrong | What you see |
|---|---|
| Wrong or missing token | `feed returned HTTP 401 Unauthorized ...; check TA_AGENT_API_TOKEN` plus the endpoint's own message |
| Endpoint spells parameters differently | `feed returned HTTP 400 ...; check the query parameter names (TA_AGENT_API_PARAMS)` |
| Host unreachable | `cannot reach <url>: [Errno 61] Connection refused` |
| HTML login page with HTTP 200 | `feed response ... is not JSON`, with the first 200 characters |

All four exit 1 with a one-line message, not a traceback.

### What the real endpoint still has to prove

- **Field names.** The parser maps the documented names (`PriceClose`,
  `PricePreviousClose`, `TotalVolume`, `ForeignerBuyQuantity`,
  `CurrentForeignRoom`, …). If the live payload spells any of them differently,
  `FIELD_MAP` in `data/ingest.py` is the one place to fix.
- **Adjustment policy.** Whether the feed adjusts closes for splits and stock
  dividends. Rows where `prev_close` disagrees with the prior row's close are
  flagged as `suspected_corporate_actions`, and OBV refuses on them, but only
  the provider can tell you whether that is an unadjusted action or a data
  error. Ask before trusting long-window indicators across such a date.
- **Reconciliation residuals.** The fixture carries ~2% buy/sell-vs-total
  residuals on purpose. Real residuals will differ in size; they stay warnings
  either way, and the provider's totals stay the source of truth.
- **Blank conventions.** A blank volume, count, value or room field is read as
  `0`. If the live feed uses blanks to mean "unknown" rather than "nothing
  traded", that reading needs revisiting.

## Checklist

```bash
uv sync                                     # deps from pyproject.toml
uv run pytest                               # 125 tests
uv run python scripts/validate.py           # 105/105 vs the independent ground truth
uv run python scripts/mcp_smoke.py          # tools over a real stdio session
uv run python scripts/install_skill.py      # register skill + server, check oh's mcp version
uv run python scripts/loop_smoke.py         # the whole loop, with a scripted model
oh setup                                    # <- your credentials
# then: docs/acceptance_prompts.md, and TA_AGENT_API_URL for the live feed
```
