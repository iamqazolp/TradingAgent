"""Group B: momentum. Wilder RSI, z-score of returns, close percentile, streaks."""

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
        "zone": _zone(value),
        "series": series,
    }


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    """RSI from smoothed averages. A flat window with no losses reads 100."""
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _zone(value: float) -> str:
    if value >= 70:
        return "overbought"
    if value <= 30:
        return "oversold"
    return "neutral"


def z_score(close: pd.Series, n: int = 20) -> dict:
    """Z-score of the latest daily return relative to the past `n` returns.

    Z > 2 or Z < -2 signals statistically extreme moves. Useful for detecting
    mean-reversion setups. Positive = unusually strong up day, negative = down.
    """
    required = n + 2  # need n+1 returns, which needs n+2 closes
    marker = require(close, required, f"z_score({n})")
    if marker:
        return marker
    values = pd.to_numeric(close, errors="coerce").astype("float64")
    returns = values.pct_change().dropna()
    if len(returns) < n:
        return insufficient(f"z_score({n}) needs {n} returns", n, len(returns))
    window_returns = returns.iloc[-n:]
    mean_r = float(window_returns.mean())
    std_r = float(window_returns.std(ddof=1))
    current_r = float(returns.iloc[-1])
    if std_r == 0 or np.isnan(std_r):
        z = 0.0
    else:
        z = (current_r - mean_r) / std_r
    flag = "normal"
    if z >= 2.0:
        flag = "extreme_high"
    elif z <= -2.0:
        flag = "extreme_low"
    elif z >= 1.5:
        flag = "elevated"
    elif z <= -1.5:
        flag = "depressed"
    return {
        "window": n,
        "latest": round(z, 2),
        "flag": flag,
        "daily_return_pct": round(current_r * 100, 2),
    }


def close_percentile(close: pd.Series, n: int = 20) -> dict:
    """Where today's close sits within the last `n` closes (0=at low, 1=at high).

    Close-only replacement for Stochastic %K. Values near 0 suggest oversold
    conditions; near 1 suggest overbought. Unlike Stochastic, uses close only
    so no high/low needed.
    """
    marker = require(close, n, f"close_percentile({n})")
    if marker:
        return marker
    values = pd.to_numeric(close, errors="coerce").astype("float64")
    window = values.iloc[-n:]
    hi = float(window.max())
    lo = float(window.min())
    cur = float(values.iloc[-1])
    if hi == lo:
        pct = 0.5
    else:
        pct = (cur - lo) / (hi - lo)
    zone = "middle"
    if pct >= 0.8:
        zone = "near_high"
    elif pct <= 0.2:
        zone = "near_low"
    return {
        "window": n,
        "latest": round(pct, 3),
        "zone": zone,
        "range_high": hi,
        "range_low": lo,
    }


def return_streak(close: pd.Series) -> dict:
    """Count of consecutive up or down close-to-close days at the tail.

    Positive = up streak, negative = down streak. In Vietnamese markets,
    streaks of 5+ days are statistically significant.
    """
    marker = require(close, 2, "return_streak")
    if marker:
        return marker
    values = pd.to_numeric(close, errors="coerce").astype("float64").to_numpy()
    if len(values) < 2:
        return {"streak": 0, "direction": "none"}
    diffs = np.diff(values)
    streak = 0
    direction = "flat"
    if diffs[-1] > 0:
        direction = "up"
        for d in reversed(diffs):
            if d > 0:
                streak += 1
            else:
                break
    elif diffs[-1] < 0:
        direction = "down"
        for d in reversed(diffs):
            if d < 0:
                streak += 1
            else:
                break
    flag = "normal"
    if streak >= 5:
        flag = "extended"
    elif streak >= 3:
        flag = "notable"
    return {
        "streak": streak if direction == "up" else -streak,
        "direction": direction,
        "flag": flag,
    }


def momentum_group(close: pd.Series, params: dict | None = None) -> dict:
    """Every Group B indicator, keyed by name."""
    params = params or {}
    n = params.get("rsi_window", 14)
    z_window = params.get("z_score_window", 20)
    pct_window = params.get("percentile_window", 20)
    return {
        f"rsi_{n}": rsi(close, n),
        f"z_score_{z_window}": z_score(close, z_window),
        f"close_percentile_{pct_window}": close_percentile(close, pct_window),
        "return_streak": return_streak(close),
    }

