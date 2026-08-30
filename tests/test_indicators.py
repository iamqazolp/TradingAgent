"""Indicator unit tests against hand-calculated values.

Every expected number below is worked out by hand from the input series in the
comment above it, so a change in convention (EMA seeding, stdev ddof, exclusive
baselines) fails a test instead of silently shifting output.

Each group is also checked for the plan's failure contract: too little history
must produce `{"insufficient_data": True, ...}`, never NaN and never a value
computed from a shorter window.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from indicators import (
    INSUFFICIENT_KEY,
    is_insufficient,
    prior_average,
    ratio_to_prior_average,
    safe_div,
)
from indicators.foreign_flow import (
    foreign_net_value,
    foreign_net_volume,
    foreign_participation_ratio,
    foreign_room_trend,
)
from indicators.momentum import rsi, stochastic
from indicators.trade_flow import avg_trade_size_by_side, buy_sell_count_imbalance
from indicators.trend import adx, ema, ema_series, ichimoku, macd, sma
from indicators.value_flow import avg_trade_value, value_spike
from indicators.volatility import atr, bollinger, close_to_close_volatility
from indicators.volume_flow import buy_sell_volume_imbalance, obv
from tests.conftest import build_frame, close_series

APPROX = dict(rel=1e-9)


def assert_insufficient(result, *, required: int, available: int | None = None):
    """The failure contract: a marker with a reason, not NaN and not an exception."""
    assert is_insufficient(result), result
    assert result[INSUFFICIENT_KEY] is True
    assert result["required_window"] == required
    assert isinstance(result["reason"], str) and result["reason"]
    if available is not None:
        assert result["available"] == available
    # No numeric value may leak out alongside the marker.
    assert "latest" not in result


# --------------------------------------------------------------------------- helpers


def test_safe_div_treats_zero_denominator_as_missing():
    assert safe_div(10, 4) == 2.5
    assert safe_div(10, 0) is None
    assert safe_div(None, 4) is None
    assert safe_div(float("nan"), 4) is None


def test_prior_average_excludes_today():
    # values 1,2,3,4: the row-4 baseline is mean(2,3) = 2.5, not mean(3,4).
    series = pd.Series([1.0, 2.0, 3.0, 4.0])
    baseline = prior_average(series, 2)
    assert math.isnan(baseline.iloc[0]) and math.isnan(baseline.iloc[1])
    assert baseline.iloc[2] == pytest.approx(1.5)
    assert baseline.iloc[3] == pytest.approx(2.5)
    assert ratio_to_prior_average(series, 2).iloc[3] == pytest.approx(4.0 / 2.5)


# --------------------------------------------------------------------------- trend


def test_sma_is_the_mean_of_the_last_n_closes():
    # closes 10,11,12,13,14 -> sma(3) = (12+13+14)/3 = 13
    result = sma(close_series([10, 11, 12, 13, 14]), 3)
    assert result["latest"] == pytest.approx(13.0)
    assert result["window"] == 3
    assert math.isnan(result["series"].iloc[1])  # no value before the window fills


def test_ema_is_sma_seeded_not_first_observation_seeded():
    # closes 10,11,12,13,14, n=3, alpha=0.5.
    # seed  = mean(10,11,12)        = 11
    # row 4 = 13*0.5 + 11*0.5       = 12
    # row 5 = 14*0.5 + 12*0.5       = 13
    series = ema_series(close_series([10, 11, 12, 13, 14]), 3)
    assert math.isnan(series.iloc[1])
    assert series.iloc[2] == pytest.approx(11.0)
    assert series.iloc[3] == pytest.approx(12.0)
    assert series.iloc[4] == pytest.approx(13.0)
    # pandas' own ewm seeds on the first observation (10, 10.5, 11.25, 12.125,
    # 13.0625), which is exactly the convention this project does not use.
    pandas_style = pd.Series([10.0, 11, 12, 13, 14]).ewm(alpha=0.5, adjust=False).mean()
    assert pandas_style.iloc[4] == pytest.approx(13.0625)
    assert ema(close_series([10, 11, 12, 13, 14]), 3)["latest"] == pytest.approx(13.0)


def test_macd_hand_calculated_with_signal_seeded_on_the_live_section():
    # closes 10,11,13,12,15 with fast=2 (a=2/3), slow=3 (a=1/2), signal=2.
    # ema2:  10.5, 12.1666667, 12.0555556, 14.0185185
    # ema3:  11.3333333, 11.6666667, 13.3333333
    # macd:  0.8333333, 0.3888889, 37/54
    # signal seeded on the live macd section: mean(0.8333333, 0.3888889) = 11/18,
    #        then (37/54)*2/3 + (11/18)/3 = 107/162
    # hist:  37/54 - 107/162 = 2/81, previous hist 7/18 - 11/18 = -2/9 -> bullish
    result = macd(close_series([10, 11, 13, 12, 15]), fast=2, slow=3, signal=2)
    assert result["latest"]["macd"] == pytest.approx(37 / 54, **APPROX)
    assert result["latest"]["signal"] == pytest.approx(107 / 162, **APPROX)
    assert result["latest"]["histogram"] == pytest.approx(2 / 81, **APPROX)
    assert result["crossover"] == "bullish_cross"
    assert result["histogram_series"].iloc[-2] == pytest.approx(-2 / 9, **APPROX)


def test_macd_needs_slow_plus_signal_minus_one_rows():
    # fast=2, slow=3, signal=2 -> 4 rows required; 3 rows must refuse.
    assert_insufficient(
        macd(close_series([10, 11, 13]), fast=2, slow=3, signal=2), required=4, available=3
    )


def test_macd_rejects_fast_not_shorter_than_slow():
    with pytest.raises(ValueError, match="must be shorter"):
        macd(close_series([10.0] * 40), fast=26, slow=12)


def test_trend_refuses_short_history():
    assert_insufficient(sma(close_series([10, 11]), 20), required=20, available=2)
    assert_insufficient(ema(close_series([10, 11]), 20), required=20, available=2)


# --------------------------------------------------------------------------- momentum


def test_rsi_wilder_smoothing_hand_calculated():
    # closes 10, 11, 10.5, 12 with n=2. deltas +1, -0.5, +1.5.
    # first averages: gain (1+0)/2 = 0.5, loss (0+0.5)/2 = 0.25 -> RS 2 -> 66.6667
    # then: gain (0.5*1 + 1.5)/2 = 1.0, loss (0.25*1 + 0)/2 = 0.125 -> RS 8 -> 88.8889
    result = rsi(close_series([10, 11, 10.5, 12]), 2)
    assert result["series"].iloc[2] == pytest.approx(66.6666666667, **APPROX)
    assert result["latest"] == pytest.approx(88.8888888889, **APPROX)
    assert result["zone"] == "overbought"


def test_rsi_edge_cases_do_not_divide_by_zero():
    # No losses at all reads 100; a completely flat window reads 50, not NaN.
    assert rsi(close_series([10, 11, 12]), 2)["latest"] == pytest.approx(100.0)
    assert rsi(close_series([10, 10, 10]), 2)["latest"] == pytest.approx(50.0)
    assert rsi(close_series([12, 11, 10]), 2)["zone"] == "oversold"


def test_rsi_needs_n_plus_one_closes():
    assert_insufficient(rsi(close_series([10, 11]), 14), required=15, available=2)


# --------------------------------------------------------------------------- volatility


def test_bollinger_uses_population_stdev():
    # closes 10,12,14, n=3, k=2. mean 12, population stdev sqrt(8/3) = 1.6329932.
    # upper 15.2659863, lower 8.7340137, width 6.5319726/12 = 0.5443311,
    # percent_b (14 - 8.7340137) / 6.5319726 = 0.8061862
    result = bollinger(close_series([10, 12, 14]), 3, 2.0)
    assert result["latest"]["middle"] == pytest.approx(12.0)
    assert result["latest"]["upper"] == pytest.approx(15.2659863237, **APPROX)
    assert result["latest"]["lower"] == pytest.approx(8.7340136763, **APPROX)
    assert result["latest"]["width"] == pytest.approx(0.5443310540, **APPROX)
    assert result["latest"]["percent_b"] == pytest.approx(0.8061862178, **APPROX)
    # Sample stdev (ddof=1) would be 2.0 and give upper 16.0; that is the wrong
    # convention for Bollinger and must not be what we compute.
    assert result["latest"]["upper"] != pytest.approx(16.0)


def test_close_to_close_volatility_is_a_labelled_atr_substitute():
    # closes 100, 110, 110, n=2. Log returns ln(1.1) and 0, so the sample stdev
    # (ddof=1) of the pair is ln(1.1)/sqrt(2) = 6.7394474 %.
    expected = math.log(1.1) / math.sqrt(2) * 100
    result = close_to_close_volatility(close_series([100, 110, 110]), 2)
    assert expected == pytest.approx(6.7394474, rel=1e-7)  # pin the arithmetic itself
    assert result["latest_daily_pct"] == pytest.approx(expected, **APPROX)
    assert result["latest_annualized_pct"] == pytest.approx(
        expected * math.sqrt(252), **APPROX
    )
    assert result["suggested_stop_distance_pct"] == pytest.approx(2 * expected, **APPROX)
    # The label is part of the contract: nothing downstream may call this ATR.
    assert result["is_atr_substitute"] is True
    assert "not ATR" in result["label"]


def test_close_to_close_volatility_refuses_non_positive_closes():
    # A zero close would make log returns undefined; that is a data problem, not
    # a number to invent.
    result = close_to_close_volatility(close_series([100, 0, 110]), 2)
    assert_insufficient(result, required=3)
    assert "positive" in result["reason"]


def test_volatility_refuses_short_history():
    assert_insufficient(bollinger(close_series([10, 12]), 20), required=20, available=2)
    assert_insufficient(
        close_to_close_volatility(close_series([10, 12]), 20), required=21, available=2
    )


# --------------------------------------------------------------------------- volume flow


def test_buy_sell_volume_imbalance_hand_calculated():
    # buy/sell 60/40 -> +0.2, 30/70 -> -0.4, 50/50 -> 0.0
    # rolling mean of the last 2 = (-0.4 + 0.0)/2 = -0.2 -> sell-side bias
    frame = build_frame(
        buy_volume=[60, 30, 50],
        sell_volume=[40, 70, 50],
        total_volume=[100, 100, 100],
    )
    result = buy_sell_volume_imbalance(frame, 2)
    assert result["series"].iloc[0] == pytest.approx(0.2)
    assert result["latest"] == pytest.approx(0.0)
    assert result["latest_rolling_avg"] == pytest.approx(-0.2)
    assert result["bias"] == "sell_side"


def test_halted_day_is_a_gap_not_a_zero_imbalance():
    # A day with no matched volume has no imbalance. It must drop out of the
    # rolling mean rather than being counted as perfectly balanced.
    frame = build_frame(
        buy_volume=[60, 0, 50],
        sell_volume=[40, 0, 50],
        total_volume=[100, 0, 100],
    )
    result = buy_sell_volume_imbalance(frame, 2)
    assert result["series"].iloc[1] is None or math.isnan(result["series"].iloc[1])
    assert result["latest_rolling_avg"] is None
    assert result["bias"] == "unknown"


def test_obv_signs_volume_by_the_providers_prev_close():
    # +100 (up day), -200 (down day), 0 (unchanged) -> cumulative -100
    frame = build_frame(
        close=[11_000, 10_000, 10_000],
        prev_close=[10_000, 11_000, 10_000],
        total_volume=[100, 200, 50],
    )
    result = obv(frame)
    assert result["latest"] == pytest.approx(-100.0)
    assert result["direction_counts"] == {"up": 1, "down": 1, "flat": 1}
    assert result["change_over_slope_window"] == pytest.approx(-200.0)
    assert list(result["series"]) == [100.0, -100.0, -100.0]


def test_obv_refuses_when_prev_close_contradicts_the_prior_row():
    # Row 2 claims a previous close of 20,000 while row 1 closed at 11,000: the
    # fingerprint of an unadjusted corporate action. OBV must refuse and name it.
    frame = build_frame(
        close=[11_000, 10_000, 10_000],
        prev_close=[10_000, 20_000, 10_000],
        total_volume=[100, 200, 50],
    )
    result = obv(frame)
    assert_insufficient(result, required=2)
    assert result["mismatches"][0]["prev_close"] == pytest.approx(20_000.0)
    assert result["mismatches"][0]["prior_close"] == pytest.approx(11_000.0)
    assert "corporate action" in result["reason"]
    assert "suggested_action" in result


def test_obv_refuses_when_prev_close_is_missing():
    frame = build_frame(
        close=[11_000, 10_000], prev_close=[0, 11_000], total_volume=[100, 200]
    )
    result = obv(frame)
    assert_insufficient(result, required=2)
    assert "prev_close is missing or non-positive" in result["reason"]


# --------------------------------------------------------------------------- trade flow


def test_buy_sell_count_imbalance_hand_calculated():
    # counts 60/40 -> +0.2, 10/30 -> -0.5, 50/50 -> 0.0; last-2 mean = -0.25
    frame = build_frame(buy_count=[60, 10, 50], sell_count=[40, 30, 50])
    result = buy_sell_count_imbalance(frame, 2)
    assert result["latest"] == pytest.approx(0.0)
    assert result["latest_rolling_avg"] == pytest.approx(-0.25)


def test_avg_trade_size_by_side_compares_each_side_to_its_own_baseline():
    # buy size 10, 20, 30 -> baseline mean(10,20) = 15 -> ratio 2.0
    # sell size flat at 10 -> ratio 1.0. Bigger buy tickets, ordinary sell tickets.
    frame = build_frame(
        buy_volume=[100, 200, 450],
        buy_count=[10, 10, 15],
        sell_volume=[100, 100, 150],
        sell_count=[10, 10, 15],
    )
    result = avg_trade_size_by_side(frame, 2)
    latest = result["latest"]
    assert latest["avg_buy_trade_size"] == pytest.approx(30.0)
    assert latest["avg_buy_trade_size_baseline"] == pytest.approx(15.0)
    assert latest["buy_ratio_to_baseline"] == pytest.approx(2.0)
    assert latest["sell_ratio_to_baseline"] == pytest.approx(1.0)
    assert latest["buy_vs_sell_size"] == pytest.approx(3.0)
    assert result["interpretation"] == "larger_buy_tickets"


def test_avg_trade_size_needs_window_plus_one_rows_for_an_exclusive_baseline():
    frame = build_frame(buy_volume=[100, 200], buy_count=[10, 10])
    assert_insufficient(avg_trade_size_by_side(frame, 2), required=3, available=2)


# --------------------------------------------------------------------------- value flow


def test_avg_trade_value_and_value_spike_hand_calculated():
    # ticket value 100, 200, 300 -> baseline 150 -> ratio 2.0 (large tickets)
    # total value 1000, 2000, 4500 -> baseline 1500 -> ratio 3.0 (spike)
    frame = build_frame(
        total_value=[1_000, 2_000, 4_500],
        total_trade=[10, 10, 15],
    )
    ticket = avg_trade_value(frame, 2)
    assert ticket["latest_vnd"] == pytest.approx(300.0)
    assert ticket["baseline_vnd"] == pytest.approx(150.0)
    assert ticket["ratio_to_baseline"] == pytest.approx(2.0)
    assert ticket["flag"] == "unusually_large_tickets"

    spike = value_spike(frame, 2)
    assert spike["latest_value_vnd"] == pytest.approx(4_500.0)
    assert spike["baseline_vnd"] == pytest.approx(1_500.0)
    assert spike["ratio_to_baseline"] == pytest.approx(3.0)
    assert spike["flag"] == "value_spike"


def test_zero_trade_day_leaves_avg_trade_value_missing():
    frame = build_frame(total_value=[1_000, 0, 4_500], total_trade=[10, 0, 15])
    result = avg_trade_value(frame, 2)
    # The halted day has no ticket size, so the exclusive baseline cannot form.
    assert result["baseline_vnd"] is None
    assert result["ratio_to_baseline"] is None
    assert result["flag"] == "unknown"


def test_value_flow_refuses_short_history():
    frame = build_frame(total_value=[1_000, 2_000], total_trade=[10, 10])
    assert_insufficient(avg_trade_value(frame, 20), required=21, available=2)
    assert_insufficient(value_spike(frame, 20), required=21, available=2)


# --------------------------------------------------------------------------- foreign flow


def test_foreign_net_volume_and_value():
    frame = build_frame(
        foreign_buy_volume=[100, 0, 50],
        foreign_sell_volume=[0, 200, 50],
        foreign_buy_value=[1_000, 0, 500],
        foreign_sell_value=[0, 2_000, 500],
    )
    volume = foreign_net_volume(frame)
    assert volume["latest"] == pytest.approx(0.0)
    assert volume["cumulative"] == pytest.approx(-100.0)
    assert volume["days_with_zero_foreign_activity"] == 0

    value = foreign_net_value(frame)
    assert value["cumulative_vnd"] == pytest.approx(-1_000.0)
    assert value["stance"] == "net_selling"


def test_zero_foreign_activity_is_a_legitimate_reading():
    frame = build_frame(
        foreign_buy_volume=[0, 0, 0],
        foreign_sell_volume=[0, 0, 0],
        total_volume=[100, 100, 100],
    )
    assert foreign_net_volume(frame)["days_with_zero_foreign_activity"] == 3
    assert foreign_participation_ratio(frame, 2)["latest_rolling_avg"] == pytest.approx(0.0)
    assert foreign_net_value(frame)["stance"] == "flat"


def test_foreign_participation_ratio_hand_calculated():
    # (100+0)/1000 = 0.1, (0+200)/1000 = 0.2, (50+50)/500 = 0.2; last-2 mean 0.2
    frame = build_frame(
        foreign_buy_volume=[100, 0, 50],
        foreign_sell_volume=[0, 200, 50],
        total_volume=[1_000, 1_000, 500],
    )
    result = foreign_participation_ratio(frame, 2)
    assert result["series"].iloc[0] == pytest.approx(0.1)
    assert result["latest"] == pytest.approx(0.2)
    assert result["latest_rolling_avg"] == pytest.approx(0.2)


def test_foreign_room_trend_reads_shrinking_room_as_accumulation():
    # Room 1000 -> 900 -> 800, each fall matched by that day's foreign buying, so
    # nothing is flagged as structural.
    frame = build_frame(
        foreign_room=[1_000, 900, 800],
        foreign_buy_volume=[0, 100, 100],
        foreign_sell_volume=[0, 0, 0],
        total_volume=[1_000, 1_000, 1_000],
    )
    result = foreign_room_trend(frame, 2)
    assert result["latest"] == pytest.approx(-200.0)
    assert result["latest_room"] == pytest.approx(800.0)
    assert result["reading"] == "room_shrinking_foreign_accumulation"
    assert result["suspected_structural_changes"] == []


def test_room_change_without_matching_foreign_trades_is_flagged_structural():
    # Room jumps by 500,000 shares on a day with no foreign trading at all: an
    # ownership-limit or charter-capital change, not flow.
    frame = build_frame(
        foreign_room=[1_000_000, 1_000_000, 1_500_000],
        foreign_buy_volume=[0, 0, 0],
        foreign_sell_volume=[0, 0, 0],
        total_volume=[1_000, 1_000, 1_000],
    )
    result = foreign_room_trend(frame, 2)
    flagged = result["suspected_structural_changes"]
    assert len(flagged) == 1
    assert flagged[0]["room_change"] == pytest.approx(500_000.0)
    assert flagged[0]["unexplained"] == pytest.approx(500_000.0)
    assert "charter capital" in flagged[0]["note"]


def test_foreign_room_trend_needs_window_plus_one_rows():
    frame = build_frame(foreign_room=[1_000, 900])
    assert_insufficient(foreign_room_trend(frame, 5), required=6, available=2)


# --------------------------------------------------------------------------- Phase 2: ATR, ADX, Stochastic


def test_atr_hand_calculated():
    # Closes: 100, 105, 102, 108
    # Highs:  102, 107, 104, 110
    # Lows:   98,  103, 100, 105
    # TRs:    TR1=max(4, 7, 3)=7, TR2=max(4, 1, 5)=5, TR3=max(5, 8, 3)=8
    # n=2 -> initial ATR at idx 2: (7+5)/2 = 6.0
    # idx 3: (6.0 * 1 + 8.0)/2 = 7.0
    frame = build_frame(
        close=[100.0, 105.0, 102.0, 108.0],
        high=[102.0, 107.0, 104.0, 110.0],
        low=[98.0, 103.0, 100.0, 105.0],
        open=[100.0, 104.0, 103.0, 106.0],
    )
    result = atr(frame, 2)
    assert result["latest_atr"] == pytest.approx(7.0)
    assert result["suggested_stop_distance"] == pytest.approx(14.0)
    assert result["latest_atr_pct"] == pytest.approx(7.0 / 108.0 * 100.0)


def test_atr_requires_window_plus_one():
    frame = build_frame(close=[100.0, 105.0])
    assert_insufficient(atr(frame, 5), required=6)


def test_adx_hand_calculated():
    frame = build_frame(
        close=[10.0, 12.0, 14.0, 13.0, 16.0, 18.0],
        high=[11.0, 13.0, 15.0, 14.0, 17.0, 19.0],
        low=[9.0, 11.0, 13.0, 12.0, 15.0, 17.0],
    )
    result = adx(frame, 2)
    assert "adx" in result["latest"]
    assert result["latest"]["adx"] > 0
    assert result["directional_bias"] == "bullish"
    assert result["trend_strength"] in ("trending", "very_strong_trend", "emerging_trend")


def test_adx_requires_double_window():
    frame = build_frame(close=[10.0, 12.0, 13.0])
    assert_insufficient(adx(frame, 2), required=4)


def test_adx_flat_halted_period_no_nan_cascade():
    # 10 bars with flat trading (high=low=close=10.0) followed by trending bars
    frame = build_frame(
        close=[10.0] * 5 + [11.0, 12.0, 13.0, 14.0, 15.0],
        high=[10.0] * 5 + [11.5, 12.5, 13.5, 14.5, 15.5],
        low=[10.0] * 5 + [10.5, 11.5, 12.5, 13.5, 14.5],
    )
    result = adx(frame, 3)
    assert result["latest"]["adx"] is not None
    assert not np.isnan(result["latest"]["adx"])
    assert result["latest"]["plus_di"] is not None
    assert result["latest"]["minus_di"] is not None


def test_stochastic_hand_calculated():
    # k=3, d=2, slowing=1
    frame = build_frame(
        high=[10.0, 12.0, 14.0, 16.0, 15.0],
        low=[8.0, 9.0, 10.0, 12.0, 11.0],
        close=[9.0, 11.0, 13.0, 15.0, 12.0],
    )
    result = stochastic(frame, k_window=3, d_window=2, slowing=1)
    assert result["latest"]["k"] == pytest.approx(33.333333333333336)
    assert result["latest"]["d"] == pytest.approx(59.523809523809526)
    assert result["zone"] == "neutral"


def test_stochastic_crossover_and_zones():
    # Bullish cross into overbought
    frame = build_frame(
        high=[10.0, 12.0, 14.0, 16.0, 20.0],
        low=[8.0, 9.0, 10.0, 11.0, 18.0],
        close=[9.0, 10.0, 11.0, 15.0, 20.0],
    )
    result = stochastic(frame, k_window=3, d_window=2, slowing=1)
    assert result["zone"] == "overbought"


def test_ichimoku_hand_calculated():
    # 6 bars: tenkan=3, kijun=4, senkou_b=5
    # Row 5 (last bar):
    # past 3 highs: [18, 20, 22] -> max 22, lows: [14, 16, 18] -> min 14 => Tenkan = (22+14)/2 = 18.0
    # past 4 highs: [16, 18, 20, 22] -> max 22, lows: [12, 14, 16, 18] -> min 12 => Kijun = (22+12)/2 = 17.0
    # Span A = (18+17)/2 = 17.5
    # past 5 highs: [14, 16, 18, 20, 22] -> max 22, lows: [10, 12, 14, 16, 18] -> min 10 => Span B = (22+10)/2 = 16.0
    # Close = 20.0
    frame = build_frame(
        high=[12.0, 14.0, 16.0, 18.0, 20.0, 22.0],
        low=[8.0, 10.0, 12.0, 14.0, 16.0, 18.0],
        close=[10.0, 12.0, 14.0, 16.0, 18.0, 20.0],
    )
    result = ichimoku(frame, tenkan_n=3, kijun_n=4, senkou_b_n=5)
    assert result["latest"]["tenkan_sen"] == pytest.approx(18.0)
    assert result["latest"]["kijun_sen"] == pytest.approx(17.0)
    assert result["latest"]["senkou_span_a"] == pytest.approx(17.5)
    assert result["latest"]["senkou_span_b"] == pytest.approx(16.0)
    assert result["latest"]["chikou_span"] == pytest.approx(20.0)
    assert result["kumo_sentiment"] == "bullish"
    assert result["price_vs_cloud"] == "above_cloud"
    assert result["cloud_thickness"] == pytest.approx(1.5)
    assert result["cloud_thickness_pct"] == pytest.approx(1.5 / 20.0 * 100.0)


def test_ichimoku_requires_senkou_b_window():
    frame = build_frame(close=[10.0] * 10)
    assert_insufficient(ichimoku(frame, 9, 26, 52), required=52)


