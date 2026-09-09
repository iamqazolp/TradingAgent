"""Group D: order flow by volume. Buy/sell volume imbalance and OBV."""

from __future__ import annotations

import numpy as np
import pandas as pd

from indicators import finite, insufficient, latest, require, safe_div, safe_series_div

#: Relative tolerance when cross-checking `prev_close` against the prior close.
PREV_CLOSE_TOLERANCE = 0.005


def buy_sell_volume_imbalance(df: pd.DataFrame, window: int = 5) -> dict:
    """(buy - sell) / (buy + sell) on matched volume, daily and `window`-day mean.

    Range -1 (all sell-side) to +1 (all buy-side). Days with no matched volume
    (halts) become gaps, not zeros, so they drop out of the rolling mean instead
    of dragging it toward neutral.
    """
    marker = require(df.index.to_series(), window, f"buy_sell_volume_imbalance({window})")
    if marker:
        return marker
    buy = pd.to_numeric(df["buy_volume"], errors="coerce").astype("float64")
    sell = pd.to_numeric(df["sell_volume"], errors="coerce").astype("float64")
    daily = safe_series_div(buy - sell, buy + sell).rename("buy_sell_volume_imbalance")
    rolling = daily.rolling(window, min_periods=window).mean().rename(
        f"buy_sell_volume_imbalance_avg{window}"
    )
    return {
        "window": window,
        "latest": latest(daily),
        "latest_rolling_avg": latest(rolling),
        "bias": _bias(latest(rolling)),
        "series": daily,
        "rolling_series": rolling,
    }


def obv(df: pd.DataFrame, tolerance: float = PREV_CLOSE_TOLERANCE) -> dict:
    """On-Balance Volume, signed by the provider's own `prev_close`.

    Direction comes from ``close`` vs the provider's ``prev_close`` field rather
    than from the previous stored row. The two are cross-checked: if they
    disagree by more than `tolerance` (relative), or `prev_close` is absent, the
    result is an insufficient-data marker naming the offending dates. That
    disagreement is a data-quality signal, usually an unadjusted split or
    dividend, and guessing a direction through it would silently corrupt the
    running total.
    """
    required = 2
    marker = require(df.index.to_series(), required, "obv")
    if marker:
        return marker

    close = pd.to_numeric(df["close"], errors="coerce").astype("float64")
    prev_close = pd.to_numeric(df["prev_close"], errors="coerce").astype("float64")
    volume = pd.to_numeric(df["total_volume"], errors="coerce").astype("float64")

    bad_prev = [str(d) for d, v in prev_close.items() if finite(v) is None or v <= 0]
    if bad_prev:
        return insufficient(
            "prev_close is missing or non-positive on "
            f"{len(bad_prev)} row(s) (first: {bad_prev[0]}); OBV direction cannot "
            "be established without it",
            required,
            int(len(df)),
        )

    mismatches = prev_close_mismatches(close, prev_close, tolerance)
    if mismatches:
        first = mismatches[0]
        return insufficient(
            f"prev_close disagrees with the prior row's close on {len(mismatches)} "
            f"date(s) (first: {first['date']}, prev_close={first['prev_close']}, "
            f"prior close={first['prior_close']}, {first['relative_diff']:.2%}); "
            "likely an unadjusted corporate action or a data gap",
            required,
            int(len(df)),
        ) | {
            "mismatches": mismatches[:10],
            "suggested_action": (
                f"restrict the window to dates after {mismatches[-1]['date']}, or "
                "source split-adjusted closes"
            ),
        }

    direction = np.sign(close.to_numpy() - prev_close.to_numpy())
    signed = pd.Series(direction * volume.to_numpy(), index=df.index, dtype="float64")
    series = signed.cumsum().rename("obv")
    slope_window = min(len(series) - 1, 20)
    reference = finite(series.iloc[-1 - slope_window]) if slope_window > 0 else None
    current = latest(series)
    return {
        "latest": current,
        "slope_window": slope_window,
        "change_over_slope_window": (
            None if current is None or reference is None else current - reference
        ),
        "direction_counts": {
            "up": int((direction > 0).sum()),
            "down": int((direction < 0).sum()),
            "flat": int((direction == 0).sum()),
        },
        "series": series,
    }


def prev_close_mismatches(
    close: pd.Series, prev_close: pd.Series, tolerance: float = PREV_CLOSE_TOLERANCE
) -> list[dict]:
    """Dates where the provider's prev_close does not match the prior row's close.

    Also used by the engine's data-quality report, where the same disagreement is
    the fingerprint of an unadjusted corporate action.
    """
    out: list[dict] = []
    prior = close.shift(1)
    for stamp, prior_value in prior.items():
        prior_close = finite(prior_value)
        if prior_close is None or prior_close == 0:
            continue  # first row has no prior close; nothing to cross-check
        stated = finite(prev_close.loc[stamp])
        if stated is None:
            continue  # already reported by the bad_prev check
        relative = abs(stated - prior_close) / prior_close
        if relative > tolerance:
            out.append(
                {
                    "date": str(stamp),
                    "prev_close": stated,
                    "prior_close": prior_close,
                    "relative_diff": relative,
                }
            )
    return out


def _bias(value: float | None) -> str:
    if value is None:
        return "unknown"
    if value > 0.05:
        return "buy_side"
    if value < -0.05:
        return "sell_side"
    return "balanced"


def volume_ratio(df: pd.DataFrame, window: int = 20) -> dict:
    """Today's total_volume relative to its `window`-day simple moving average.

    A ratio of 1.8 means today's volume is 80% above the 20-day average.
    Useful for confirming breakouts and flagging abnormally quiet sessions.
    """
    marker = require(df.index.to_series(), window + 1, f"volume_ratio({window})")
    if marker:
        return marker
    vol = pd.to_numeric(df["total_volume"], errors="coerce").astype("float64")
    vol_sma = vol.shift(1).rolling(window, min_periods=window).mean()
    ratio = safe_series_div(vol, vol_sma).rename(f"volume_ratio_{window}")
    ratio_now = latest(ratio)
    pct_of_average = round(ratio_now * 100, 2) if ratio_now is not None else None
    flag = "normal"
    if ratio_now is not None:
        if ratio_now >= 2.0:
            flag = "very_high"
        elif ratio_now >= 1.5:
            flag = "elevated"
        elif ratio_now <= 0.5:
            flag = "very_low"
        elif ratio_now <= 0.7:
            flag = "low"
    return {
        "window": window,
        "latest": ratio_now,
        "pct_of_average": pct_of_average,
        "flag": flag,
        "series": ratio,
    }


def volume_spikes(df: pd.DataFrame, threshold: float = 1.5) -> dict:
    """Count sessions where volume exceeded `threshold` * 20-day SMA.

    Counts over 20 and 60 sessions, broken down by up-day vs down-day.
    """
    if len(df) < 22:
        return {"spikes_20d": {"total": 0, "up": 0, "down": 0}, "spikes_60d": {"total": 0, "up": 0, "down": 0}}

    vol = pd.to_numeric(df["total_volume"], errors="coerce")
    close = pd.to_numeric(df["close"], errors="coerce")
    prev_close = pd.to_numeric(df["prev_close"], errors="coerce")
    sma20 = vol.shift(1).rolling(20, min_periods=20).mean()
    is_spike = vol > (sma20 * threshold)
    is_up = close > prev_close
    is_down = close < prev_close

    def count_in_window(w: int) -> dict:
        sub_spike = is_spike.tail(w)
        sub_up = is_up.tail(w)
        sub_down = is_down.tail(w)
        total = int((sub_spike).sum())
        up_cnt = int((sub_spike & sub_up).sum())
        down_cnt = int((sub_spike & sub_down).sum())
        return {"total": total, "up": up_cnt, "down": down_cnt}

    return {
        "spikes_20d": count_in_window(20),
        "spikes_60d": count_in_window(min(60, len(df))),
    }


def obv_divergence(df: pd.DataFrame, obv_res: dict) -> dict:
    """Analyze OBV vs Price divergence over multiple windows (20, 60 rows)."""
    if not isinstance(obv_res, dict) or "obv_series" not in obv_res:
        return {"divergence_20d": "insufficient_data", "divergence_60d": "insufficient_data"}

    obv_s = obv_res["obv_series"]
    close = pd.to_numeric(df["close"], errors="coerce")
    out: dict[str, Any] = {}

    for w in (20, 60, 52):
        if len(close) <= w or len(obv_s.dropna()) <= w:
            out[f"divergence_{w}"] = "insufficient_data"
            continue
        _base = float(close.iloc[-1 - w])
        p_chg_raw = safe_div(close.iloc[-1] - _base, _base)
        p_chg = p_chg_raw * 100.0 if p_chg_raw is not None else 0.0
        o_chg = (obv_s.iloc[-1] - obv_s.iloc[-1 - w]) / 1e6  # million shares

        if p_chg > 2.0 and o_chg < -1.0:
            div = "bearish_divergence"
            desc = f"Giá tăng (+{p_chg:.1f}%) nhưng OBV giảm ({o_chg:.1f}M cp) — phân kỳ âm cảnh báo cạn lực cầu"
        elif p_chg < -2.0 and o_chg > 1.0:
            div = "bullish_divergence"
            desc = f"Giá giảm ({p_chg:.1f}%) nhưng OBV tăng (+{o_chg:.1f}M cp) — phân kỳ dương cho thấy có lực gom ngầm"
        else:
            div = "in_sync"
            desc = "Biến động giá và OBV đồng pha"

        out[f"divergence_{w}"] = {
            "status": div,
            "description": desc,
            "price_pct_change": round(p_chg, 2),
            "obv_change_mil": round(o_chg, 2),
        }
    return out


def volume_flow_group(df: pd.DataFrame, params: dict | None = None) -> dict:
    """Every Group D indicator, keyed by name."""
    params = params or {}
    window = params.get("flow_window", 5)
    vol_window = params.get("volume_ratio_window", 20)
    obv_val = obv(df, params.get("prev_close_tolerance", PREV_CLOSE_TOLERANCE))
    return {
        "buy_sell_volume_imbalance": buy_sell_volume_imbalance(df, window),
        f"buy_sell_volume_imbalance_{window}": buy_sell_volume_imbalance(df, window),
        "obv": obv_val,
        "volume_ratio": volume_ratio(df, vol_window),
        f"volume_ratio_{vol_window}": volume_ratio(df, vol_window),
        "volume_spikes": volume_spikes(df),
        "obv_divergence": obv_divergence(df, obv_val),
    }

