"""Group A: trend. SMA, EMA, MACD, and ADX/DMI.

EMA seeding follows the TA-Lib convention: the first EMA value is the simple
average of the first `n` closes, and the recursion runs from there.
ADX (Average Directional Index) is computed via Wilder smoothing on +DM, -DM, and TR.
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


def adx(data: pd.DataFrame | pd.Series, n: int = 14) -> dict:
    """Wilder's Average Directional Movement Index (ADX) over `n` periods.

    Measures trend strength independently of direction.
    Requires at least 2*n rows for double Wilder smoothing.
    """
    required = 2 * n
    if isinstance(data, pd.Series):
        return insufficient("adx requires high, low, and close columns (DataFrame)", required)

    marker = require(data.index.to_series(), required, f"adx({n})")
    if marker:
        return marker

    high = pd.to_numeric(data["high"], errors="coerce").astype("float64").to_numpy()
    low = pd.to_numeric(data["low"], errors="coerce").astype("float64").to_numpy()
    close = pd.to_numeric(data["close"], errors="coerce").astype("float64").to_numpy()
    count = len(close)

    plus_dm = np.zeros(count, dtype="float64")
    minus_dm = np.zeros(count, dtype="float64")
    tr = np.zeros(count, dtype="float64")

    for i in range(1, count):
        up_move = high[i] - high[i - 1]
        down_move = low[i - 1] - low[i]
        if up_move > down_move and up_move > 0:
            plus_dm[i] = up_move
        if down_move > up_move and down_move > 0:
            minus_dm[i] = down_move
        tr[i] = max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1]))

    # Wilder smoothing on TR, +DM, -DM
    smooth_tr = np.full(count, np.nan, dtype="float64")
    smooth_plus_dm = np.full(count, np.nan, dtype="float64")
    smooth_minus_dm = np.full(count, np.nan, dtype="float64")
    plus_di = np.full(count, np.nan, dtype="float64")
    minus_di = np.full(count, np.nan, dtype="float64")
    dx = np.full(count, np.nan, dtype="float64")

    smooth_tr[n] = float(np.mean(tr[1 : n + 1]))
    smooth_plus_dm[n] = float(np.mean(plus_dm[1 : n + 1]))
    smooth_minus_dm[n] = float(np.mean(minus_dm[1 : n + 1]))

    for i in range(n, count):
        if i > n:
            smooth_tr[i] = (smooth_tr[i - 1] * (n - 1) + tr[i]) / n
            smooth_plus_dm[i] = (smooth_plus_dm[i - 1] * (n - 1) + plus_dm[i]) / n
            smooth_minus_dm[i] = (smooth_minus_dm[i - 1] * (n - 1) + minus_dm[i]) / n

        str_val = smooth_tr[i]
        if str_val > 0:
            p_di = 100.0 * (smooth_plus_dm[i] / str_val)
            m_di = 100.0 * (smooth_minus_dm[i] / str_val)
        else:
            p_di = 0.0
            m_di = 0.0
        plus_di[i] = p_di
        minus_di[i] = m_di
        di_sum = p_di + m_di
        dx[i] = 100.0 * (abs(p_di - m_di) / di_sum) if di_sum > 0 else 0.0

    # Wilder smoothing on DX -> ADX
    adx_arr = np.full(count, np.nan, dtype="float64")
    start_adx = 2 * n - 1
    if start_adx < count:
        adx_arr[start_adx] = float(np.mean(dx[n : start_adx + 1]))
        prev_adx = adx_arr[start_adx]
        for i in range(start_adx + 1, count):
            prev_adx = (prev_adx * (n - 1) + dx[i]) / n
            adx_arr[i] = prev_adx

    adx_series = pd.Series(adx_arr, index=data.index, name=f"adx{n}")
    plus_di_series = pd.Series(plus_di, index=data.index, name=f"plus_di{n}")
    minus_di_series = pd.Series(minus_di, index=data.index, name=f"minus_di{n}")

    adx_val = latest(adx_series)
    plus_val = latest(plus_di_series)
    minus_val = latest(minus_di_series)

    if adx_val is None:
        return insufficient(f"adx({n}) has no value on the latest row", required, count)

    return {
        "window": n,
        "latest": {
            "adx": adx_val,
            "plus_di": plus_val,
            "minus_di": minus_val,
        },
        "trend_strength": _adx_strength(adx_val),
        "directional_bias": _adx_bias(plus_val, minus_val),
        "adx_series": adx_series,
        "plus_di_series": plus_di_series,
        "minus_di_series": minus_di_series,
    }


def _adx_strength(val: float | None) -> str:
    if val is None:
        return "unknown"
    if val >= 50:
        return "very_strong_trend"
    if val >= 25:
        return "trending"
    if val >= 20:
        return "emerging_trend"
    return "weak_or_ranging"


def _adx_bias(plus: float | None, minus: float | None) -> str:
    if plus is None or minus is None:
        return "unknown"
    if plus > minus + 1.0:
        return "bullish"
    if minus > plus + 1.0:
        return "bearish"
    return "neutral"


def _crossover(previous: float | None, current: float | None) -> str:
    """Describe the histogram sign change between the last two rows."""
    if previous is None or current is None:
        return "unknown"
    if previous <= 0 < current:
        return "bullish_cross"
    if previous >= 0 > current:
        return "bearish_cross"
    return "none"


def ichimoku(
    data: pd.DataFrame,
    tenkan_n: int = 9,
    kijun_n: int = 26,
    senkou_b_n: int = 52,
    displacement: int = 26,
) -> dict:
    """Ichimoku Kinko Hyo (Equilibrium Chart).

    Components (classic 9, 26, 52 parameters):
      - Tenkan-sen (Conversion Line): (9-period High + 9-period Low) / 2
      - Kijun-sen (Base Line): (26-period High + 26-period Low) / 2
      - Senkou Span A (Leading Span A): (Tenkan-sen + Kijun-sen) / 2
      - Senkou Span B (Leading Span B): (52-period High + 52-period Low) / 2
      - Chikou Span (Lagging Span): Close
    """
    required = senkou_b_n
    marker = require(data.index.to_series(), required, f"ichimoku({tenkan_n},{kijun_n},{senkou_b_n})")
    if marker:
        return marker

    high = pd.to_numeric(data["high"], errors="coerce").astype("float64")
    low = pd.to_numeric(data["low"], errors="coerce").astype("float64")
    close = pd.to_numeric(data["close"], errors="coerce").astype("float64")

    tenkan = (high.rolling(tenkan_n).max() + low.rolling(tenkan_n).min()) / 2.0
    kijun = (high.rolling(kijun_n).max() + low.rolling(kijun_n).min()) / 2.0
    senkou_a = (tenkan + kijun) / 2.0
    senkou_b = (high.rolling(senkou_b_n).max() + low.rolling(senkou_b_n).min()) / 2.0

    tenkan_now = latest(tenkan)
    kijun_now = latest(kijun)
    senkou_a_now = latest(senkou_a)
    senkou_b_now = latest(senkou_b)
    close_now = latest(close)

    if tenkan_now is None or kijun_now is None or senkou_a_now is None or senkou_b_now is None:
        return insufficient(
            f"ichimoku({tenkan_n},{kijun_n},{senkou_b_n}) has no value on the latest row",
            required,
            int(len(data)),
        )

    t_prev = float(tenkan.iloc[-2]) if len(tenkan) > 1 else None
    k_prev = float(kijun.iloc[-2]) if len(kijun) > 1 else None
    if t_prev is not None and k_prev is not None:
        if t_prev <= k_prev and tenkan_now > kijun_now:
            tk_cross = "bullish_cross"
        elif t_prev >= k_prev and tenkan_now < kijun_now:
            tk_cross = "bearish_cross"
        elif tenkan_now > kijun_now:
            tk_cross = "bullish_alignment"
        elif tenkan_now < kijun_now:
            tk_cross = "bearish_alignment"
        else:
            tk_cross = "neutral"
    else:
        tk_cross = (
            "bullish_alignment"
            if tenkan_now > kijun_now
            else "bearish_alignment"
            if tenkan_now < kijun_now
            else "neutral"
        )

    cloud_top = max(senkou_a_now, senkou_b_now)
    cloud_bottom = min(senkou_a_now, senkou_b_now)
    cloud_thickness = abs(senkou_a_now - senkou_b_now)
    cloud_thickness_pct = (cloud_thickness / close_now * 100.0) if close_now and close_now > 0 else 0.0

    if senkou_a_now > senkou_b_now:
        kumo_sentiment = "bullish"
    elif senkou_a_now < senkou_b_now:
        kumo_sentiment = "bearish"
    else:
        kumo_sentiment = "neutral"

    if close_now is not None and close_now > cloud_top:
        price_vs_cloud = "above_cloud"
    elif close_now is not None and close_now < cloud_bottom:
        price_vs_cloud = "below_cloud"
    else:
        price_vs_cloud = "inside_cloud"

    return {
        "tenkan_window": tenkan_n,
        "kijun_window": kijun_n,
        "senkou_b_window": senkou_b_n,
        "displacement": displacement,
        "latest": {
            "tenkan_sen": tenkan_now,
            "kijun_sen": kijun_now,
            "senkou_span_a": senkou_a_now,
            "senkou_span_b": senkou_b_now,
            "chikou_span": close_now,
        },
        "tk_cross": tk_cross,
        "kumo_sentiment": kumo_sentiment,
        "price_vs_cloud": price_vs_cloud,
        "cloud_thickness": cloud_thickness,
        "cloud_thickness_pct": cloud_thickness_pct,
        "tenkan_series": tenkan.rename("tenkan_sen"),
        "kijun_series": kijun.rename("kijun_sen"),
        "senkou_a_series": senkou_a.rename("senkou_span_a"),
        "senkou_b_series": senkou_b.rename("senkou_span_b"),
        "chikou_series": close.rename("chikou_span"),
        "chikou_shifted_series": close.shift(-displacement).rename("chikou_span_shifted"),
    }


def trend_group(data: pd.DataFrame | pd.Series, params: dict | None = None) -> dict:
    """Every Group A indicator, keyed by name."""
    params = params or {}
    close = data["close"] if isinstance(data, pd.DataFrame) else data
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
    if isinstance(data, pd.DataFrame) and "high" in data.columns and "low" in data.columns:
        adx_n = params.get("adx_window", 14)
        out[f"adx_{adx_n}"] = adx(data, adx_n)
        tenkan_n = params.get("ichimoku_tenkan", 9)
        kijun_n = params.get("ichimoku_kijun", 26)
        senkou_b_n = params.get("ichimoku_senkou_b", 52)
        displacement = params.get("ichimoku_displacement", 26)
        out["ichimoku"] = ichimoku(data, tenkan_n, kijun_n, senkou_b_n, displacement)
    return out
