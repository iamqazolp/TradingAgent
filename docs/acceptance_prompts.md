# Acceptance Verification: 10 Interactive Prompts for Testing Your Agent

Run these test prompts with your AI agent connected to the `ta-agent` MCP server with `skills/technical-analysis/SKILL.md` loaded.

These prompts verify:
1. The agent calls the correct MCP tool rather than answering from memory.
2. The agent reports numbers matching ground truth rather than hallucinating.
3. The agent evaluates technical analysis group-by-group and names conflicts explicitly.
4. The agent cleanly refuses unsupported metrics (fundamentals, news, tick VWAP, sub-hour bars).

Expected values come from `tests/fixtures/ground_truth.json` (the independent stdlib reimplementation) for the fixture window **2024-11-18 → 2026-01-22 (300 trading rows)**. Tolerance is 0.5%.

Cross-check what the agent reports against `logs/tool_calls.jsonl` — every number it quotes must appear in an audit entry from that session.

| # | Prompt | Expected tool call | Expect in the answer |
|---|---|---|---|
| 1 | `What is VNM's trend?` | `compute_indicators` (trend) | SMA20 65,015, SMA50 64,756.6, SMA200 69,474.5, MACD 756.59 / signal 668.91 / hist 87.67, ADX14 20.33 (+DI 15.21, -DI 21.23, bearish bias). Price below SMA200. |
| 2 | `Is VNM overbought?` | `compute_indicators` (momentum) | RSI14 48.16 (neutral zone), Stochastic %K 69.29 / %D 81.13. No claim of extreme overbought or oversold. |
| 3 | `Where should I put a stop on HPG based on volatility?` | `compute_indicators` (volatility) | ATR14 = 1,227.6 VND, suggested stop distance (2x ATR) ≈ 2,455 VND (≈ 7.96%). Realized volatility 2.01%. |
| 4 | `Are foreigners buying VNM this week?` | `get_flow_summary` (window=5) | 5-day foreign participation 7.33%, cumulative net value −599.8bn VND, room trend +453,372. Relays structural room change note. |
| 5 | `Who is buying TNG, retail or institutions?` | `compute_indicators` (trade_flow, value_flow) | Average trade value ≈ 27.6m VND, value spike ratio 0.78 (quiet), buy ticket ratio 0.66x baseline. Reads ticket size, not just volume. |
| 6 | `What is HPG's OBV telling you?` | `compute_indicators` (volume_flow) | Cumulative OBV 994,025,060 with volume imbalance −0.434 (sell side). |
| 7 | `Show me weekly indicators for VNM.` | `compute_indicators` (timeframe="1W") | Resamples to 1W weekly bars. SMA20 = 66,941, RSI14 = 46.07. Demonstrates multi-timeframe capability. |
| 8 | `Give me 1-minute intraday bars for VNM.` | none, or tool error | Plainly refuses: feed provides hourly (1H) and higher timeframes; sub-hour minute/tick data is not supported. |
| 9 | `What's TNG's P/E ratio and any recent news?` | none | Plainly refuses both: no fundamentals and no news sentiment in this feed. No guessing from memory. |
| 10 | `Full multi-timeframe analysis of TNG.` | `compute_indicators` | Multi-timeframe synthesis across trend, momentum, volatility, order flow, foreign flow. Expects confidence level and named invalidation condition. |

Prompt 4 and prompt 10 also exercise the two data-quality paths the fixture
deliberately contains: a foreign-room move on VNM that the day's foreign trading
cannot explain, and near-zero foreign participation on a small cap (which is
normal, not missing data).

## What counts as a failure

- A number that does not appear in `logs/tool_calls.jsonl` for that session.
- Calling close-to-close volatility "ATR", or presenting `total_value /
  total_volume` as VWAP.
- Answering prompts 7 through 9 with a value instead of a refusal.
- Reporting a value where the tool returned `insufficient_data`.
- Silently averaging away a conflict between groups on prompt 10.
