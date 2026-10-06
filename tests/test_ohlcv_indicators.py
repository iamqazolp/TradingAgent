"""Tests for OHLCV indicator enhancements, Candlestick metrics, Market Breadth & MCP tools."""

from __future__ import annotations

import math
import sqlite3
import pandas as pd
import pytest

from indicators import is_insufficient
from indicators.engine import compute, get_unsupported_metrics, rows_to_frame, unsupported
from indicators.market_breadth import market_breadth_summary
from indicators.momentum import momentum_group, stochastic
from indicators.trend import candlestick_metrics, trend_group
from indicators.volatility import atr, true_range, volatility_group
from mcp_server import server
from data import store


# --------------------------------------------------------------------------- True Range & ATR
def test_true_range_calculations():
    # Session 0: High=110, Low=100, Close=105. No prev_close -> TR = High - Low = 10
    # Session 1: High=120, Low=112, Close=118. Gap up from 105 ->
    #   H - L = 8, |H - PC| = |120 - 105| = 15, |L - PC| = |112 - 105| = 7 -> max = 15
    # Session 2: High=108, Low=95, Close=100. Gap down from 118 ->
    #   H - L = 13, |H - PC| = |108 - 118| = 10, |L - PC| = |95 - 118| = 23 -> max = 23
    # Session 3: High=102, Low=98, Close=99. Inside day (PC=100) ->
    #   H - L = 4, |H - PC| = 2, |L - PC| = 2 -> max = 4
    high = pd.Series([110.0, 120.0, 108.0, 102.0])
    low = pd.Series([100.0, 112.0, 95.0, 98.0])
    close = pd.Series([105.0, 118.0, 100.0, 99.0])

    tr = true_range(high, low, close)
    assert tr.iloc[0] == pytest.approx(10.0)
    assert tr.iloc[1] == pytest.approx(15.0)
    assert tr.iloc[2] == pytest.approx(23.0)
    assert tr.iloc[3] == pytest.approx(4.0)

    # Explicit prev_close on first row: prev_close = 102.0
    # On session 0: H-L=10, |110-102|=8, |100-102|=2 -> max = 10
    prev_close = pd.Series([102.0, 105.0, 118.0, 100.0])
    tr_pc = true_range(high, low, close, prev_close=prev_close)
    assert tr_pc.iloc[0] == pytest.approx(10.0)
    assert tr_pc.iloc[1] == pytest.approx(15.0)


def test_atr_hand_calculated_wilder_smoothing():
    # 4 bars with TRs: 10, 15, 23, 4. Window n=3.
    # Seed (row 2) = mean(10, 15, 23) = 48 / 3 = 16.0
    # Row 3 = (16.0 * (3 - 1) + 4) / 3 = (32 + 4) / 3 = 12.0
    high = pd.Series([110.0, 120.0, 108.0, 102.0])
    low = pd.Series([100.0, 112.0, 95.0, 98.0])
    close = pd.Series([105.0, 118.0, 100.0, 99.0])

    res = atr(high, low, close, n=3)
    assert res["window"] == 3
    assert res["is_atr_substitute"] is False
    assert res["label"] == "Average True Range (ATR)"
    assert res["latest"] == pytest.approx(12.0)
    assert res["series"].iloc[2] == pytest.approx(16.0)
    assert res["series"].iloc[3] == pytest.approx(12.0)
    assert res["suggested_stop_distance"] == pytest.approx(24.0)
    # latest_pct = 12.0 / 99.0 * 100 = 12.12 %
    assert res["latest_pct"] == pytest.approx(round(12.0 / 99.0 * 100, 2))


def test_atr_refuses_short_history():
    high = pd.Series([110.0, 120.0])
    low = pd.Series([100.0, 112.0])
    close = pd.Series([105.0, 118.0])

    res = atr(high, low, close, n=14)
    assert is_insufficient(res)
    assert res["required_window"] == 14


def test_volatility_group_with_and_without_high_low():
    # Without high/low: close-to-close volatility is returned with substitute flag
    close = pd.Series([100.0 + i for i in range(25)])
    vg_no_hl = volatility_group(close)
    assert "bollinger" in vg_no_hl
    assert "close_to_close_vol" in vg_no_hl
    assert vg_no_hl["close_to_close_vol"]["is_atr_substitute"] is True
    assert "atr" not in vg_no_hl

    # With high/low: atr is returned alongside close_to_close_vol
    high = close + 2.0
    low = close - 2.0
    vg_hl = volatility_group(close, params={"high": high, "low": low})
    assert "atr" in vg_hl
    assert vg_hl["atr"]["is_atr_substitute"] is False
    assert vg_hl["atr"]["label"] == "Average True Range (ATR)"
    assert "close_to_close_vol" in vg_hl


# --------------------------------------------------------------------------- Stochastic
def test_stochastic_hand_calculated():
    # Window k=3, d=2. Required = 3 + 2 - 1 = 4 rows.
    # High: 10, 12, 14, 16
    # Low:   5,  6,  7,  8
    # Close: 8, 10, 13, 12
    # Row 2 (index 2): k-window uses rows 0,1,2:
    #   Lowest Low = min(5, 6, 7) = 5
    #   Highest High = max(10, 12, 14) = 14
    #   Range = 14 - 5 = 9
    #   %K_2 = (13 - 5) / 9 * 100 = 8 / 9 * 100 = 88.8889
    # Row 3 (index 3): k-window uses rows 1,2,3:
    #   Lowest Low = min(6, 7, 8) = 6
    #   Highest High = max(12, 14, 16) = 14.. wait max(12,14,16) = 16
    #   Range = 16 - 6 = 10
    #   %K_3 = (12 - 6) / 10 * 100 = 6 / 10 * 100 = 60.0
    # %D_3 = SMA(%K, 2) = (88.8889 + 60.0) / 2 = 74.4444
    high = pd.Series([10.0, 12.0, 14.0, 16.0])
    low = pd.Series([5.0, 6.0, 7.0, 8.0])
    close = pd.Series([8.0, 10.0, 13.0, 12.0])

    res = stochastic(high, low, close, k_window=3, d_window=2)
    assert res["latest"]["k"] == pytest.approx(60.0, rel=1e-2)
    assert res["latest"]["d"] == pytest.approx(74.44, rel=1e-2)
    assert res["condition"] == "neutral"  # 60 is between 20 and 80


def test_stochastic_conditions_and_crossovers():
    # Overbought condition test (%K > 80)
    high = pd.Series([10.0, 12.0, 14.0, 16.0])
    low = pd.Series([5.0, 6.0, 7.0, 8.0])
    close = pd.Series([8.0, 10.0, 11.0, 16.0])  # Close at highest high -> %K = 100
    res_ob = stochastic(high, low, close, k_window=3, d_window=2)
    assert res_ob["k"] == 100.0
    assert res_ob["condition"] == "overbought"

    # Oversold condition test (%K < 20)
    close_os = pd.Series([8.0, 10.0, 11.0, 6.5])  # Close near lowest low (6) -> %K = 5%
    res_os = stochastic(high, low, close_os, k_window=3, d_window=2)
    assert res_os["k"] < 20.0
    assert res_os["condition"] == "oversold"

    # Bullish crossover: %K was below %D at row -2, then crossed above %D at row -1
    # Range is 10 throughout (High=20, Low=10)
    # Row 2: Close=15 -> %K=50
    # Row 3: Close=12 -> %K=20, %D=(50+20)/2=35 -> K_prev(20) <= D_prev(35)
    # Row 4: Close=18 -> %K=80, %D=(20+80)/2=50 -> K_now(80) > D_now(50) -> bullish crossover!
    high_co = pd.Series([20.0, 20.0, 20.0, 20.0, 20.0])
    low_co = pd.Series([10.0, 10.0, 10.0, 10.0, 10.0])
    close_co = pd.Series([15.0, 15.0, 15.0, 12.0, 18.0])
    res_cross = stochastic(high_co, low_co, close_co, k_window=3, d_window=2)
    assert res_cross["crossover"] == "bullish_crossover"

    # Bearish crossover: %K was above %D, then crossed below %D
    # Row 3: Close=18 -> %K=80, %D=65 -> K_prev >= D_prev
    # Row 4: Close=12 -> %K=20, %D=50 -> K_now < D_now -> bearish crossover!
    close_bear = pd.Series([15.0, 15.0, 15.0, 18.0, 12.0])
    res_bear = stochastic(high_co, low_co, close_bear, k_window=3, d_window=2)
    assert res_bear["crossover"] == "bearish_crossover"


def test_stochastic_refuses_insufficient_history():
    high = pd.Series([10.0, 12.0])
    low = pd.Series([5.0, 6.0])
    close = pd.Series([8.0, 10.0])
    res = stochastic(high, low, close, k_window=14, d_window=3)
    assert is_insufficient(res)
    assert res["required_window"] == 16


def test_momentum_group_with_and_without_high_low():
    close = pd.Series([100.0 + i for i in range(25)])
    mg_no_hl = momentum_group(close)
    assert "stochastic" not in mg_no_hl
    assert "rsi_14" in mg_no_hl

    high = close + 1.0
    low = close - 1.0
    mg_hl = momentum_group(close, params={"high": high, "low": low})
    assert "stochastic" in mg_hl
    assert "k" in mg_hl["stochastic"]
    assert "d" in mg_hl["stochastic"]


# --------------------------------------------------------------------------- Candlestick Metrics
def test_candlestick_patterns():
    # 1. Bullish Marubozu: Open=100, High=110, Low=100, Close=110
    # Range = 10, Body = 10 -> ratio = 1.0 >= 0.85, Close > Open
    cm_b_maru = candlestick_metrics(
        open_=pd.Series([100.0]),
        high=pd.Series([110.0]),
        low=pd.Series([100.0]),
        close=pd.Series([110.0]),
    )
    assert cm_b_maru["pattern"] == "bullish_marubozu"
    assert cm_b_maru["candle_body_ratio"] == 1.0
    assert cm_b_maru["upper_wick_ratio"] == 0.0
    assert cm_b_maru["lower_wick_ratio"] == 0.0

    # 2. Bearish Marubozu: Open=110, High=110, Low=100, Close=100
    cm_bear_maru = candlestick_metrics(
        open_=pd.Series([110.0]),
        high=pd.Series([110.0]),
        low=pd.Series([100.0]),
        close=pd.Series([100.0]),
    )
    assert cm_bear_maru["pattern"] == "bearish_marubozu"

    # 3. Hammer: Open=108, High=110, Low=90, Close=109
    # Range = 20. Body = 1 (ratio = 0.05.. wait, body ratio > 0.10: let's use Open=107, Close=110 -> body=3, ratio=0.15)
    # Range = 20. High = 110, Max(O,C) = 110 -> Upper wick = 0
    # Min(O,C) = 107, Low = 90 -> Lower wick = 17 -> ratio = 17/20 = 0.85 >= 0.55
    cm_hammer = candlestick_metrics(
        open_=pd.Series([107.0]),
        high=pd.Series([110.0]),
        low=pd.Series([90.0]),
        close=pd.Series([110.0]),
    )
    assert cm_hammer["pattern"] == "hammer"
    assert cm_hammer["lower_wick_ratio"] == 0.85
    assert cm_hammer["upper_wick_ratio"] == 0.0

    # 4. Shooting Star: Open=93, High=110, Low=90, Close=91
    # Range = 20. Body = 2 (ratio = 0.10.. let's use Open=93, Close=90.5 -> body=2.5, ratio=0.125)
    # Upper wick = 110 - 93 = 17 (ratio = 17/20 = 0.85 >= 0.55)
    # Lower wick = 90.5 - 90 = 0.5 (ratio = 0.5/20 = 0.025 <= 0.15)
    cm_star = candlestick_metrics(
        open_=pd.Series([93.0]),
        high=pd.Series([110.0]),
        low=pd.Series([90.0]),
        close=pd.Series([90.5]),
    )
    assert cm_star["pattern"] == "shooting_star"
    assert cm_star["upper_wick_ratio"] == 0.85

    # 5. Doji: Open=100, High=105, Low=95, Close=100.2
    # Range = 10, Body = 0.2 -> ratio = 0.02 <= 0.10
    cm_doji = candlestick_metrics(
        open_=pd.Series([100.0]),
        high=pd.Series([105.0]),
        low=pd.Series([95.0]),
        close=pd.Series([100.2]),
    )
    assert cm_doji["pattern"] == "doji"

    # 6. Standard candle
    # Open=102, High=108, Low=96, Close=104
    # Range=12, Body=2 (ratio=0.1667), Upper wick=4 (0.333), Lower wick=6 (0.50)
    cm_std = candlestick_metrics(
        open_=pd.Series([102.0]),
        high=pd.Series([108.0]),
        low=pd.Series([96.0]),
        close=pd.Series([104.0]),
    )
    assert cm_std["pattern"] == "standard"


def test_candlestick_overnight_gap():
    # PrevClose=100, Open=105 -> Overnight gap = +5.0%
    cm = candlestick_metrics(
        open_=pd.Series([105.0]),
        high=pd.Series([108.0]),
        low=pd.Series([104.0]),
        close=pd.Series([107.0]),
        prev_close=pd.Series([100.0]),
    )
    assert cm["overnight_gap_pct"] == 5.0


def test_trend_group_with_and_without_ohlc():
    close = pd.Series([100.0 + i for i in range(25)])
    tg_no_ohlc = trend_group(close)
    assert "candlestick" not in tg_no_ohlc
    assert "sma_20" in tg_no_ohlc

    open_ = close - 0.5
    high = close + 1.0
    low = close - 1.0
    tg_ohlc = trend_group(close, params={"open": open_, "high": high, "low": low})
    assert "candlestick" in tg_ohlc
    assert "pattern" in tg_ohlc["candlestick"]


# --------------------------------------------------------------------------- Engine Integration
def test_engine_compute_with_ohlcv_rows():
    # Construct rows with OHLCV data
    rows = []
    for i in range(30):
        c = 10_000.0 + 100 * i
        rows.append({
            "ticker": "HPG",
            "date": f"2026-01-{i+1:02d}",
            "open": c - 50.0,
            "high": c + 150.0,
            "low": c - 100.0,
            "close": c,
            "prev_close": c - 100.0 if i > 0 else c,
            "total_trade": 1000,
            "total_value": 50_000_000_000.0,
            "total_volume": 2_000_000,
            "buy_count": 500,
            "sell_count": 500,
            "buy_volume": 1_000_000,
            "sell_volume": 1_000_000,
            "foreign_buy_volume": 100_000,
            "foreign_sell_volume": 50_000,
            "foreign_buy_value": 2_500_000_000.0,
            "foreign_sell_value": 1_250_000_000.0,
            "foreign_room": 500_000_000,
        })

    result = compute(rows)
    groups = result["groups"]

    # Volatility group should have true ATR
    assert "atr" in groups["volatility"]
    assert groups["volatility"]["atr"]["is_atr_substitute"] is False

    # Momentum group should have Stochastic
    assert "stochastic" in groups["momentum"]
    assert "k" in groups["momentum"]["stochastic"]

    # Trend group should have candlestick metrics
    assert "candlestick" in groups["trend"]

    # Unsupported metrics should not include atr or stochastic
    unsupported_m = result.get("unsupported_metrics", {})
    assert "atr" not in unsupported_m
    assert "stochastic" not in unsupported_m


def test_engine_compute_with_close_only_rows():
    # Construct close-only rows (no high or low)
    rows = []
    for i in range(30):
        c = 10_000.0 + 100 * i
        rows.append({
            "ticker": "VNM",
            "date": f"2026-01-{i+1:02d}",
            "close": c,
            "prev_close": c - 100.0 if i > 0 else c,
            "total_trade": 1000,
            "total_value": 50_000_000_000.0,
            "total_volume": 2_000_000,
            "buy_count": 500,
            "sell_count": 500,
            "buy_volume": 1_000_000,
            "sell_volume": 1_000_000,
            "foreign_buy_volume": 100_000,
            "foreign_sell_volume": 50_000,
            "foreign_buy_value": 2_500_000_000.0,
            "foreign_sell_value": 1_250_000_000.0,
            "foreign_room": 500_000_000,
        })

    result = compute(rows)
    groups = result["groups"]
    assert "atr" not in groups["volatility"]
    assert groups["volatility"]["close_to_close_vol"]["is_atr_substitute"] is True
    assert "stochastic" not in groups["momentum"]


# --------------------------------------------------------------------------- Market Breadth
def test_market_breadth_summary_stored_rows():
    rows = [
        {
            "exchange": "VNINDEX",
            "date": "2026-01-05",
            "index_current": 1250.5,
            "index_change": 5.2,
            "index_percent_change": 0.42,
            "advances": 250,
            "declines": 100,
            "unchanged": 50,
            "total_trade": 150000,
            "total_value": 18500000000000.0,
            "total_volume": 750000000,
        },
        {
            "exchange": "HNX",
            "date": "2026-01-05",
            "index_current": 235.1,
            "index_change": -1.5,
            "index_percent_change": -0.63,
            "advances": 60,
            "declines": 150,
            "unchanged": 40,
            "total_trade": 45000,
            "total_value": 2100000000000.0,
            "total_volume": 120000000,
        },
    ]

    summary = market_breadth_summary(rows)
    assert "VNINDEX" in summary["exchanges"]
    assert "HNX" in summary["exchanges"]

    vni = summary["VNINDEX"]
    assert vni["ad_ratio"] == 2.5  # 250 / 100
    assert vni["breadth_regime"] == "strongly_bullish"  # AD ratio >= 2.0
    assert vni["total_value_vnd"] == 18500000000000.0
    assert vni["advances"] == 250
    assert vni["declines"] == 100

    hnx = summary["HNX"]
    assert hnx["ad_ratio"] == 0.4  # 60 / 150
    assert hnx["breadth_regime"] == "strongly_bearish"  # AD ratio < 0.5


def test_market_breadth_regimes():
    # Test each regime threshold
    # 1. Strongly Bullish (>= 2.0)
    r1 = market_breadth_summary([{"exchange": "TEST", "advances": 200, "declines": 100}])
    assert r1["breadth_regime"] == "strongly_bullish"

    # 2. Bullish (1.2 to 2.0)
    r2 = market_breadth_summary([{"exchange": "TEST", "advances": 150, "declines": 100}])
    assert r2["breadth_regime"] == "bullish"

    # 3. Neutral (0.8 to 1.2)
    r3 = market_breadth_summary([{"exchange": "TEST", "advances": 100, "declines": 100}])
    assert r3["breadth_regime"] == "neutral"

    # 4. Bearish (0.5 to 0.8)
    r4 = market_breadth_summary([{"exchange": "TEST", "advances": 60, "declines": 100}])
    assert r4["breadth_regime"] == "bearish"

    # 5. Strongly Bearish (< 0.5)
    r5 = market_breadth_summary([{"exchange": "TEST", "advances": 40, "declines": 100}])
    assert r5["breadth_regime"] == "strongly_bearish"

    # 6. Zero declines
    r6 = market_breadth_summary([{"exchange": "TEST", "advances": 100, "declines": 0}])
    assert r6["breadth_regime"] == "strongly_bullish"


def test_market_breadth_stockbiz_pascal_case_rows():
    # Stockbiz SOAP XML parsed dict format with PascalCase strings
    rows = [
        {
            "Exchange": "VNINDEX",
            "Date": "2026-01-05T00:00:00",
            "IndexCurrent": "1250.5",
            "IndexChange": "5.2",
            "IndexPercentChange": "0.42",
            "TotalTrade": "150000",
            "TotalValue": "18500000000000",
            "TotalVolume": "750000000",
            "Advances": "230",
            "Declines": "150",
            "Unchanged": "60",
        }
    ]

    summary = market_breadth_summary(rows)
    assert summary["exchange"] == "VNINDEX"
    assert summary["index_current"] == 1250.5
    assert summary["advances"] == 230
    assert summary["declines"] == 150
    assert summary["ad_ratio"] == pytest.approx(round(230 / 150, 2))
    assert summary["breadth_regime"] == "bullish"  # 1.53 is in [1.2, 2.0)


# --------------------------------------------------------------------------- MCP Server Tools
def test_mcp_get_market_breadth(tmp_path, monkeypatch):
    db_file = tmp_path / "test_mcp_breadth.sqlite"
    monkeypatch.setenv("TA_AGENT_DB", str(db_file))

    conn = store.connect(db_file)
    store.upsert_market_indices(
        conn,
        [
            {
                "exchange": "VNINDEX",
                "date": "2026-01-05",
                "index_current": 1250.5,
                "index_change": 5.2,
                "index_percent_change": 0.42,
                "total_trade": 150000,
                "total_value": 18500000000000.0,
                "total_volume": 750000000,
                "advances": 230,
                "declines": 150,
                "unchanged": 60,
            },
            {
                "exchange": "HNX",
                "date": "2026-01-05",
                "index_current": 235.1,
                "index_change": -1.5,
                "index_percent_change": -0.63,
                "total_trade": 45000,
                "total_value": 2100000000000.0,
                "total_volume": 120000000,
                "advances": 70,
                "declines": 95,
                "unchanged": 55,
            },
        ],
    )
    conn.close()

    # Query specific exchange
    res_vni = server.get_market_breadth(exchange="VNINDEX")
    assert res_vni["exchange"] == "VNINDEX"
    assert res_vni["advances"] == 230
    assert res_vni["declines"] == 150
    assert res_vni["ad_ratio"] == 1.53
    assert res_vni["breadth_regime"] == "bullish"

    # Query ALL
    res_all = server.get_market_breadth(exchange="ALL")
    assert "VNINDEX" in res_all["exchanges"]
    assert "HNX" in res_all["exchanges"]


def test_mcp_get_price_data_includes_ohlc(tmp_path, monkeypatch):
    db_file = tmp_path / "test_mcp_ohlc.sqlite"
    monkeypatch.setenv("TA_AGENT_DB", str(db_file))

    conn = store.connect(db_file)
    store.upsert_rows(
        conn,
        [
            {
                "ticker": "VNM",
                "date": "2026-01-05",
                "prev_close": 68000.0,
                "open": 68200.0,
                "high": 69000.0,
                "low": 67800.0,
                "close": 68500.0,
                "total_trade": 1000,
                "total_value": 100000000.0,
                "total_volume": 10000,
                "buy_count": 500,
                "sell_count": 500,
                "buy_volume": 5000,
                "sell_volume": 5000,
                "foreign_buy_volume": 100,
                "foreign_sell_volume": 100,
                "foreign_buy_value": 1000000.0,
                "foreign_sell_value": 1000000.0,
                "foreign_room": 1000000,
            }
        ],
    )
    conn.close()

    res = server.get_price_data(ticker="VNM", lookback_days=1)
    assert res["row_count"] == 1
    row = res["rows"][0]
    assert row["open"] == 68200.0
    assert row["high"] == 69000.0
    assert row["low"] == 67800.0
    assert row["close"] == 68500.0
