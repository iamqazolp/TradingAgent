"""Tests for multi-timeframe aggregation, multi-horizon analysis, and strategies."""

from __future__ import annotations

import pandas as pd
import pytest

from indicators.engine import multi_horizon_compute, rows_to_frame
from indicators.horizon import (
    classify_momentum,
    classify_trend,
    compute_key_levels,
    horizon_analysis,
    signal_strength,
)
from indicators.strategies import suggest_strategies
from indicators.weekly import aggregate_weekly, weekly_quality_flags
from data import ingest


@pytest.fixture
def sample_rows(sample_payload):
    records = ingest.extract_records(sample_payload)
    report = ingest.parse_records(records)
    vnm_rows = [r for r in report.rows if r.get("ticker") == "VNM"]
    return vnm_rows


# --------------------------------------------------------------------------- weekly aggregation


def test_aggregate_weekly_basic(sample_rows):
    frame = rows_to_frame(sample_rows)
    weekly = aggregate_weekly(frame)

    assert not weekly.empty
    assert "close" in weekly.columns
    assert "prev_close" in weekly.columns
    assert "total_volume" in weekly.columns
    assert "trading_days_in_week" in weekly.columns

    # First row's prev_close is NaN because there is no prior week
    assert pd.isna(weekly["prev_close"].iloc[0])
    # Subsequent weeks have valid prev_close equal to previous week's close
    if len(weekly) > 1:
        assert weekly["prev_close"].iloc[1] == weekly["close"].iloc[0]

    # Total weekly volume is sum of daily volumes
    first_week_date = weekly.index[0]
    # Check that trading_days_in_week is positive
    assert weekly["trading_days_in_week"].iloc[0] > 0


def test_weekly_quality_flags():
    # Build a small dummy weekly frame
    df = pd.DataFrame(
        {
            "close": [100.0, 102.0],
            "trading_days_in_week": [2, 5],
        },
        index=["2024-01-05", "2024-01-12"],
    )
    flags = weekly_quality_flags(df, thin_threshold=3)
    assert len(flags) == 1
    assert flags[0]["flag"] == "thin_week"
    assert flags[0]["trading_days"] == 2
    assert flags[0]["week_ending"] == "2024-01-05"


# --------------------------------------------------------------------------- horizon analysis


def test_classify_trend():
    # Bullish alignment
    assert classify_trend(120, 115, 110, 100) == "strong_bullish"
    # Bearish alignment
    assert classify_trend(90, 95, 100, 110) == "strong_bearish"
    # Above long-term
    assert classify_trend(105, 102, 108, 100) == "moderate_bullish"
    # Missing data
    assert classify_trend(None, 100, 90) == "insufficient_data"


def test_classify_momentum():
    assert classify_momentum(75) == "overbought"
    assert classify_momentum(25) == "oversold"
    assert "bullish" in classify_momentum(65, 0.5)
    assert "bearish" in classify_momentum(35, -0.5)
    assert classify_momentum(None) == "insufficient_data"


def test_compute_key_levels():
    sma_vals = {"SMA20": 95.0, "SMA50": 90.0, "SMA200": 110.0}
    bb = {"lower": 88.0, "upper": 112.0}
    levels = compute_key_levels(100.0, sma_vals, bb)

    assert "support" in levels
    assert "resistance" in levels
    # Supports should all be < close
    for s in levels["support"]:
        assert s["level"] < 100.0
    # Resistances should all be > close
    for r in levels["resistance"]:
        assert r["level"] > 100.0


def test_signal_strength():
    strong = signal_strength("strong_bullish", "bullish_macd_positive", "buy_side", "net_buying")
    assert strong == "strong_bullish"

    bear = signal_strength("strong_bearish", "bearish_macd_negative", "sell_side", "net_selling")
    assert bear == "strong_bearish"


# --------------------------------------------------------------------------- strategies & levels


def test_strategies_and_no_scenarios(sample_rows):
    res = multi_horizon_compute(sample_rows)
    assert res["analysis_type"] == "multi_horizon"
    assert "daily" in res
    assert "horizons" in res
    assert "scenarios" not in res
    assert "strategies" in res
    assert "levels" in res

    # Important levels exist
    assert res["levels"]["supports"] or res["levels"]["resistances"]

    # Strategies exist for all 3 horizons
    strat = res["strategies"]
    assert "short_term" in strat
    assert "mid_term" in strat
    assert "long_term" in strat


# --------------------------------------------------------------------------- mcp tool execution


def test_mcp_server_analyze_multi_horizon(sample_rows, monkeypatch):
    from data import store
    from mcp_server.server import analyze_multi_horizon

    # Point store.get_recent to return sample_rows
    monkeypatch.setattr(store, "get_recent", lambda conn, ticker, lookback: sample_rows)
    monkeypatch.setattr(store, "list_tickers", lambda conn: ["TEST"])

    result = analyze_multi_horizon(ticker="TEST", lookback_days=300)
    assert result["ticker"] == "TEST"
    assert "daily" in result
    assert "horizons" in result
    assert "scenarios" not in result
    assert "strategies" in result
    assert "levels" in result


def test_mcp_server_compute_weekly_indicators(sample_rows, monkeypatch):
    from data import store
    from mcp_server.server import compute_weekly_indicators

    monkeypatch.setattr(store, "get_recent", lambda conn, ticker, lookback: sample_rows)
    monkeypatch.setattr(store, "list_tickers", lambda conn: ["TEST"])

    result = compute_weekly_indicators(ticker="TEST", lookback_days=300, groups=["trend", "momentum"])
    assert result["ticker"] == "TEST"
    assert result["timeframe"] == "weekly"
    assert "groups" in result
    assert "trend" in result["groups"]
    assert "momentum" in result["groups"]
