"""Group B: momentum. Wilder RSI on close."""

from __future__ import annotations

import numpy as np
import pandas as pd

from indicators import insufficient, latest, require


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


def momentum_group(close: pd.Series, params: dict | None = None) -> dict:
    """Every Group B indicator, keyed by name."""
    params = params or {}
    n = params.get("rsi_window", 14)
    return {f"rsi_{n}": rsi(close, n)}
