"""Engine tests: frame building, dispatch, data quality, serialization, refusals."""

from __future__ import annotations

import json
import math

import pytest

from indicators import is_insufficient
from indicators.engine import (
    GROUPS,
    UNSUPPORTED_METRICS,
    EngineError,
    compute,
    data_quality,
    derived_frame,
    flow_summary,
    rows_to_frame,
    serialize,
    unsupported,
)
from tests.conftest import build_frame, build_rows


# --------------------------------------------------------------------------- frames


def test_rows_to_frame_sorts_dedupes_and_casts_strings():
    # Out of order, one duplicated date, and every number arriving as a string,
    # which is how the real feed delivers them.
    rows = [
        dict(r, close=str(r["close"]), total_volume=str(r["total_volume"]))
        for r in build_rows(close=[10_000.0, 11_000.0, 12_000.0])
    ]
    rows = [rows[2], rows[0], rows[1], dict(rows[1], close="99000")]
    frame = rows_to_frame(rows)
    assert list(frame.index) == ["2026-01-05", "2026-01-06", "2026-01-07"]
    assert frame["close"].dtype == "float64"
    assert frame["close"].loc["2026-01-06"] == pytest.approx(99_000.0)  # last one wins
    assert frame["total_volume"].dtype == "float64"


def test_rows_to_frame_rejects_empty_and_incomplete_rows():
    with pytest.raises(EngineError, match="no rows"):
        rows_to_frame([])
    incomplete = build_rows(close=[10_000.0, 11_000.0])
    del incomplete[0]["foreign_room"]
    with pytest.raises(EngineError, match="missing required columns: foreign_room"):
        rows_to_frame(incomplete)


def test_derived_frame_adds_the_read_time_columns():
    frame = build_frame(
        buy_volume=[600, 400],
        sell_volume=[400, 600],
        buy_count=[30, 20],
        sell_count=[20, 30],
        total_value=[1_000.0, 2_000.0],
        total_trade=[10, 20],
        foreign_room=[1_000, 900],
    )
    out = derived_frame(frame)
    assert out["net_buy_volume"].iloc[0] == pytest.approx(200.0)
    assert out["net_buy_count"].iloc[1] == pytest.approx(-10.0)
    assert out["avg_buy_trade_size"].iloc[0] == pytest.approx(20.0)
    assert out["avg_trade_value"].iloc[1] == pytest.approx(100.0)
    assert out["foreign_room_change"].iloc[1] == pytest.approx(-100.0)
    assert math.isnan(out["foreign_room_change"].iloc[0])  # no prior room to diff


def test_zero_count_day_leaves_derived_sizes_missing_not_zero():
    frame = build_frame(buy_volume=[0, 400], buy_count=[0, 20])
    out = derived_frame(frame)
    assert math.isnan(out["avg_buy_trade_size"].iloc[0])


# --------------------------------------------------------------------------- compute


def test_compute_returns_every_requested_group_and_nothing_else():
    rows = build_rows(close=[10_000.0 + 100 * i for i in range(30)])
    result = compute(rows, ["trend", "momentum"], series_tail=3)
    assert set(result["groups"]) == {"trend", "momentum"}
    assert result["rows_used"] == 30
    assert result["date_range"]["start"] < result["date_range"]["end"]
    assert result["latest_close"] == pytest.approx(12_900.0)
    # Series are trimmed on the way out; the full history comes from get_price_data.
    assert len(result["groups"]["trend"]["sma_20"]["series"]["values"]) == 3


def test_compute_defaults_to_all_groups():
    rows = build_rows(close=[10_000.0 + 50 * i for i in range(40)])
    assert set(compute(rows).get("groups")) == set(GROUPS)


def test_compute_rejects_unknown_groups():
    rows = build_rows(close=[10_000.0, 10_100.0])
    with pytest.raises(EngineError, match="unknown group"):
        compute(rows, ["atr"])


def test_short_history_yields_markers_in_every_group_not_numbers():
    # Three rows: nothing with a 20-day window can be honestly reported.
    rows = build_rows(close=[10_000.0, 10_100.0, 10_050.0])
    groups = compute(rows)["groups"]
    assert is_insufficient(groups["trend"]["sma_20"])
    assert is_insufficient(groups["momentum"]["rsi_14"])
    assert is_insufficient(groups["volatility"]["close_to_close_volatility_20"])
    assert is_insufficient(groups["volume_flow"]["buy_sell_volume_imbalance_5"])
    assert is_insufficient(groups["trade_flow"]["avg_trade_size_by_side_20"])
    assert is_insufficient(groups["value_flow"]["value_spike_20"])
    assert is_insufficient(groups["foreign_flow"]["foreign_room_trend_5"])
    # OBV only needs two rows, so it does have a value here.
    assert groups["volume_flow"]["obv"]["latest"] is not None


def test_compute_honours_param_overrides():
    rows = build_rows(close=[10_000.0 + 100 * i for i in range(10)])
    result = compute(rows, ["trend"], {"sma_windows": [3], "ema_windows": [3]})
    trend = result["groups"]["trend"]
    assert "sma_3" in trend and "sma_20" not in trend
    assert trend["sma_3"]["latest"] == pytest.approx(10_800.0)


def test_compute_output_is_json_serializable():
    rows = build_rows(close=[10_000.0 + 100 * i for i in range(30)])
    payload = compute(rows)
    text = json.dumps(payload)  # would raise on a Series, numpy scalar or NaN
    assert "NaN" not in text


# --------------------------------------------------------------------------- flow summary


def test_flow_summary_is_narrow_and_flags_conflicting_signals():
    # Overall matched flow is sell-side while foreign money is net buying.
    rows = build_rows(
        buy_volume=[300] * 6,
        sell_volume=[700] * 6,
        buy_count=[30] * 6,
        sell_count=[70] * 6,
        foreign_buy_volume=[100] * 6,
        foreign_sell_volume=[0] * 6,
        foreign_buy_value=[1_000_000.0] * 6,
        foreign_sell_value=[0.0] * 6,
        foreign_room=[1_000_000 - 100 * i for i in range(6)],
        total_volume=[1_000] * 6,
    )
    result = flow_summary(rows, 5)
    assert result["buy_sell_volume_imbalance_avg"] == pytest.approx(-0.4)
    assert result["buy_sell_count_imbalance_avg"] == pytest.approx(-0.4)
    assert result["foreign_net_value_cum"] == pytest.approx(6_000_000.0)
    assert result["foreign_net_value_window"] == pytest.approx(5_000_000.0)
    assert result["foreign_participation_ratio_avg"] == pytest.approx(0.1)
    assert result["foreign_room_trend"] == pytest.approx(-500.0)
    assert any("net buying while" in note for note in result["notes"])


def test_flow_summary_notes_a_structural_room_change():
    rows = build_rows(
        foreign_room=[1_000_000] * 5 + [2_000_000],
        total_volume=[1_000] * 6,
    )
    result = flow_summary(rows, 5)
    assert any("ownership" in note for note in result["notes"])


def test_flow_summary_passes_markers_through_instead_of_numbers():
    rows = build_rows(close=[10_000.0, 10_100.0])
    result = flow_summary(rows, 5)
    assert is_insufficient(result["buy_sell_volume_imbalance_avg"])
    assert is_insufficient(result["foreign_room_trend"])
    # Foreign net value needs only one row, so it is a real number here.
    assert result["foreign_net_value_cum"] == pytest.approx(0.0)


# --------------------------------------------------------------------------- data quality


def test_data_quality_reports_gaps_halts_and_corporate_actions():
    frame = build_frame(
        dates=["2026-01-05", "2026-01-06", "2026-01-26", "2026-01-27"],
        close=[10_000.0, 20_000.0, 20_100.0, 20_200.0],
        prev_close=[10_000.0, 10_000.0, 10_050.0, 20_100.0],
        total_volume=[1_000, 1_000, 0, 1_000],
    )
    report = data_quality(frame)
    assert report["rows"] == 4
    assert report["calendar_gap_count"] == 1
    assert report["calendar_gaps"][0] == {
        "from": "2026-01-06",
        "to": "2026-01-26",
        "calendar_days": 20,
    }
    assert report["zero_volume_days"] == ["2026-01-26"]
    # Row 3 states a previous close of 10,050 after a 20,000 close: a 2:1 split
    # that the feed has not adjusted for.
    suspects = report["suspected_corporate_actions"]
    assert len(suspects) == 1
    assert suspects[0]["date"] == "2026-01-26"
    assert suspects[0]["ratio"] == pytest.approx(20_000.0 / 10_050.0)
    assert len(report["warnings"]) == 3
    assert any("split" in w for w in report["warnings"])
    assert any("never filled with zeros" in w for w in report["warnings"])
    assert any("halted" in w for w in report["warnings"])


def test_clean_history_produces_no_warnings():
    frame = build_frame(close=[10_000.0 + 100 * i for i in range(10)])
    report = data_quality(frame)
    assert report["warnings"] == []
    assert report["calendar_gap_count"] == 0
    assert report["suspected_corporate_action_count"] == 0


def test_data_quality_travels_with_every_compute_result():
    rows = build_rows(close=[10_000.0 + 100 * i for i in range(30)])
    assert "data_quality" in compute(rows, ["trend"])


# --------------------------------------------------------------------------- refusals


def test_serialize_maps_nan_to_null_and_trims_series():
    frame = build_frame(close=[10_000.0, 11_000.0, 12_000.0])
    payload = serialize({"s": frame["close"].pct_change(), "n": float("inf")}, series_tail=2)
    assert payload["n"] is None
    assert payload["s"]["dates"] == ["2026-01-06", "2026-01-07"]
    assert payload["s"]["values"][0] == pytest.approx(0.1)


def test_unsupported_metrics_are_refused_with_a_reason():
    for metric in ("sub_hour_intraday", "vwap", "market_breadth", "fundamentals", "news_sentiment", "ichimoku"):
        result = unsupported(metric)
        assert result["unsupported"] is True
        assert result["reason"]
    assert "tick" in unsupported("vwap")["reason"]
    assert is_insufficient(unsupported("moon_phase"))


def test_unsupported_list_covers_every_unsupported_metric():
    for metric in (
        "sub_hour_intraday",
        "vwap",
        "ichimoku",
        "market_breadth",
        "fundamentals",
        "news_sentiment",
    ):
        assert metric in UNSUPPORTED_METRICS
