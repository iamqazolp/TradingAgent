# Vietnamese Equities Technical Analysis Agent & Skills

A standalone Model Context Protocol (MCP) server and agent reasoning skill for technical analysis of Vietnamese equities (VNM, HPG, TNG, etc.).

Computes multi-timeframe OHLC indicators, order flow, and foreign activity directly from market data. Equips any AI agent (Claude Desktop, Antigravity, Cursor, Windsurf, Claude Code, etc.) with disciplined, hallucination-free technical analysis capabilities.

Supports multi-timeframe analysis across **1H, 4H, 1D, 3D, 1W, 1M, 1Y**.

---

## Capabilities

The engine provides OHLC price action and order-flow dynamics:

| Available | Excluded (refused rather than faked) |
|---|---|
| **OHLC prices**: Open, High, Low, Close, Previous Close | Sub-hour minute / tick data |
| **Timeframes**: 1H, 4H, 1D, 3D, 1W, 1M, 1Y | Real-time sub-minute tick VWAP |
| **Order flow**: Traded value (VND), volume, matched trades | Order book bid/ask depth |
| **Trade sizing**: Buy-side / sell-side counts, avg ticket size | Fundamentals, balance sheet ratios |
| **Foreign flow**: Foreign buy/sell value & volume, foreign room | News sentiment, social sentiment |

### Supported Indicators

- **Trend**: SMA (20, 50, 200), EMA (12, 26), MACD (12, 26, 9), Wilder ADX / DMI (+DI, -DI, DX, ADX), Ichimoku Kinko Hyo (Tenkan, Kijun, Senkou Span A/B, Chikou, Kumo sentiment, TK cross)
- **Momentum**: Wilder RSI (14), Stochastic Oscillator (%K, %D with slowing)
- **Volatility**: Bollinger Bands (20, 2σ), Wilder True ATR (14) with volatility stop sizing, Realized Volatility
- **Order & Foreign Flow**: Buy/Sell volume & trade-count imbalances, average trade ticket size by side, value spikes vs 20-period baseline, foreign net value (VND), foreign participation ratio, structural foreign room trend

The engine explicitly refuses metrics this feed cannot support (`indicators/engine.py::UNSUPPORTED_METRICS`), and `skills/technical-analysis/SKILL.md` instructs the agent to decline rather than fabricate.

---

## Quick Start

```bash
# 1. Install dependencies
uv sync

# 2. Ingest fixture dataset (VNM, HPG, TNG: 520 trading days each)
uv run python -m data.ingest --file tests/fixtures/sample_daily_data.json

# 3. Verify ground truth (138/138 checks pass)
uv run python scripts/ground_truth.py
uv run python scripts/validate.py

# 4. Run test suite (150 tests)
uv run pytest

# 5. Smoke-test the MCP server over stdio
uv run python scripts/mcp_smoke.py
```

---

## Using with Any AI Agent

This project exposes its capabilities via standard **Model Context Protocol (MCP)** and an agent **Skill definition**.

### 1. Register the MCP Server

Add to your MCP client configuration (Claude Desktop, Antigravity, Cursor, Windsurf, Zed, etc.):

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

A pre-configured template is available at `mcp_config.json`.

### 2. Equip Your Agent with the Skill

Give your agent the reasoning instructions in `skills/technical-analysis/SKILL.md`:

- **For Antigravity / Claude Code**: Point your client or slash command to `skills/technical-analysis/SKILL.md`.
- **For Claude Desktop / Web / Custom System Prompts**: Include the contents of `skills/technical-analysis/SKILL.md` in the system instructions.

---

## MCP Tools Reference

The server exposes three focused tools:

| Tool | Parameters | Purpose |
|---|---|---|
| `get_price_data` | `ticker`, `lookback_days=300`, `start=None`, `end=None`, `timeframe="1D"` | Fetches raw / resampled OHLC bars with order flow, oldest first. `lookback_days` counts bars in the target timeframe. |
| `compute_indicators` | `ticker` (or `rows`), `groups=None`, `params=None`, `series_tail=10`, `lookback_days=300`, `timeframe="1D"` | Computes requested indicator groups (`trend`, `momentum`, `volatility`, `volume_flow`, `trade_flow`, `value_flow`, `foreign_flow`). |
| `get_flow_summary` | `ticker` (or `rows`), `window=5`, `lookback_days=300`, `timeframe="1D"` | Lightweight answer for order flow and foreign capital movement without a full indicator pass. |

Every indicator function is pure (pandas in, dictionary out, no I/O). When history is too short, it returns an explicit marker:

```json
{
  "insufficient_data": true,
  "reason": "sma(200) requires 200 rows of history, 3 available",
  "required_window": 200,
  "available": 3
}
```
Never a `NaN`, never an unhandled exception, and never a value silently computed from an inadequate window.

---

## Fixed Indicator Conventions

Conventions are pinned mathematically in `indicators/` and cross-verified by an independent pure standard-library implementation in `scripts/ground_truth.py`:

| Indicator | Convention |
|---|---|
| **EMA** | SMA-seeded (TA-Lib style): first value is the mean of the first `n` closes. Deliberately *not* pandas `ewm(adjust=False)` which seeds on a single observation. |
| **MACD** | Signal EMA computed on the live (`dropna`) MACD section only; leading NaNs cannot contaminate the recursion. Requires `slow + signal - 1` rows. |
| **ADX / DMI** | Wilder smoothing on +DM, -DM, and TR. Defaults DI/DX to `0.0` when smoothed TR is 0 to prevent NaN cascades on flat/halted periods. |
| **RSI** | Wilder smoothing. Flat windows (`avg_loss == 0` and `avg_gain == 0`) read 50.0. |
| **Stochastic** | Classic (14, 3, 3) %K and %D over highest high and lowest low. |
| **ATR** | Wilder 14-period True Range for volatility stop-loss sizing. |
| **Bollinger** | Population standard deviation (`ddof=0`), the classic definition. |
| **Realized Volatility** | Sample standard deviation (`ddof=1`) of log returns, annualized on 252 trading days. |
| **Baseline Ratios** | Today's value divided by the mean of the prior `n` days, **excluding today** to avoid dampening the detected spike. |
| **OBV** | Direction determined by provider's `prev_close`, validated against the prior row's close at 0.5% tolerance. |

---

## Live Data Ingestion

To connect live market data feeds, configure environment variables in `.env`:

```bash
cp .env.example .env
```

Supports single-endpoint feeds or dual-source feeds (Market OHLC + Foreign Flow):

```bash
# Single endpoint:
export TA_AGENT_API_URL='https://.../GetTradingStatistics'
export TA_AGENT_API_TOKEN='...'

# Ingest historical range:
uv run python -m data.ingest --ticker VNM --start 2024-01-01 --end 2026-01-22
```

`data.ingest.extract_records` unwraps standard REST envelopes, double-encoded ASP.NET `{"d": "..."}` envelopes, and handles missing/reconciliation errors transparently.

---

## Auditability & Verification

- **Audit trail**: Every tool execution is logged with arguments, results, and latency to `logs/tool_calls.jsonl` (override with `TA_AGENT_AUDIT_LOG`). Any number an agent quotes can be verified against this audit trail.
- **Ground truth validation**: `scripts/ground_truth.py` computes reference values using only Python's standard library (`math`, `statistics`). `scripts/validate.py` checks the engine against it across 138 metrics (VNM, HPG, TNG). Current result: **138/138 checks pass** (maximum discrepancy ~2.6e-14, floating point precision).

---

## Repository Structure

```
data/            schema.sql (SQLite schema), store.py (prices store & MTF queries),
                 resample.py (1H, 4H, 1D, 3D, 1W, 1M, 1Y resampler), ingest.py (feed ingestion)
indicators/      Pure indicator modules: trend, momentum, volatility, volume_flow,
                 trade_flow, value_flow, foreign_flow + engine.py (dispatch & quality)
mcp_server/      server.py (stdio MCP server), tool_schemas.py (Pydantic validation schemas)
skills/          technical-analysis/SKILL.md — agent reasoning instructions & multi-timeframe rules
scripts/         ground_truth.py, validate.py, generate_fixtures.py, mcp_smoke.py, mock_feed.py
tests/           150 unit and integration tests (engine, indicators, store, resample, mcp server)
docs/            going_live.md, acceptance_prompts.md
```
