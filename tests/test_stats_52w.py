"""Unit tests for 52-week statistical metrics (high, low, drawdown, returns)."""

from __future__ import annotations

import pandas as pd
import pytest

from indicators.engine import rows_to_frame
from indicators.stats_52w import stats_52w
from tests.conftest import build_rows


def test_stats_52w_basic():
    # Construct 100 days of price data
    closes = [10_000.0 + i * 100 for i in range(50)] + [15_000.0 - i * 100 for i in range(50)]
    rows = build_rows(close=closes)
    frame = rows_to_frame(rows)

    res = stats_52w(frame, window=100)
    assert res["window_sessions"] == 100
    assert res["high_52w"]["price"] == 15_000.0
    assert res["low_52w"]["price"] == 10_000.0
    assert "pct_from_current" in res["high_52w"]
    assert "pct_from_current" in res["low_52w"]
    assert res["max_drawdown_pct"] < 0
    assert "avg_daily_volume_shares" in res
    assert "avg_daily_value_vnd" in res
    assert len(res["normalized_series_100"]["values"]) == 100
    assert res["normalized_series_100"]["values"][0] == 100.0


def test_stats_52w_insufficient_data():
    rows = build_rows(close=[10_000.0, 10_100.0])
    frame = rows_to_frame(rows)
    res = stats_52w(frame, window=50)
    assert res.get("insufficient_data") is True
