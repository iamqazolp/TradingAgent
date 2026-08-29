"""Group D: order flow by volume. Buy/sell volume imbalance and OBV."""

from __future__ import annotations

import numpy as np
import pandas as pd

from indicators import finite, insufficient, latest, require, safe_series_div

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


def volume_flow_group(df: pd.DataFrame, params: dict | None = None) -> dict:
    """Every Group D indicator, keyed by name."""
    params = params or {}
    window = params.get("flow_window", 5)
    return {
        f"buy_sell_volume_imbalance_{window}": buy_sell_volume_imbalance(df, window),
        "obv": obv(df, params.get("prev_close_tolerance", PREV_CLOSE_TOLERANCE)),
    }
