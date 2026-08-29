"""Group C: volatility. Bollinger Bands, realized volatility, and True ATR.

With high and low prices available, True ATR is computed using Wilder's 14-period
smoothing. Realized close-to-close volatility is retained as a complementary metric.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from indicators import finite, insufficient, latest, require, safe_div

#: Trading days per year used for the annualized figure.
TRADING_DAYS = 252


def atr(data: pd.DataFrame | pd.Series, n: int = 14) -> dict:
    """Wilder's Average True Range (ATR) over `n` periods.

    True Range = max(High - Low, |High - Prior Close|, |Low - Prior Close|).
    Smoothed via Wilder smoothing: initial ATR is the SMA of the first `n` TRs,
    then ATR_t = (ATR_{t-1} * (n - 1) + TR_t) / n.
    Requires `n + 1` rows to form `n` true ranges.
    """
    required = n + 1
    if isinstance(data, pd.Series):
        return insufficient("atr requires high, low, and close columns (DataFrame)", required)

    marker = require(data.index.to_series(), required, f"atr({n})")
    if marker:
        return marker

    high = pd.to_numeric(data["high"], errors="coerce").astype("float64").to_numpy()
    low = pd.to_numeric(data["low"], errors="coerce").astype("float64").to_numpy()
    close = pd.to_numeric(data["close"], errors="coerce").astype("float64").to_numpy()
    
    if "prev_close" in data.columns:
        prev_close = pd.to_numeric(data["prev_close"], errors="coerce").astype("float64").to_numpy()
    else:
        prev_close = np.roll(close, 1)
        prev_close[0] = close[0]

    # True range computation
    tr = np.zeros(len(close), dtype="float64")
    for i in range(len(close)):
        pc = close[i - 1] if i > 0 else prev_close[0]
        hl = high[i] - low[i]
        hpc = abs(high[i] - pc)
        lpc = abs(low[i] - pc)
        tr[i] = max(hl, hpc, lpc)

    out = np.full(len(close), np.nan, dtype="float64")
    # First n TR values (from index 1 to n if first row has no true prior, or 0 to n-1)
    # Using 1..n for consistent n deltas:
    initial_mean = float(np.mean(tr[1 : n + 1]))
    out[n] = initial_mean
    prev = initial_mean
    for i in range(n + 1, len(close)):
        prev = (prev * (n - 1) + tr[i]) / n
        out[i] = prev

    atr_series = pd.Series(out, index=data.index, name=f"atr{n}")
    latest_val = latest(atr_series)
    if latest_val is None:
        return insufficient(f"atr({n}) has no value on the latest row", required, int(len(data)))

    latest_close = finite(close[-1])
    atr_pct = (latest_val / latest_close * 100.0) if latest_close and latest_close > 0 else None

    return {
        "window": n,
        "latest": latest_val,
        "latest_atr": latest_val,
        "latest_atr_pct": atr_pct,
        "suggested_stop_distance": latest_val * 2.0,
        "suggested_stop_distance_pct": (atr_pct * 2.0) if atr_pct else None,
        "series": atr_series,
    }


def bollinger(close: pd.Series, n: int = 20, k: float = 2.0) -> dict:
    """Classic Bollinger Bands: SMA(n) of close +/- k population stdev of close.

    Population stdev (ddof=0), which is the classic Bollinger definition and
    what TA-Lib computes.
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
    percent_b = safe_div(finite(values.iloc[-1]) - finite(lower.iloc[-1]),
                         finite(upper.iloc[-1]) - finite(lower.iloc[-1]))
    return {
        "window": n,
        "k": float(k),
        "latest": {
            "middle": latest(middle),
            "upper": latest(upper),
            "lower": latest(lower),
            "close": finite(values.iloc[-1]),
            "percent_b": percent_b,
            "width": latest(width),
        },
        "middle_series": middle.rename("bollinger_middle"),
        "upper_series": upper.rename("bollinger_upper"),
        "lower_series": lower.rename("bollinger_lower"),
        "width_series": width,
    }


def close_to_close_volatility(close: pd.Series, n: int = 20) -> dict:
    """Rolling stdev of daily log returns of close, in percent.

    Sample stdev (ddof=1), the usual realized-volatility convention. Requires
    `n + 1` closes to form `n` returns.
    """
    required = n + 1
    marker = require(close, required, f"close_to_close_volatility({n})")
    if marker:
        return marker
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


def volatility_group(data: pd.DataFrame | pd.Series, params: dict | None = None) -> dict:
    """Every Group C indicator, keyed by name."""
    params = params or {}
    close = data["close"] if isinstance(data, pd.DataFrame) else data
    n_bb = params.get("bollinger_window", 20)
    k = params.get("bollinger_k", 2.0)
    n_vol = params.get("volatility_window", 20)
    n_atr = params.get("atr_window", 14)

    out = {
        f"bollinger_{n_bb}_{k:g}": bollinger(close, n_bb, k),
        f"close_to_close_volatility_{n_vol}": close_to_close_volatility(close, n_vol),
    }
    if isinstance(data, pd.DataFrame) and "high" in data.columns and "low" in data.columns:
        out[f"atr_{n_atr}"] = atr(data, n_atr)
    return out
