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
    """Rolling mean of close over `n` rows, with direction."""
    marker = require(close, n, f"sma({n})")
    if marker:
        return marker
    series = pd.to_numeric(close, errors="coerce").rolling(n).mean()
    current = latest(series)
    slope_lookback = min(5, len(series) - n)
    direction = "unknown"
    if slope_lookback > 0 and current is not None:
        prior = finite(series.iloc[-1 - slope_lookback])
        if prior is not None and prior != 0:
            pct_change = (current - prior) / prior
            if pct_change > 0.001:
                direction = "rising"
            elif pct_change < -0.001:
                direction = "falling"
            else:
                direction = "flat"
    return {"window": n, "latest": current, "direction": direction, "series": series}


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


def returns_by_window(close: pd.Series, windows=(5, 20, 60, 120)) -> dict:
    """% return from N sessions ago to the latest close. Omits a window key
    entirely (does not return 0 or null) when there isn't enough history,
    per the addendum's contract."""
    out = {}
    n = len(close)
    for w in windows:
        key = f"{w}d"
        if n <= w:
            continue  # insufficient_data for this window, omit the key
        past = close.iloc[-(w + 1)]
        latest = close.iloc[-1]
        if past == 0:
            continue
        out[key] = round(float((latest - past) / past * 100), 2)
    return out


def detect_sma_crossover(close: pd.Series, fast: int, slow: int, dates: pd.Series = None) -> dict:
    """Most recent golden/death cross between two SMAs, with the date it
    happened. `dates` should be the same length as `close`, aligned index.
    If omitted, close.index itself must be a real DatetimeIndex -- this
    function refuses to guess a date from a plain integer position, since a
    wrong-but-plausible-looking date (e.g. silently defaulting to the Unix
    epoch) is worse than an explicit error."""
    if len(close) < slow + 2:
        return {"last_event": "none", "date": None}

    if dates is None and not isinstance(close.index, pd.DatetimeIndex):
        raise ValueError(
            "detect_sma_crossover: no `dates` argument was given and close.index "
            "is not a DatetimeIndex. Pass the real session dates explicitly rather "
            "than relying on a positional index, otherwise the returned date is "
            "meaningless."
        )

    sma_fast = close.rolling(fast).mean()
    sma_slow = close.rolling(slow).mean()
    diff = sma_fast - sma_slow
    diff = diff.dropna()

    if len(diff) < 2:
        return {"last_event": "none", "date": None}

    sign = (diff > 0).astype(int)
    change = sign.diff().dropna()  # dropna to exclude leading NaN from diff

    crossings = change[change != 0]
    if crossings.empty:
        return {"last_event": "none", "date": None}

    last_idx = crossings.index[-1]
    event = "golden_cross" if crossings.iloc[-1] == 1 else "death_cross"

    date_value = dates.loc[last_idx] if dates is not None else last_idx
    if isinstance(date_value, pd.Series):
        date_value = date_value.iloc[-1]
    date_str = pd.Timestamp(date_value).strftime("%Y-%m-%d")
    return {"last_event": event, "date": date_str}


def trend_group(close: pd.Series, params: dict | None = None) -> dict:
    """Every Group A indicator, keyed by name."""
    params = params or {}
    sma_windows = params.get("sma_windows", (20, 50, 100, 200))
    ema_windows = params.get("ema_windows", (12, 20, 26, 50, 200))
    out: dict[str, dict] = {}
    latest_c = latest(close)

    for n in sma_windows:
        res = sma(close, n)
        if isinstance(res, dict) and "latest" in res and res["latest"] is not None and latest_c is not None:
            ma_val = res["latest"]
            res["distance_pct"] = round((latest_c - ma_val) / ma_val * 100, 2) if ma_val > 0 else None
        out[f"sma_{n}"] = res

    for n in ema_windows:
        res = ema(close, n)
        if isinstance(res, dict) and "latest" in res and res["latest"] is not None and latest_c is not None:
            ma_val = res["latest"]
            res["distance_pct"] = round((latest_c - ma_val) / ma_val * 100, 2) if ma_val > 0 else None
        out[f"ema_{n}"] = res

    out["macd"] = macd(
        close,
        params.get("macd_fast", 12),
        params.get("macd_slow", 26),
        params.get("macd_signal", 9),
    )
    return_windows = params.get("return_windows", (5, 20, 60, 120, 250))
    out["returns"] = returns_by_window(close, return_windows)
    out["returns_by_window"] = out["returns"]

    dates = params.get("dates")
    if dates is None and not isinstance(close.index, pd.DatetimeIndex):
        if len(close) > 0 and isinstance(close.index[0], str):
            try:
                pd.Timestamp(close.index[0])
                dates = pd.Series(close.index, index=close.index)
            except Exception:
                dates = None

    crossover_pairs = params.get("crossover_pairs", ((20, 50), (50, 200)))
    for fast, slow in crossover_pairs:
        key = f"sma_crossover_{fast}_{slow}"
        try:
            out[key] = detect_sma_crossover(close, fast, slow, dates=dates)
        except ValueError:
            out[key] = {"last_event": "none", "date": None}
    return out

