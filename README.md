# Vietnamese stock technical analysis agent

A technical-analysis agent for Vietnamese equities, built on
[OpenHarness](https://github.com/HKUDS/OpenHarness). An MCP server computes
indicators from an hourly/daily OHLC and order-flow feed; a skill file makes the agent reason group
by group and refuse what the data cannot support.

Supports multi-timeframe analysis across **1H, 4H, 1D, 3D, 1W, 1M, 1Y**.

## The data and indicator capabilities

The feed provides OHLC price action and order-flow dynamics:

| Available | Not available |
|---|---|
| OHLC prices: open, high, low, close, previous close | sub-hour minute / tick data |
| Timeframes: 1H, 4H, 1D, 3D, 1W, 1M, 1Y | real-time sub-minute tick VWAP |
| Matched trade count, traded value (VND), traded volume | bid/ask order book depth |
| Buy-side / sell-side trade counts and matched volumes | fundamentals, news sentiment |
| Foreign buy/sell volume and value, remaining foreign room | multi-ticker index breadth |

Supported indicators include:
- **Trend**: SMA, EMA, MACD, Wilder ADX / DMI (+DI, -DI, DX, ADX)
- **Momentum**: Wilder RSI(14), Stochastic (%K, %D)
- **Volatility**: Bollinger Bands, Wilder True ATR(14) with stop-loss sizing, Realized Volatility
- **Order & Foreign Flow**: Buy/Sell volume & count imbalances, Average trade size by side, Value spikes, Foreign net value, Room trend

The engine refuses metrics this feed cannot support (`indicators/engine.py::UNSUPPORTED_METRICS`), and the skill instructs the agent to refuse rather than approximate.

## Data provenance: synthetic fixtures

**The bundled data is synthetic.** The documented `GetTradingStatistics` endpoint
could not be located, so `scripts/generate_fixtures.py` produces a deterministic
fixture matching the documented response schema exactly: string-typed numerics
with thousands separators, `dd/mm/yyyy` dates, holiday gaps, ~2% volume/trade
reconciliation residuals, one unexplained foreign-room event on VNM, and blank
fields where a small cap has no foreign trading. It covers **VNM, HPG and TNG,
520 trading days each (2024-01-02 → 2026-01-22)**.

Treat every number in this repo as a correctness fixture, not a market fact.

Switching to the live feed needs no code change, only environment variables:

```bash
export TA_AGENT_API_URL='https://.../GetTradingStatistics'
export TA_AGENT_API_TOKEN='...'                       # optional bearer token
export TA_AGENT_API_PARAMS='{"symbol":"ticker"}'      # if the query params differ
uv run python -m data.ingest --ticker VNM --start 2024-01-01 --end 2026-01-22
```

`data.ingest.extract_records` unwraps whatever envelope the response arrives in
(including the double-encoded ASP.NET `{"d": "..."}` form), so the exact wrapper
does not need to be known in advance.

That path is not taken on trust: `scripts/mock_feed.py` serves the documented
schema over real HTTP, and `tests/test_live_feed.py` drives ingestion through it
— token, parameter remapping, envelope forms, idempotent re-ingest, and the
error message for each way it can fail. See `docs/going_live.md`.

## Quick start

```bash
uv sync                                              # create .venv from pyproject.toml
uv run python scripts/generate_fixtures.py           # write tests/fixtures/*.json
uv run python -m data.ingest --file tests/fixtures/sample_daily_data.json
uv run pytest                                        # 146 tests
uv run python scripts/ground_truth.py                # independent reference values
uv run python scripts/validate.py                    # engine vs ground truth
uv run python scripts/mcp_smoke.py                   # real stdio MCP session
uv run python scripts/loop_smoke.py                  # the whole agent loop, scripted model
```

All Python work goes through `uv`; `pyproject.toml` is the source of truth.
`requirements.txt` is a generated compatibility artifact
(`uv export --no-hashes --no-dev --no-emit-project -o requirements.txt`) — edit
`pyproject.toml` and re-export, never the other way round.

## Using it with OpenHarness

```bash
uv tool install 'openharness-ai==0.1.9' --with 'mcp<2' --force   # required, see below
uv run python scripts/install_skill.py    # symlinks the skill + registers the MCP server
uv run python scripts/loop_smoke.py       # proves the loop before you spend a token
oh setup                                  # your provider and model, once
oh                                        # /skills lists technical-analysis
```

**The `mcp<2` pin is not optional.** OpenHarness 0.1.9 reads `tool.inputSchema`,
renamed to `input_schema` in MCP SDK 2.0, so with mcp 2.x it connects to the
server, fails while listing its tools, marks it `failed`, and runs the agent with
**no MCP tools and no visible error** — the exact situation where a model answers
from memory. `install_skill.py` checks the version and `loop_smoke.py` fails with
the fix. mcp 1.29.1 works: it still has the old field name and also has the
newer transport helpers 0.1.9 imports.

`install_skill.py` symlinks `skills/technical-analysis` into
`~/.openharness/skills/` and adds the `ta-agent` stdio server to
`~/.openharness/settings.json`. Pass `--config-dir` to install somewhere else
(`OPENHARNESS_CONFIG_DIR` is honoured), and `--uninstall` to undo both.

Note for OpenHarness **0.1.9**: `oh --mcp-config <file>` and `oh --settings <file>`
are accepted on the command line but never read — both options are declared in
`cli.py` and unused. MCP servers are loaded from `<config dir>/settings.json`
(`mcp_servers`) and from enabled plugins, so that is what the installer writes.
`mcp_config.json` is kept in the repo as the portable artifact for other MCP hosts
and is regenerated by the installer from this checkout's paths.

Also in 0.1.9: `oh mcp list` crashes with
`AttributeError: 'McpStdioServerConfig' object has no attribute 'get'` whenever
any stdio server is configured, including one added by `oh mcp add` itself
(`cli.py::mcp_list` calls `.get()` on a pydantic model). Nothing here depends on
it — use `oh --dry-run` to inspect the resolved MCP config instead.

Verify without a model provider:

```bash
oh --dry-run -p "analyse VNM"
# skills: 9 ... technical-analysis ...
# Configured MCP - ta-agent: stdio -> .../python -m mcp_server.server (ok)
# Likely Matches - skills: technical-analysis (score=7) [analyse]
```

`--dry-run` resolves the config but does not start anything ("mcp: skipped in
dry-run"), so its `(ok)` means "config is valid", not "tools load". For that,
`scripts/loop_smoke.py` runs `oh` against a scripted model
(`scripts/mock_model.py`) and checks that our three tools are advertised, that
one is dispatched and executed, that its result returns into the conversation,
and that the value quoted back also appears in `logs/tool_calls.jsonl`.

## Tools

| Tool | Use it for |
|---|---|
| `get_price_data(ticker, lookback_days=300, start=None, end=None, timeframe="1D")` | raw stored / resampled OHLC rows, oldest first. `lookback_days` counts trading rows, not calendar days |
| `compute_indicators(ticker \| rows, groups=None, params=None, series_tail=10, lookback_days=300, timeframe="1D")` | the seven indicator groups |
| `get_flow_summary(ticker \| rows, window=5, lookback_days=300, timeframe="1D")` | the flow-only answer |

Groups: `trend`, `momentum`, `volatility`, `volume_flow`, `trade_flow`,
`value_flow`, `foreign_flow`. Supported timeframes: `1H`, `4H`, `1D`, `3D`, `1W`, `1M`, `1Y`.

Every indicator function is pure — pandas in, dict out, no I/O — and when history
is too short it returns

```json
{"insufficient_data": true, "reason": "sma(200) requires 200 rows of history, 3 available",
 "required_window": 200, "available": 3}
```

never a NaN, never an exception, and never a value quietly computed from a shorter
window.

## Fixed indicator conventions

These are choices, not accidents. The independent ground-truth script in
`scripts/ground_truth.py` implements the same conventions from scratch, so
changing one here fails validation there.

| Indicator | Convention |
|---|---|
| EMA | SMA-seeded (TA-Lib style): the first value is the mean of the first `n` closes. Deliberately **not** pandas `ewm(adjust=False)`, which seeds on the first observation |
| MACD | signal EMA computed on the live (dropna) MACD section, so leading NaNs cannot contaminate the recursion; requires `slow + signal - 1` rows |
| ADX / DMI | Wilder smoothing on +DM, -DM, and TR. Defaults p_di/m_di/dx to 0.0 when smoothed TR is 0 (prevents NaN cascades) |
| RSI | Wilder smoothing. `avg_loss == 0` reads 100 when there were gains, 50 when the window is flat |
| Stochastic | Classic (14, 3, 3) %K and %D over highest high and lowest low |
| ATR | True Wilder 14-period Average True Range for stop sizing |
| Bollinger | population stdev (`ddof=0`), the classic definition |
| Realized volatility | sample stdev (`ddof=1`) of log returns, annualized on 252 days |
| "vs baseline" ratios | today divided by the mean of the previous `n` days, **excluding today** — including it would damp the very spike being detected |
| OBV | signed by the provider's `prev_close`, cross-checked against the prior row's close at 0.5% tolerance; disagreement refuses instead of guessing |

## Known limitations

Documented so no future contributor assumes otherwise:

- **No sub-hour tick data, sub-minute tick VWAP, fundamentals, news sentiment, or index breadth.**
- **The bundled data is synthetic.** See "Data provenance".
- **Corporate actions may be unadjusted.** A `prev_close` that disagrees with the
  prior row's close is the fingerprint of an unadjusted split or stock dividend.
  It is reported in `data_quality.suspected_corporate_actions` on every
  `compute_indicators` result, and OBV refuses outright rather than carrying a
  corrupted running total. Confirm adjustment status with the provider before
  trusting close-based indicators across such a date.
- **Foreign room is a snapshot, not a flow.** Room can move because the foreign
  ownership limit or charter capital changed. Room changes the day's foreign net
  volume cannot explain are reported as `suspected_structural_changes`, not read
  as flow.
- **Volume and trade counts may not reconcile.** `total_volume` and `total_trade`
  are stored as reported, never derived from the two sides. A mismatch is an
  ingest warning, never a rejection.
- **Zero foreign volume is normal** on small caps (TNG averages 0.19%
  participation in the fixture). It is market behaviour, not an ingestion bug.
- **Gaps stay gaps.** Holidays, halts and zero-volume days are never filled with
  zeros; they drop out of rolling windows instead of dragging them toward neutral.
- **Newly listed tickers get markers, not numbers.** Anything that needs more
  history than exists returns the insufficient-data marker.
- **Every numeric field arrives as a string** and is cast explicitly. A missing or
  non-positive `PriceClose` / `PricePreviousClose` rejects the row; a blank volume,
  count, value or room field becomes 0, which is what a blank means in this feed.

## Auditability

Every tool call is appended to `logs/tool_calls.jsonl` (override with
`TA_AGENT_AUDIT_LOG`) with its arguments, computed values and duration; bulk row
lists collapse to a count so the log stays readable. Any number the agent quotes
can be traced to a real call — a number with no matching entry was hallucinated.
Logging goes to stderr so stdout stays clean for the MCP protocol, and a
non-writable log never costs the user their answer.

## Validation

`scripts/ground_truth.py` reimplements every Tier 0 indicator in pure stdlib
(`math`, `statistics`, its own parser, no pandas and no project imports) and
writes `tests/fixtures/ground_truth.json`. `scripts/validate.py` compares the
engine against it — 46 checks per ticker across VNM, HPG and TNG.

Current result: **138/138 checks pass**, largest disagreement ~2.6e-14 (floating
point noise) against a 0.5% tolerance, with no sign or order-of-magnitude
failures. Report: `logs/validation_report.json`.

## Layout

```
data/            schema.sql, store.py (SQLite upsert store), ingest.py (casts, rejects, warnings), resample.py (multi-timeframe)
indicators/      pure functions: trend, momentum, volatility, volume_flow,
                 trade_flow, value_flow, foreign_flow + engine.py (dispatch, quality, serialize)
mcp_server/      server.py (three tools, audit log), tool_schemas.py (pydantic validation)
skills/          technical-analysis/SKILL.md — the reasoning framework
scripts/         generate_fixtures, ground_truth, validate, mcp_smoke, install_skill,
                 mock_feed + mock_model (test doubles for the two plug-in points),
                 loop_smoke (oh end to end against the scripted model)
tests/           146 tests: hand-calculated indicators, engine, store/ingest, MCP layer,
                 resampling, live feed over HTTP
docs/            going_live.md — the two plug-in points, what is proved and what is not
                 acceptance_prompts.md — the 10 interactive prompts for Phase 4
```

## Status against the plan

| Phase | State |
|---|---|
| 0 Environment | done. `oh -p` runs a full turn end to end against the scripted model in `scripts/loop_smoke.py`; only the real provider key is missing (`oh setup`) |
| 1 Data layer | done: OHLC schema, 3 tickers × 520 days ingested, multi-timeframe resampler (1H, 4H, 1D, 3D, 1W, 1M, 1Y) tested |
| 2 Indicator engine | done: Groups A–G pure, ATR/ADX/Stochastic/Ichimoku, hand-calculated unit tests, insufficient-data contract enforced |
| 3 MCP server | done: three tools, multi-timeframe pydantic-validated, verified over a real stdio session |
| 4 OpenHarness integration | done mechanically: the skill reaches the system prompt, all three tools reach the model, one is dispatched, executed and audited (`loop_smoke.py`). The 10-prompt judgement check needs credentials — see `docs/acceptance_prompts.md` |
| 5 Ground-truth validation | done: 138/138, max deviation 2.6e-14 |
| 6 Hardening | done: gaps/halts/short history/flat periods, corporate-action flagging, per-call audit log |
