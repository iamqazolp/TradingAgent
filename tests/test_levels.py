"""Tests for close-based support/resistance levels."""

from __future__ import annotations

import pandas as pd
import pytest

from indicators.engine import compute, rows_to_frame
from indicators.levels import close_extremes, key_levels, swing_points
from tests.conftest import build_rows


def test_swing_points_finds_confirmed_extrema_only():
    # A clear peak at index 5 and trough at index 11.
    closes = [100, 102, 104, 106, 108, 120, 108, 106, 104, 102, 100, 90, 95, 100, 104, 108]
    series = pd.Series(
        [float(c) for c in closes],
        index=[f"2024-01-{i + 1:02d}" for i in range(len(closes))],
    )
    result = swing_points(series, lookback=3)

    highs = {entry["level"] for entry in result["highs"]}
    lows = {entry["level"] for entry in result["lows"]}
    assert 120.0 in highs
    assert 90.0 in lows
    # The last `lookback` sessions cannot host a confirmed swing.
    assert result["confirmed_through"] == series.index[len(closes) - 3 - 1]


def test_close_extremes_omits_windows_longer_than_history():
    rows = build_rows(ticker="AAA", close=[10_000.0 + i * 10 for i in range(30)])
    frame = rows_to_frame(rows)
    result = close_extremes(frame["close"], windows=(20, 60, 250))

    assert "20d" in result
    # 60 and 250 sessions of history do not exist, so no key claims them.
    assert "60d" not in result
    assert "250d" not in result
    assert result["20d"]["high"] >= result["20d"]["low"]


def test_key_levels_supports_below_resistances_above():
    rows = build_rows(ticker="AAA", close=[10_000.0 + (i % 11) * 120 for i in range(120)])
    frame = rows_to_frame(rows)
    groups = compute(rows, ["trend", "volatility"], series_tail=0)["groups"]

    result = key_levels(frame, groups["trend"], groups["volatility"])
    close = result["latest_close"]

    assert result["supports"], "expected at least one support"
    assert result["resistances"], "expected at least one resistance"
    for entry in result["supports"]:
        assert entry["level"] < close
    for entry in result["resistances"]:
        assert entry["level"] > close
    # Nearest first.
    assert result["supports"] == sorted(result["supports"], key=lambda e: -e["level"])
    assert result["resistances"] == sorted(result["resistances"], key=lambda e: e["level"])


def test_key_levels_every_level_names_a_real_basis():
    """No level may appear without saying where it came from.

    The replaced pivot implementation published seven levels derived from an
    invented intraday range; nothing downstream could tell them from measured
    ones.
    """
    rows = build_rows(ticker="AAA", close=[10_000.0 + (i % 9) * 90 for i in range(150)])
    frame = rows_to_frame(rows)
    groups = compute(rows, ["trend", "volatility"], series_tail=0)["groups"]

    result = key_levels(frame, groups["trend"], groups["volatility"])
    for entry in result["supports"] + result["resistances"]:
        assert entry["basis"], "level with no basis"
        assert entry["bases"], "level with no basis list"
        assert entry["confluence"] >= 1
        assert entry["distance_pct"] is not None


def test_key_levels_position_is_not_just_the_days_direction():
    """Position must vary with structure, not merely with today's close change.

    The old pivot `position` was algebraically a restatement of
    `close > prev_close`: across 1,530 fixture sessions only two of its seven
    branches ever occurred, split exactly on up days versus down days.
    """
    readings_on_up_days = set()
    rows = build_rows(
        ticker="AAA",
        close=[10_000.0 + (i % 17) * 150 - (i % 5) * 40 for i in range(200)],
    )
    frame = rows_to_frame(rows)
    groups_cache = compute(rows, ["trend", "volatility"], series_tail=0)["groups"]

    for end in range(120, len(frame), 7):
        sub = frame.iloc[:end]
        if sub["close"].iloc[-1] <= sub["prev_close"].iloc[-1]:
            continue
        result = key_levels(sub, groups_cache["trend"], None)
        if not isinstance(result, dict) or "position" not in result:
            continue
        readings_on_up_days.add(result["position"]["reading"])

    # More than one reading must be reachable on up days alone.
    assert len(readings_on_up_days) > 1, (
        f"position collapsed to a single reading on up days: {readings_on_up_days}"
    )


def test_key_levels_refuses_without_close():
    frame = pd.DataFrame({"other": [1.0, 2.0]}, index=["2024-01-01", "2024-01-02"])
    result = key_levels(frame)
    assert result["insufficient_data"] is True
