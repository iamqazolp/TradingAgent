# Phase 4 acceptance: 10 prompts to run interactively

The plan's Phase 4 and definition-of-done both require a live `oh` session: the
agent has to load the skill, call the MCP tools, and report numbers that match
ground truth rather than inventing them. That needs a model provider, so it needs
your credentials — run `oh setup` once, then work through the list below.

```
uv tool install 'openharness-ai==0.1.9' --with 'mcp<2' --force   # required; see docs/going_live.md
uv run python scripts/install_skill.py      # skill + MCP server into ~/.openharness
uv run python scripts/loop_smoke.py         # confirms the tools reach the model at all
oh setup                                    # your provider and model
oh                                          # interactive; /skills should list technical-analysis
```

The mechanics below the model — skill in the system prompt, tools advertised,
tool dispatched, result returned, value audited — are already verified by
`scripts/loop_smoke.py` against a scripted model. Run it first: if it fails,
these prompts will fail for reasons that have nothing to do with the model's
judgement. What follows tests only the judgement.

Expected values come from `tests/fixtures/ground_truth.json` (the independent
stdlib reimplementation, not the engine) for the fixture window
**2024-11-18 → 2026-01-22, 300 rows**. Tolerance is 0.5%; a wrong sign or a wrong
order of magnitude is a failure, not a rounding difference.

Cross-check what the agent reports against `logs/tool_calls.jsonl` — every number
it quotes should appear in an audit entry from the same session. A number with no
matching entry is a hallucination, regardless of whether it happens to be right.

| # | Prompt | Expected tool call | Expect in the answer |
|---|---|---|---|
| 1 | `What is VNM's trend?` | `compute_indicators` (trend) | SMA20 68,446, SMA50 68,184, SMA200 74,322.05, MACD 65.57 / signal 123.52 / hist −57.95. Price below SMA200 and a negative histogram, so not an aligned uptrend. |
| 2 | `Is VNM overbought?` | `compute_indicators` (momentum) | RSI14 48.57, neutral zone. No claim of overbought or oversold. |
| 3 | `Where should I put a stop on HPG?` | `compute_indicators` (volatility) | Close-to-close realized volatility 2.60% daily, suggested distance ≈5.19%. Must say this is an ATR substitute, not ATR. |
| 4 | `Are foreigners buying VNM this week?` | `get_flow_summary` (not a full pass) | 5-day foreign participation 20.6%, cumulative net value +6.47bn VND, room trend +1,651,872. Should also relay the structural-room-change note. |
| 5 | `Who is buying TNG, retail or institutions?` | `compute_indicators` (trade_flow, value_flow) | Average ticket ≈27.6m VND, value spike ratio 0.78 (quiet). Reads ticket size, not just volume. |
| 6 | `What's HPG's OBV telling you?` | `compute_indicators` (volume_flow) | OBV 541,618,180 with a 5-day volume imbalance of −0.081. Contradiction between the OBV level and current sell-side flow should be named. |
| 7 | `Give me the ATR for VNM.` | none, or a refusal | Refuses: no high/low in this feed. Offers close-to-close realized volatility (1.50% daily) as a labelled substitute. |
| 8 | `Did VNM gap up at the open today?` | none | Refuses: no open price in the feed, only close and previous close. Must not answer from the close-to-close change as though it were a gap. |
| 9 | `What's TNG's P/E and any recent news?` | none | Refuses both: no fundamentals and no news in this feed. No guessing from memory. |
| 10 | `Full analysis of TNG.` | `compute_indicators` (all groups) | Close 13,060 above SMA20 11,668 but below SMA200 14,384; RSI 60.46; percent_b 0.995 (at the upper band); foreign participation 0.19%. Expect an explicit conflict between the short-term and long-term trend, a confidence level, and a named invalidation condition. |

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
