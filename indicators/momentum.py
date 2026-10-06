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


def close_percentile_by_window(close: pd.Series, windows=(20, 60, 126)) -> dict:
    """Extends the existing single-window close_percentile to several windows.
    Percentile is close-based (highest/lowest CLOSE in the window), explicitly
    not a true high/low range, must be labeled as such wherever surfaced."""
    out = {}
    n = len(close)
    for w in windows:
        key = f"{w}d"
        if n < w:
            continue  # insufficient_data, omit the key
        window = close.iloc[-w:]
        lo, hi = window.min(), window.max()
        latest = close.iloc[-1]
        if hi == lo:
            pct = 0.5  # flat window, avoid divide by zero, midpoint is the honest answer
        else:
            pct = (latest - lo) / (hi - lo)
        out[key] = {
            "value": round(float(pct), 3),
            "range_high": round(float(hi), 2),
            "range_low": round(float(lo), 2),
        }
    return out


def stochastic(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    k_window: int = 14,
    d_window: int = 3,
) -> dict:
    """Stochastic Oscillator (%K, %D).

    %K = (Close - LowestLow(k)) / (HighestHigh(k) - LowestLow(k)) * 100
    %D = SMA(%K, d)
    """
    required = k_window + d_window - 1
    marker = require(close, required, f"stochastic({k_window},{d_window})")
    if marker:
        return marker

    h = pd.to_numeric(high, errors="coerce").astype("float64")
    l = pd.to_numeric(low, errors="coerce").astype("float64")
    c = pd.to_numeric(close, errors="coerce").astype("float64")

    if h.dropna().empty or l.dropna().empty:
        return insufficient(
            f"stochastic({k_window},{d_window}) requires valid high and low prices",
            required,
            int(len(close)),
        )

    lowest_low = l.rolling(k_window).min()
    highest_high = h.rolling(k_window).max()
    rng = highest_high - lowest_low

    # When rng == 0 (flat window), %K is 50.0
    pct_k = ((c - lowest_low) / rng.where(rng != 0, np.nan)) * 100.0
    pct_k = pct_k.fillna(50.0).where(lowest_low.notna() & highest_high.notna(), np.nan)
    pct_k.name = f"stochastic_k_{k_window}"

    pct_d = pct_k.rolling(d_window).mean()
    pct_d.name = f"stochastic_d_{d_window}"

    k_now = latest(pct_k)
    d_now = latest(pct_d)

    if k_now is None or d_now is None:
        return insufficient(
            f"stochastic({k_window},{d_window}) has no value on the latest row",
            required,
            int(len(close)),
        )

    if k_now > 80:
        condition = "overbought"
    elif k_now < 20:
        condition = "oversold"
    else:
        condition = "neutral"

    crossover = None
    if len(pct_k) >= 2 and len(pct_d) >= 2:
        k_prev = finite(pct_k.iloc[-2])
        d_prev = finite(pct_d.iloc[-2])
        if k_prev is not None and d_prev is not None:
            if k_prev <= d_prev and k_now > d_now:
                crossover = "bullish_crossover"
            elif k_prev >= d_prev and k_now < d_now:
                crossover = "bearish_crossover"

    return {
        "k_window": k_window,
        "d_window": d_window,
        "latest": {
            "k": round(k_now, 2),
            "d": round(d_now, 2),
        },
        "k": round(k_now, 2),
        "d": round(d_now, 2),
        "condition": condition,
        "crossover": crossover,
        "k_series": pct_k,
        "d_series": pct_d,
    }


def momentum_group(close: pd.Series, params: dict | None = None) -> dict:
    """Every Group B indicator, keyed by name."""
    params = params or {}
    n = params.get("rsi_window", 14)
    z_window = params.get("z_score_window", 20)
    pct_window = params.get("percentile_window", 20)
    pct_windows = params.get("percentile_windows", (20, 60, 126))
    by_window = close_percentile_by_window(close, pct_windows)

    out = {
        f"rsi_{n}": rsi(close, n),
        f"z_score_{z_window}": z_score(close, z_window),
        f"close_percentile_{pct_window}": close_percentile(close, pct_window),
        "close_percentile_by_window": by_window,
        "close_percentiles": by_window,
        "return_streak": return_streak(close),
    }

    high = params.get("high")
    low = params.get("low")
    if isinstance(high, pd.Series) and isinstance(low, pd.Series):
        h_num = pd.to_numeric(high, errors="coerce")
        l_num = pd.to_numeric(low, errors="coerce")
        if h_num.notna().any() and l_num.notna().any():
            k_win = params.get("stochastic_k_window", 14)
            d_win = params.get("stochastic_d_window", 3)
            stoch_res = stochastic(high, low, close, k_window=k_win, d_window=d_win)
            out["stochastic"] = stoch_res
            out[f"stochastic_{k_win}_{d_win}"] = stoch_res

    return out


