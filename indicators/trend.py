"""Group A: trend. SMA, EMA, MACD on close.

EMA seeding follows the TA-Lib convention: the first EMA value is the simple
average of the first `n` closes, and the recursion runs from there. This is
stated explicitly because pandas' ``ewm(adjust=False)`` seeds on the *first
observation* instead, which produces different early values and would not match
the independent ground-truth implementation in ``scripts/ground_truth.py``.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from indicators import finite, insufficient, latest, require


def ema_series(close: pd.Series, n: int) -> pd.Series:
    """SMA-seeded EMA. The first n-1 entries are NaN by construction."""
    values = pd.to_numeric(close, errors="coerce").astype("float64").to_numpy()
    out = np.full(values.shape, np.nan, dtype="float64")
    if len(values) < n:
        return pd.Series(out, index=close.index, name=f"ema{n}")
    alpha = 2.0 / (n + 1.0)
    prev = float(np.mean(values[:n]))
    out[n - 1] = prev
    for i in range(n, len(values)):
        prev = values[i] * alpha + prev * (1.0 - alpha)
        out[i] = prev
    return pd.Series(out, index=close.index, name=f"ema{n}")


def sma(close: pd.Series, n: int = 20) -> dict:
    """Rolling mean of close over `n` rows."""
    marker = require(close, n, f"sma({n})")
    if marker:
        return marker
    series = pd.to_numeric(close, errors="coerce").rolling(n).mean()
    return {"window": n, "latest": latest(series), "series": series}


def ema(close: pd.Series, n: int = 20) -> dict:
    """SMA-seeded exponential moving average of close over `n` rows."""
    marker = require(close, n, f"ema({n})")
    if marker:
        return marker
    series = ema_series(close, n)
    return {"window": n, "latest": latest(series), "series": series}


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> dict:
    """Standard MACD on close.

    The MACD line needs `slow` rows; the signal line needs a further
    `signal - 1`, so the whole indicator requires ``slow + signal - 1`` rows
    before it reports anything.
    """
    if fast >= slow:
        raise ValueError(f"fast ({fast}) must be shorter than slow ({slow})")
    required = slow + signal - 1
    marker = require(close, required, f"macd({fast},{slow},{signal})")
    if marker:
        return marker

    macd_line = ema_series(close, fast) - ema_series(close, slow)
    # The signal line is an EMA of the MACD line, which only exists from index
    # slow-1 onward. Seed it on that live section so the recursion is not
    # contaminated by leading NaNs.
    live = macd_line.dropna()
    signal_live = ema_series(live, signal)
    signal_line = signal_live.reindex(macd_line.index)
    signal_line.name = "macd_signal"
    histogram = macd_line - signal_line
    histogram.name = "macd_histogram"
    macd_line.name = "macd"

    macd_now = latest(macd_line)
    signal_now = latest(signal_line)
    hist_now = latest(histogram)
    hist_prev = finite(histogram.iloc[-2]) if len(histogram) > 1 else None
    if macd_now is None or signal_now is None:
        return insufficient(
            f"macd({fast},{slow},{signal}) has no value on the latest row",
            required,
            int(len(close)),
        )
    return {
        "fast": fast,
        "slow": slow,
        "signal": signal,
        "latest": {"macd": macd_now, "signal": signal_now, "histogram": hist_now},
        "crossover": _crossover(hist_prev, hist_now),
        "macd_series": macd_line,
        "signal_series": signal_line,
        "histogram_series": histogram,
    }


def _crossover(previous: float | None, current: float | None) -> str:
    """Describe the histogram sign change between the last two rows."""
    if previous is None or current is None:
        return "unknown"
    if previous <= 0 < current:
        return "bullish_cross"
    if previous >= 0 > current:
        return "bearish_cross"
    return "none"


def trend_group(close: pd.Series, params: dict | None = None) -> dict:
    """Every Group A indicator, keyed by name."""
    params = params or {}
    sma_windows = params.get("sma_windows", (20, 50, 200))
    ema_windows = params.get("ema_windows", (12, 26))
    out: dict[str, dict] = {}
    for n in sma_windows:
        out[f"sma_{n}"] = sma(close, n)
    for n in ema_windows:
        out[f"ema_{n}"] = ema(close, n)
    out["macd"] = macd(
        close,
        params.get("macd_fast", 12),
        params.get("macd_slow", 26),
        params.get("macd_signal", 9),
    )
    return out
