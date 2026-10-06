"""Group C: volatility, adapted to a close-only feed.

There is no high/low in this feed, so there is no true ATR. The substitute here
is close-to-close realized volatility, and it is labelled as a substitute in the
payload itself (`is_atr_substitute`) so no downstream layer can present it as
ATR by accident.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from indicators import finite, insufficient, latest, require, safe_div

#: Trading days per year used for the annualized figure.
TRADING_DAYS = 252


def bollinger(close: pd.Series, n: int = 20, k: float = 2.0) -> dict:
    """Classic Bollinger Bands: SMA(n) of close +/- k population stdev of close.

    Population stdev (ddof=0), which is the classic Bollinger definition and
    what TA-Lib computes. Uses closing price only, so no high/low is needed.
    """
    marker = require(close, n, f"bollinger({n},{k})")
    if marker:
        return marker
    values = pd.to_numeric(close, errors="coerce")
    middle = values.rolling(n).mean()
    stdev = values.rolling(n).std(ddof=0)
    upper = middle + k * stdev
    lower = middle - k * stdev
    width = pd.Series(
        [safe_div(u - lo, m) for u, lo, m in zip(upper, lower, middle)],
        index=close.index,
        name="bollinger_width",
    )
    # Where the last close sits inside the band: 0 at the lower band, 1 at the upper.
    _val = finite(values.iloc[-1])
    _low = finite(lower.iloc[-1])
    _up = finite(upper.iloc[-1])
    if _val is not None and _low is not None and _up is not None:
        percent_b = safe_div(_val - _low, _up - _low)
    else:
        percent_b = None
    width_val = latest(width)
    bandwidth_pct = round(width_val * 100, 2) if width_val is not None else None
    percent_b_val = percent_b
    percent_b_pct = round(percent_b_val * 100, 2) if percent_b_val is not None else None

    # Check for Bollinger Band Squeeze (width below 20-period 10th percentile or narrow bandwidth)
    squeeze = False
    if len(width.dropna()) >= 20:
        squeeze = bool(width_val is not None and width_val <= float(width.dropna().tail(20).quantile(0.15)))

    # Qualitative position
    pos = "middle"
    if percent_b_val is not None:
        if percent_b_val >= 1.0:
            pos = "above_upper"
        elif percent_b_val >= 0.8:
            pos = "near_upper"
        elif percent_b_val <= 0.0:
            pos = "below_lower"
        elif percent_b_val <= 0.2:
            pos = "near_lower"
        else:
            pos = "inside_band"

    return {
        "window": n,
        "k": float(k),
        "latest": {
            "middle": latest(middle),
            "upper": latest(upper),
            "lower": latest(lower),
            "close": finite(values.iloc[-1]),
            "percent_b": percent_b,
            "percent_b_pct": percent_b_pct,
            "width": width_val,
            "bandwidth_pct": bandwidth_pct,
        },
        "position": pos,
        "squeeze": squeeze,
        "bandwidth": bandwidth_pct,
        "middle_series": middle.rename("bollinger_middle"),
        "upper_series": upper.rename("bollinger_upper"),
        "lower_series": lower.rename("bollinger_lower"),
        "width_series": width,
    }


def close_to_close_volatility(close: pd.Series, n: int = 20) -> dict:
    """Rolling stdev of daily log returns over `n` rows, ATR substitute."""
    marker = require(close, n + 1, f"close_to_close_volatility({n})")
    if marker:
        return marker
    required = n + 1
    values = pd.to_numeric(close, errors="coerce").astype("float64")
    if bool((values <= 0).any()):
        return insufficient(
            "close_to_close_volatility needs strictly positive closes for log returns",
            required,
            int(len(close)),
        )
    log_returns = np.log(values / values.shift(1))
    daily = log_returns.rolling(n).std(ddof=1) * 100.0
    daily.name = "close_to_close_volatility_pct"
    daily_now = latest(daily)
    if daily_now is None:
        return insufficient(
            f"close_to_close_volatility({n}) has no value on the latest row",
            required,
            int(len(close)),
        )
    return {
        "window": n,
        "label": "close-to-close realized volatility (ATR substitute, not ATR)",
        "is_atr_substitute": True,
        "latest": daily_now,
        "latest_daily_pct": daily_now,
        "latest_annualized_pct": daily_now * float(np.sqrt(TRADING_DAYS)),
        "suggested_stop_distance_pct": daily_now * 2.0,
        "series": daily,
    }


def volatility_group(close: pd.Series, params: dict | None = None) -> dict:
    """Every Group C indicator, keyed by name."""
    params = params or {}
    n_bb = params.get("bollinger_window", 20)
    k = params.get("bollinger_k", 2.0)
    n_vol = params.get("volatility_window", 20)
    bb_res = bollinger(close, n_bb, k)
    vol_res = close_to_close_volatility(close, n_vol)
    return {
        "bollinger": bb_res,
        f"bollinger_{n_bb}_{k:g}": bb_res,
        "close_to_close_vol": vol_res,
        f"close_to_close_volatility_{n_vol}": vol_res,
    }
