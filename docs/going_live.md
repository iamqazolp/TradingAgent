# Going live: the two things left to plug in

Everything between the data and the answer is built and verified. Two seams need
something only you have: a **model provider** and the **real data endpoint**.
Both are configuration, not code.

This document says exactly what to set, how each seam was proved without the real
thing, and what can only be checked once the real thing is in place.

## 1. The model provider

```bash
uv run python scripts/install_skill.py     # skill + MCP server into ~/.openharness
oh setup                                   # your provider, model and key
oh                                         # /skills lists technical-analysis
```

### Required: pin oh's MCP client

OpenHarness 0.1.9 reads `tool.inputSchema` from the MCP SDK, which renamed that
field to `input_schema` in 2.0. With mcp 2.x installed, `oh` starts the server,
raises `AttributeError: 'Tool' object has no attribute 'inputSchema'` while
listing its tools, marks the server `failed`, and carries on with **zero MCP
tools and no visible error**. The agent then answers from memory, which is the
one behaviour this project exists to prevent.

```bash
uv tool install 'openharness-ai==0.1.9' --with 'mcp<2' --force
```

mcp 1.29.1 has both the old field name and the newer transport helpers 0.1.9
imports, so this is the version to be on. `scripts/install_skill.py` checks and
warns; `scripts/loop_smoke.py` fails loudly with the same hint.

Note that `oh --dry-run` prints `ta-agent: stdio -> ... (ok)` even when the
connection would fail — dry-run resolves the config without starting anything
("mcp: skipped in dry-run"). It is not a substitute for the loop check.

### How this was verified without credentials

`scripts/mock_model.py` is a stand-in for the Anthropic Messages API: it streams
a scripted reply instead of a generated one. `scripts/loop_smoke.py` runs `oh`
against it and asserts the six things that make the loop real:

```
$ uv run python scripts/loop_smoke.py
  ok   oh called the model endpoint
  ok   all three MCP tools advertised to the model: mcp__ta-agent__get_price_data,
       mcp__ta-agent__compute_indicators, mcp__ta-agent__get_flow_summary
  ok   the skill is in the system prompt
  ok   the tool ran and its result went back to the model
  ok   the answer quotes a value from the tool result: foreign_room_trend=1651872.0
  ok   the same value is in the audit log: 1 new entr(y/ies)

RESULT ok - the loop works; plug in a real provider with `oh setup`
```

So: the skill reaches the system prompt, the tools reach the model under the
names `mcp__ta-agent__*`, a `tool_use` is dispatched to our server, the JSON
result comes back into the conversation, and the number the model repeats is the
number in `logs/tool_calls.jsonl`.

### What a real provider still has to prove

The mock model does not think, so it cannot test judgement. Once your credentials
are in, work through `docs/acceptance_prompts.md` (10 prompts, with the expected
tool call and expected values for each). Those check what only a real model can:
that it calls the right tool, reports the numbers the tool returned, keeps the
seven groups separate, names conflicts instead of averaging them, and **refuses**
prompts 7–9 (ATR, overnight gap, P/E and news) instead of approximating.

## 2. The data endpoint

The bundled data is synthetic (see the README). Switching to the live feed is
three environment variables and one command:

```bash
export TA_AGENT_API_URL='https://.../GetTradingStatistics'
export TA_AGENT_API_TOKEN='...'                                   # if it needs a bearer token
export TA_AGENT_API_PARAMS='{"symbol":"stockCode","fromDate":"from","toDate":"to"}'
                                                                  # only if the names differ
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
