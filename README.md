# Vietnamese stock technical analysis agent

A technical-analysis agent for Vietnamese equities, built on
[OpenHarness](https://github.com/HKUDS/OpenHarness). An MCP server computes
indicators from a daily close-only feed; a skill file makes the agent reason group
by group and refuse what the data cannot support.

Built from `PLAN.md`. Nothing from the plan's deferred roadmap (section 10) is
implemented, not even as a stub.

## The one thing to know about the data

The feed (VietinBank `GetTradingStatistics`) gives **close and previous close
only**. Per ticker per trading day:

| Available | Not available |
|---|---|
| previous close, close | **open, high, low** |
| matched trade count, traded value (VND), traded volume | any intraday or tick data |
| buy-side / sell-side trade counts and matched volumes | bid/ask, order book |
| foreign buy/sell volume and value, remaining foreign room | fundamentals, news, index or sector data |

So there is no ATR, no ADX, no Stochastic, no Ichimoku, no candlestick pattern, no
overnight gap, no true VWAP, and no market breadth. The engine refuses each of
these by name with the honest substitute where one exists
(`indicators/engine.py::UNSUPPORTED_METRICS`), and the skill tells the agent to
refuse rather than approximate. Close-to-close realized volatility stands in for
ATR in stop sizing and is labelled `is_atr_substitute: true` in the payload
itself, so no layer downstream can present it as ATR by accident.

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
uv run pytest                                        # 326 tests
uv run python scripts/ground_truth.py                # independent reference values
uv run python scripts/validate.py                    # engine vs ground truth
uv run python scripts/mcp_smoke.py                   # real stdio MCP session
uv run python scripts/loop_smoke.py                  # the whole agent loop, scripted model
```

All Python work goes through `uv`; `pyproject.toml` is the source of truth.
`requirements.txt` is a generated compatibility artifact
(`uv export --no-hashes --no-dev --no-emit-project -o requirements.txt`) — edit
`pyproject.toml` and re-export, never the other way round.

## Using it as a drop-in skill (any local agent)

The project supplies two things: the `ta-agent` MCP server and the skill
(`skills/technical-analysis/SKILL.md`). See `docs/INTEGRATION.md` for the
full step-by-step.

```bash
# 1. Drop in the MCP server (any host — Claude Code, Cursor, Gemini CLI, OH, ...)
uv run python scripts/mcp_smoke.py                # prove the 9 tools load locally

# 2. Copy the skill to the agent's skills dir
cp -r skills/technical-analysis ~/.claude/skills/   # adjust for the host
# or: paste skills/technical-analysis/SKILL.md as the agent system prompt
```

For **OpenHarness** (`oh`), an installer is provided:

```bash
uv tool install 'openharness-ai==0.1.9' --with 'mcp<2' --force   # required pin, see below
uv run python scripts/install_skill.py             # symlinks skill + registers MCP server
uv run python scripts/loop_smoke.py                # scripted end-to-end check
oh --dry-run -p "analyse VNM"                      # config resolves to (ok) — see docs/INTEGRATION.md
```

The `mcp<2` pin is not optional with `oh 0.1.9`: it unmarshals tools via
`tool.inputSchema`, renamed to `input_schema` in mcp 2.x. With 2.x it connects
then silently advertises no tools and the agent falls back to memory.
`install_skill.py` and `loop_smoke.py` both warn/fail with the fix (mcp 1.29.1).

## Tools

| Tool | Use it for |
|---|---|
| `get_price_data(ticker, lookback_days=300, start, end)` | raw stored rows, oldest first. `lookback_days` counts trading rows, not calendar days |
| `compute_indicators(ticker \| rows, groups, params, series_tail)` | the seven indicator groups |
| `get_flow_summary(ticker \| rows, window=5)` | the cheap flow-only answer |
| `analyze_multi_horizon(ticker, lookback_days=500, scope="full", detail="compact")` | the analysis report, scoped to the question |
| `compute_weekly_indicators(ticker \| rows, groups, series_tail=26)` | indicator groups on weekly bars only |
| `compare_tickers(tickers, lookback_days=250, detail="compact")` | 2–5 tickers head to head |
| `get_market_breadth(exchange="VNINDEX"\|HNX\|UPCOM\|ALL")` | advances/declines/unchanged, AD ratio, regime per index |
| `screen_and_rank(universe, strategy, top_n)` | ranked picks; the leader's flow, foreign and levels ride along in the same payload |
| `scan_foreign_flow(universe, window_days, top_n)` | foreign net buy/sell ranking + room warnings across the universe |

Groups: `trend`, `momentum`, `volatility`, `volume_flow`, `trade_flow`,
`value_flow`, `foreign_flow`.

### Two independent ways the payload is kept small

Both matter because the consumer is a locally hosted model holding the skill
prompt, the tool payload and its own report in one context window.

**`scope` — compute only what the question needs.** Answering "phân tích ngắn
hạn VNM" with the full report spends about 73% of its tokens on weekly bars,
52-week statistics and two unused horizons, and pays to compute them. Each scope
declares its own inputs:

| `scope` | For | Payload vs `full` |
|---|---|---|
| `full` | phân tích toàn diện / đa khung — all three horizons, measured levels, 52-week stats | 100% (~14k tokens) |
| `short_term` | ngắn hạn only. Skips weekly aggregation entirely | 43% |
| `mid_term` | trung hạn only, with weekly SMA20 confirmation | 63% |
| `long_term` | dài hạn only, plus 52-week stats | 57% |
| `levels` | hỗ trợ / kháng cự and current price position only | 23% (~3.3k tokens) |

Sections a scope skips are named in `sections_omitted`, with a note stating they
were **not requested** rather than unavailable — otherwise a report would say
"chưa đủ dữ liệu" about data that was never asked for.

**`detail` — drop fields duplicated elsewhere in the same response.**
`"compact"` (the default) cuts `analyze_multi_horizon` from ~17k to ~14k tokens
and `compare_tickers` on three tickers from ~13k to ~7.6k (32.6k of its 40k
characters were a per-ticker indicator dump no table reads). `"full"` restores
every field.

## The interpretation layer

`analyze_multi_horizon` does the reasoning in Python rather than leaving it to
the model, because the deployment target is a ~31B local model that renders the
payload into a report.

Each of the three horizons — ngắn hạn (1–4 weeks), trung hạn (1–3 months), dài
hạn (> 3 months) — is built from windows appropriate to *its own* timeframe, so
agreement between them is real rather than one reading counted three times:

| Horizon | Inputs |
|---|---|
| short | EMA12/SMA20, RSI(14) daily, MACD daily, close percentile 20d, imbalance 5d, foreign 20d, return streak |
| mid | SMA20/50/100, SMA20/50 crossover, RSI(14) daily, MACD daily, percentile 60d, OBV divergence 60d, foreign 60d, weekly SMA20 confirmation |
| long | weekly SMA20/50, daily SMA200, SMA50/200 crossover, weekly RSI, weekly MACD, percentile 126d, foreign 120d, 250d return |

Each horizon returns a `components` list — one entry per contributing group with
a `direction` (+1 / 0 / −1), a `weight`, and a Vietnamese `evidence` sentence
that already contains the number. A group with no data reports
`direction: null` plus a `missing_reason` and is excluded from the score, so
thin history lowers `confidence` instead of dragging the verdict toward neutral.
`confidence_reason`, `conflicts` and a measured `invalidation` level come with
it. `indicators/levels.py` supplies support and resistance from swing closes,
N-session close extremes, moving averages and Bollinger bands, merged into
confluence zones — every level names its `basis`, and nothing is derived from a
multiple of the close.

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
| RSI | Wilder smoothing. `avg_loss == 0` reads 100 when there were gains, 50 when the window is flat |
| Bollinger | population stdev (`ddof=0`), the classic definition |
| Realized volatility | sample stdev (`ddof=1`) of log returns, annualized on 252 days, labelled an ATR substitute |
| "vs baseline" ratios | today divided by the mean of the previous `n` days, **excluding today** — including it would damp the very spike being detected |
| OBV | signed by the provider's `prev_close`, cross-checked against the prior row's close at 0.5% tolerance; disagreement refuses instead of guessing |

## Known limitations

Documented so no future contributor assumes otherwise:

- **No open, high, low, fundamentals, news, or index data.** See the table above.
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
engine against it — 35 checks per ticker across VNM, HPG and TNG.

Current result: **105/105 checks pass**, largest disagreement ~2.6e-14 (floating
point noise) against a 0.5% tolerance, with no sign or order-of-magnitude
failures. Report: `logs/validation_report.json`.

## Layout

```
data/            schema.sql, store.py (SQLite upsert store), ingest.py (casts, rejects, warnings)
indicators/      pure functions: trend, momentum, volatility, volume_flow,
                 trade_flow, value_flow, foreign_flow + engine.py (dispatch, quality, serialize)
                 interpretation: weekly.py (bar aggregation), levels.py (support/resistance),
                 horizon.py (three horizons, scored components, confidence),
                 strategies.py, stats_52w.py, comparison.py
mcp_server/      server.py (nine tools, audit log), tool_schemas.py (pydantic validation)
skills/          technical-analysis/SKILL.md — the reasoning framework
scripts/         generate_fixtures, ground_truth, validate, mcp_smoke, install_skill,
                 mock_feed + mock_model (test doubles for the two plug-in points),
                 loop_smoke (oh end to end against the scripted model)
tests/           326 tests: hand-calculated indicators, engine, store/ingest, MCP layer,
                 live feed over HTTP, levels, horizon inputs, SKILL.md field contract
docs/            INTEGRATION.md — drop-in steps for any local agent (start here)
                 going_live.md — the two plug-in points, what is proved and what is not
                 acceptance_prompts.md — the 10 interactive prompts for Phase 4
```

## Status against the plan

| Phase | State |
|---|---|
| 0 Environment | done. `oh -p` runs a full turn end to end against the scripted model in `scripts/loop_smoke.py`; only the real provider key is missing (`oh setup`) |
| 1 Data layer | done: 3 tickers × 520 days ingested, range queries and idempotent re-ingest tested, live HTTP fetch tested against `scripts/mock_feed.py` |
| 2 Indicator engine | done: Groups A–G pure, hand-calculated unit tests, insufficient-data contract enforced |
| 3 MCP server | done: nine tools, pydantic-validated, verified over a real stdio session |
| 4 OpenHarness integration | done mechanically: the skill reaches the system prompt, all nine tools reach the model, one is dispatched, executed and audited (`loop_smoke.py`). The 10-prompt judgement check needs credentials — see `docs/acceptance_prompts.md` |
| 5 Ground-truth validation | done: 105/105, max deviation 2.6e-14 |
| 6 Hardening | done: gaps/halts/short history, corporate-action flagging, per-call audit log |
