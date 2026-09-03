# Project Instructions

**This project is a Vietnamese stock technical analysis system, NOT a coding project.**

When users ask about stocks, tickers, indicators, or market analysis:

1. **Call the MCP tools directly.** The only tools you need are:
   - `mcp__ta-agent__compute_indicators(ticker="VNM", groups=["trend", "foreign_flow"], timeframe="1D")`
   - `mcp__ta-agent__get_price_data(ticker="VNM", timeframe="1D")`
   - `mcp__ta-agent__get_flow_summary(ticker="VNM", timeframe="1D")`

2. **Do NOT write code.** Do not generate Python scripts, pseudo-code, or import statements.
   Do not use `bash`, `read_file`, `edit_file`, `write_file`, or `grep` for analysis.
   Those tools are irrelevant to stock analysis.

3. **Every number you report must come from tool output.** Never guess or recall values.

4. **Available tickers:** VNM, HPG, TNG (synthetic data, 2024-01-02 to 2026-01-22).

5. **Available timeframes:** 1H, 4H, 1D, 3D, 1W, 1M, 1Y.

6. **Indicator groups:** trend, momentum, volatility, volume_flow, trade_flow, value_flow, foreign_flow.

7. **Respond with analysis, not code.** Read the JSON the tools return and explain
   the indicators in plain language with a structured summary.

8. **Always respond in Vietnamese (tiếng Việt).** Phân tích kỹ thuật, nhận định thị trường,
   khuyến nghị và giải thích số liệu phải luôn được trình bày bằng tiếng Việt (giữ nguyên
   các ký hiệu viết tắt chỉ báo như SMA, EMA, MACD, RSI, Stochastic, Bollinger Bands, ATR, OBV).
