"""Group B: momentum. Wilder RSI and Stochastic Oscillator (%K, %D)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from indicators import finite, insufficient, latest, require


def rsi(close: pd.Series, n: int = 14) -> dict:
    """Wilder's RSI over `n` rows.

    Wilder smoothing, not a simple rolling mean: the first average gain/loss is
    the mean of the first `n` deltas, then each subsequent average is
    ``(prev * (n - 1) + current) / n``. `n + 1` closes are required to produce
    `n` deltas.
    """
    required = n + 1
    marker = require(close, required, f"rsi({n})")
    if marker:
        return marker

    values = pd.to_numeric(close, errors="coerce").astype("float64").to_numpy()
    deltas = np.diff(values)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)

    out = np.full(values.shape, np.nan, dtype="float64")
    avg_gain = float(np.mean(gains[:n]))
    avg_loss = float(np.mean(losses[:n]))
    out[n] = _rsi_value(avg_gain, avg_loss)
    for i in range(n, len(deltas)):
        avg_gain = (avg_gain * (n - 1) + gains[i]) / n
        avg_loss = (avg_loss * (n - 1) + losses[i]) / n
        out[i + 1] = _rsi_value(avg_gain, avg_loss)

    series = pd.Series(out, index=close.index, name=f"rsi{n}")
    value = latest(series)
    if value is None:
        return insufficient(
            f"rsi({n}) has no value on the latest row", required, int(len(close))
        )
    return {
        "window": n,
        "latest": value,
        "zone": _rsi_zone(value),
        "series": series,
    }


def stochastic(
    data: pd.DataFrame | pd.Series,
    k_window: int = 14,
    d_window: int = 3,
    slowing: int = 3,
) -> dict:
    """Classic Stochastic Oscillator (%K, %D).

    %K = 100 * (Close - Lowest Low) / (Highest High - Lowest Low) smoothed by `slowing` SMA.
    %D = `d_window` SMA of %K.
    """
    required = k_window + slowing + d_window - 2
    if isinstance(data, pd.Series):
        return insufficient("stochastic requires high, low, and close columns (DataFrame)", required)

    marker = require(data.index.to_series(), required, f"stochastic({k_window},{d_window},{slowing})")
    if marker:
        return marker

    high = pd.to_numeric(data["high"], errors="coerce").astype("float64")
    low = pd.to_numeric(data["low"], errors="coerce").astype("float64")
    close = pd.to_numeric(data["close"], errors="coerce").astype("float64")

    lowest_low = low.rolling(k_window, min_periods=k_window).min()
    highest_high = high.rolling(k_window, min_periods=k_window).max()
    denom = highest_high - lowest_low

    fast_k = 100.0 * (close - lowest_low) / denom.where(denom != 0, other=np.nan)
    fast_k = fast_k.fillna(50.0)

    if slowing > 1:
        k_series = fast_k.rolling(slowing, min_periods=slowing).mean().rename(f"stoch_k_{k_window}")
    else:
        k_series = fast_k.rename(f"stoch_k_{k_window}")

    d_series = k_series.rolling(d_window, min_periods=d_window).mean().rename(f"stoch_d_{d_window}")

    k_now = latest(k_series)
    d_now = latest(d_series)
    k_prev = finite(k_series.iloc[-2]) if len(k_series) > 1 else None
    d_prev = finite(d_series.iloc[-2]) if len(d_series) > 1 else None

    if k_now is None or d_now is None:
        return insufficient(
            f"stochastic({k_window},{d_window},{slowing}) has no value on the latest row",
            required,
            int(len(data)),
        )

    return {
        "k_window": k_window,
        "d_window": d_window,
        "slowing": slowing,
        "latest": {"k": k_now, "d": d_now},
        "zone": _stoch_zone(k_now),
        "crossover": _stoch_crossover(k_prev, d_prev, k_now, d_now),
        "k_series": k_series,
        "d_series": d_series,
    }


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    """RSI from smoothed averages. A flat window with no losses reads 100."""
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _rsi_zone(value: float) -> str:
    if value >= 70:
        return "overbought"
    if value <= 30:
        return "oversold"
    return "neutral"


def _stoch_zone(value: float) -> str:
    if value >= 80:
        return "overbought"
    if value <= 20:
        return "oversold"
    return "neutral"


def _stoch_crossover(k_prev: float | None, d_prev: float | None, k_now: float, d_now: float) -> str:
    if k_prev is None or d_prev is None:
        return "unknown"
    if k_prev <= d_prev and k_now > d_now:
        return "bullish_cross"
    if k_prev >= d_prev and k_now < d_now:
        return "bearish_cross"
    return "none"


def momentum_group(data: pd.DataFrame | pd.Series, params: dict | None = None) -> dict:
    """Every Group B indicator, keyed by name."""
    params = params or {}
    close = data["close"] if isinstance(data, pd.DataFrame) else data
    n = params.get("rsi_window", 14)
    out = {f"rsi_{n}": rsi(close, n)}
    if isinstance(data, pd.DataFrame) and "high" in data.columns and "low" in data.columns:
        k_w = params.get("stoch_k", 14)
        d_w = params.get("stoch_d", 3)
        slowing = params.get("stoch_slowing", 3)
        out[f"stoch_{k_w}_{d_w}"] = stochastic(data, k_w, d_w, slowing)
    return out
