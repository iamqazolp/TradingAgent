"""Tests for timeframe resampling across 1H, 4H, 1D, 3D, 1W, 1M, 1Y."""

from __future__ import annotations

import pandas as pd
import pytest

from data.resample import normalize_timeframe, resample_bars
from tests.conftest import build_frame


def test_normalize_timeframe_accepts_valid_and_aliases():
    assert normalize_timeframe("1h") == "1H"
    assert normalize_timeframe("4H") == "4H"
    assert normalize_timeframe("daily") == "1D"
    assert normalize_timeframe("3d") == "3D"
    assert normalize_timeframe("week") == "1W"
    assert normalize_timeframe("month") == "1M"
    assert normalize_timeframe("year") == "1Y"
    assert normalize_timeframe("1y") == "1Y"


def test_normalize_timeframe_rejects_invalid():
    with pytest.raises(ValueError, match="unsupported timeframe"):
        normalize_timeframe("15m")


def test_daily_to_weekly_resampling():
    # 10 weekdays: Mon-Fri (week 1), Mon-Fri (week 2)
    dates = [
        "2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09",
        "2026-01-12", "2026-01-13", "2026-01-14", "2026-01-15", "2026-01-16",
    ]
    frame = build_frame(
        dates=dates,
        open=[100, 101, 102, 103, 104, 110, 111, 112, 113, 114],
        high=[105, 106, 107, 108, 109, 115, 116, 117, 118, 119],
        low=[95, 96, 97, 98, 99, 105, 106, 107, 108, 109],
        close=[102, 103, 104, 105, 108, 112, 113, 114, 115, 118],
        total_volume=[100] * 10,
    )
    weekly = resample_bars(frame, "1W")
    assert len(weekly) == 2
    # First week (ending 2026-01-09)
    assert weekly.iloc[0]["open"] == pytest.approx(100.0)
    assert weekly.iloc[0]["high"] == pytest.approx(109.0)  # max of week 1
    assert weekly.iloc[0]["low"] == pytest.approx(95.0)    # min of week 1
    assert weekly.iloc[0]["close"] == pytest.approx(108.0) # last of week 1
    assert weekly.iloc[0]["total_volume"] == 500           # sum of week 1

    # Second week (ending 2026-01-16)
    assert weekly.iloc[1]["open"] == pytest.approx(110.0)
    assert weekly.iloc[1]["high"] == pytest.approx(119.0)
    assert weekly.iloc[1]["low"] == pytest.approx(105.0)
    assert weekly.iloc[1]["close"] == pytest.approx(118.0)
    assert weekly.iloc[1]["total_volume"] == 500
    assert weekly.iloc[1]["prev_close"] == pytest.approx(108.0)  # connected to prior close


def test_daily_to_3d_resampling():
    # 6 daily rows -> 2 3-day bars
    frame = build_frame(
        close=[10, 12, 11, 13, 15, 14],
        high=[11, 13, 12, 14, 16, 15],
        low=[9, 10, 10, 12, 13, 13],
        open=[10, 11, 12, 12, 14, 15],
        total_volume=[100, 100, 100, 200, 200, 200],
    )
    res = resample_bars(frame, "3D")
    assert len(res) == 2
    assert res.iloc[0]["open"] == pytest.approx(10.0)
    assert res.iloc[0]["high"] == pytest.approx(13.0)
    assert res.iloc[0]["low"] == pytest.approx(9.0)
    assert res.iloc[0]["close"] == pytest.approx(11.0)
    assert res.iloc[0]["total_volume"] == 300
    assert res.iloc[1]["close"] == pytest.approx(14.0)
    assert res.iloc[1]["total_volume"] == 600


def test_daily_to_monthly_and_yearly():
    dates = ["2024-01-15", "2024-06-15", "2025-01-15", "2025-06-15"]
    frame = build_frame(
        dates=dates,
        open=[10, 20, 30, 40],
        high=[15, 25, 35, 45],
        low=[5, 15, 25, 35],
        close=[12, 22, 32, 42],
        total_volume=[100, 200, 300, 400],
    )
    monthly = resample_bars(frame, "1M")
    assert len(monthly) == 4

    yearly = resample_bars(frame, "1Y")
    assert len(yearly) == 2
    assert yearly.iloc[0]["open"] == pytest.approx(10.0)
    assert yearly.iloc[0]["high"] == pytest.approx(25.0)
    assert yearly.iloc[0]["low"] == pytest.approx(5.0)
    assert yearly.iloc[0]["close"] == pytest.approx(22.0)
    assert yearly.iloc[0]["total_volume"] == 300
    assert yearly.iloc[1]["close"] == pytest.approx(42.0)
    assert yearly.iloc[1]["total_volume"] == 700


def test_hourly_to_daily_resampling():
    # 4 hourly bars on same date
    dates = [
        "2026-01-05 09:00:00",
        "2026-01-05 10:00:00",
        "2026-01-05 13:00:00",
        "2026-01-05 14:00:00",
    ]
    frame = build_frame(
        dates=dates,
        open=[100, 102, 105, 107],
        high=[103, 106, 108, 110],
        low=[99, 101, 104, 106],
        close=[102, 105, 107, 109],
        total_volume=[1000, 2000, 1500, 2500],
    )
    daily = resample_bars(frame, "1D")
    assert len(daily) == 1
    assert daily.index[0] == "2026-01-05"
    assert daily.iloc[0]["open"] == pytest.approx(100.0)
    assert daily.iloc[0]["high"] == pytest.approx(110.0)
    assert daily.iloc[0]["low"] == pytest.approx(99.0)
    assert daily.iloc[0]["close"] == pytest.approx(109.0)
    assert daily.iloc[0]["total_volume"] == 7000
