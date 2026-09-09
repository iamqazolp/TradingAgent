"""Multi-horizon analysis: short / mid / long-term investment views.

This module is an *interpretation layer* that sits on top of the indicator
engine.  It receives the output dicts from :func:`engine.compute` (either
raw or serialized — only scalar fields are accessed, never Series) and
classifies the current technical state for three investment horizons.

Horizons
--------
- **Short-term** (Swing): 1–4 weeks, driven by daily SMA5/20, EMA12, RSI,
  MACD, Bollinger, recent volume flow.
- **Mid-term** (Position): 1–3 months, driven by daily SMA20/50/200, RSI,
  MACD, foreign flow, with optional weekly confirmation.
- **Long-term** (Trend): > 3 months, primarily weekly indicators —
  weekly SMA20/50, weekly RSI/MACD, daily SMA200.
"""

from __future__ import annotations

from typing import Any

from indicators import is_insufficient


# ------------------------------------------------------------------ extractors


def _sma_latest(groups: dict, key: str) -> float | None:
    """Extract the latest SMA value from a groups dict."""
    trend = groups.get("trend", {})
    entry = trend.get(key, {})
    if is_insufficient(entry):
        return None
    return entry.get("latest")


def _sma_direction(groups: dict, key: str) -> str | None:
    trend = groups.get("trend", {})
    entry = trend.get(key, {})
    if is_insufficient(entry):
        return None
    return entry.get("direction")


def _ema_latest(groups: dict, key: str) -> float | None:
    trend = groups.get("trend", {})
    entry = trend.get(key, {})
    if is_insufficient(entry):
        return None
    return entry.get("latest")


def _rsi_latest(groups: dict) -> float | None:
    momentum = groups.get("momentum", {})
    rsi = momentum.get("rsi", {})
    if is_insufficient(rsi):
        return None
    return rsi.get("latest")


def _macd_snapshot(groups: dict) -> dict | None:
    """Return MACD scalar snapshot or None."""
    trend = groups.get("trend", {})
    macd = trend.get("macd", {})
    if is_insufficient(macd):
        return None
    lat = macd.get("latest", {})
    return {
        "macd": lat.get("macd"),
        "signal": lat.get("signal"),
        "histogram": lat.get("histogram"),
        "crossover": macd.get("crossover"),
    }


def _bollinger_snapshot(groups: dict) -> dict | None:
    vol = groups.get("volatility", {})
    bb = vol.get("bollinger", {})
    if is_insufficient(bb):
        return None
    lat = bb.get("latest", {})
    return {
        "upper": lat.get("upper"),
        "middle": lat.get("middle"),
        "lower": lat.get("lower"),
        "position": bb.get("position"),
        "squeeze": bb.get("squeeze"),
        "bandwidth": bb.get("bandwidth"),
    }


def _volatility_latest(groups: dict) -> float | None:
    vol = groups.get("volatility", {})
    cc = vol.get("close_to_close_vol", {})
    if is_insufficient(cc):
        return None
    return cc.get("latest")


def _volume_bias(groups: dict) -> str | None:
    vf = groups.get("volume_flow", {})
    imb = vf.get("buy_sell_volume_imbalance", {})
    if is_insufficient(imb):
        return None
    return imb.get("bias")


def _foreign_snapshot(groups: dict) -> dict:
    """Extract foreign flow scalars."""
    ff = groups.get("foreign_flow", {})
    fnv = ff.get("foreign_net_value", {})
    if is_insufficient(fnv):
        return {"stance": None, "cumulative": None}
    return {
        "stance": fnv.get("stance"),
        "cumulative": fnv.get("cumulative"),
    }


# ------------------------------------------------------------------ classifiers


def classify_trend(
    close: float | None,
    sma_short: float | None,
    sma_mid: float | None,
    sma_long: float | None = None,
) -> str:
    """Classify trend strength from price vs. SMA alignment."""
    if close is None or sma_short is None or sma_mid is None:
        return "insufficient_data"

    if sma_long is not None:
        if close > sma_short > sma_mid > sma_long:
            return "strong_bullish"
        if close < sma_short < sma_mid < sma_long:
            return "strong_bearish"
        if close > sma_long:
            return "moderate_bullish" if close > sma_short else "weak_bullish"
        return "moderate_bearish" if close < sma_short else "weak_bearish"

    # Two-SMA variant
    if close > sma_short > sma_mid:
        return "bullish"
    if close < sma_short < sma_mid:
        return "bearish"
    if close > sma_mid:
        return "weak_bullish"
    return "weak_bearish"


def classify_momentum(rsi: float | None, macd_hist: float | None = None) -> str:
    """Classify momentum from RSI and optionally MACD histogram."""
    if rsi is None:
        return "insufficient_data"
    if rsi > 70:
        base = "overbought"
    elif rsi < 30:
        base = "oversold"
    elif rsi > 60:
        base = "bullish"
    elif rsi < 40:
        base = "bearish"
    else:
        base = "neutral"

    if macd_hist is not None and base not in ("overbought", "oversold"):
        if macd_hist > 0:
            return f"{base}_macd_positive"
        return f"{base}_macd_negative"
    return base


def compute_key_levels(
    close: float,
    sma_values: dict[str, float | None],
    bollinger: dict | None,
) -> dict:
    """Identify nearest support and resistance levels."""
    supports: list[dict] = []
    resistances: list[dict] = []

    for name, val in sma_values.items():
        if val is None or close == 0:
            continue
        dist = round((val - close) / close * 100, 2)
        entry = {"level": round(val, 0), "basis": name, "distance_pct": abs(dist)}
        if val < close:
            supports.append(entry)
        elif val > close:
            resistances.append(entry)

    if bollinger:
        lower = bollinger.get("lower")
        upper = bollinger.get("upper")
        if lower is not None and lower < close and close != 0:
            supports.append({
                "level": round(lower, 0),
                "basis": "bollinger_lower",
                "distance_pct": round(abs(close - lower) / close * 100, 2),
            })
        if upper is not None and upper > close and close != 0:
            resistances.append({
                "level": round(upper, 0),
                "basis": "bollinger_upper",
                "distance_pct": round(abs(upper - close) / close * 100, 2),
            })

    supports.sort(key=lambda x: x["distance_pct"])
    resistances.sort(key=lambda x: x["distance_pct"])
    return {"support": supports[:4], "resistance": resistances[:4]}


def signal_strength(
    trend_bias: str,
    momentum: str,
    vol_bias: str | None = None,
    foreign_stance: str | None = None,
) -> str:
    """Aggregate signal strength from components."""
    _SCORES = {
        "strong_bullish": 2, "moderate_bullish": 1, "bullish": 1,
        "weak_bullish": 0.5, "above_long_term_ma": 0.5,
        "neutral": 0, "insufficient_data": 0,
        "weak_bearish": -0.5, "below_long_term_ma": -0.5,
        "moderate_bearish": -1, "bearish": -1, "strong_bearish": -2,
    }
    total = _SCORES.get(trend_bias, 0)
    # Momentum contribution (capped)
    mom_base = momentum.split("_macd_")[0] if "_macd_" in momentum else momentum
    total += _SCORES.get(mom_base, 0) * 0.6
    if "_macd_positive" in momentum:
        total += 0.3
    elif "_macd_negative" in momentum:
        total -= 0.3

    if vol_bias == "buy_side":
        total += 0.4
    elif vol_bias == "sell_side":
        total -= 0.4

    if foreign_stance == "net_buying":
        total += 0.4
    elif foreign_stance == "net_selling":
        total -= 0.4

    if total >= 2.0:
        return "strong_bullish"
    if total >= 1.0:
        return "moderate_bullish"
    if total >= 0.3:
        return "lean_bullish"
    if total > -0.3:
        return "neutral"
    if total > -1.0:
        return "lean_bearish"
    if total > -2.0:
        return "moderate_bearish"
    return "strong_bearish"


# ------------------------------------------------------------------ per-horizon


def _short_term(daily: dict, close: float) -> dict:
    """Short-term (swing) analysis: 1–4 weeks, 5–20 sessions."""
    groups = daily.get("groups", {})

    sma5 = _sma_latest(groups, "sma_5")
    sma20 = _sma_latest(groups, "sma_20")
    sma50 = _sma_latest(groups, "sma_50")
    ema12 = _ema_latest(groups, "ema_12")
    rsi = _rsi_latest(groups)
    macd = _macd_snapshot(groups)
    bb = _bollinger_snapshot(groups)
    vol = _volatility_latest(groups)
    v_bias = _volume_bias(groups)
    foreign = _foreign_snapshot(groups)

    # Use EMA12 or SMA20 as short-term reference
    short_ref = ema12 if ema12 is not None else sma20
    trend = classify_trend(close, short_ref, sma50 if sma50 else sma20)
    macd_hist = macd.get("histogram") if macd else None
    mom = classify_momentum(rsi, macd_hist)

    sma_vals = {"SMA20": sma20, "SMA50": sma50}
    if sma5 is not None:
        sma_vals["SMA5"] = sma5
    if ema12 is not None:
        sma_vals["EMA12"] = ema12
    levels = compute_key_levels(close, sma_vals, bb)
    strength = signal_strength(trend, mom, v_bias, foreign["stance"])

    returns = daily.get("returns") or {}
    return {
        "horizon": "short_term",
        "description": "1–4 tuần (5–20 phiên)",
        "trend_bias": trend,
        "momentum": mom,
        "rsi": rsi,
        "macd": macd,
        "bollinger": bb,
        "volatility": vol,
        "volume_bias": v_bias,
        "foreign_stance": foreign["stance"],
        "key_levels": levels,
        "signal_strength": strength,
        "sma_values": {k: v for k, v in sma_vals.items() if v is not None},
        "returns_5d": returns.get("5d"),
        "returns_20d": returns.get("20d"),
    }


def _mid_term(daily: dict, weekly: dict | None, close: float) -> dict:
    """Mid-term (position) analysis: 1–3 months, 20–60 sessions."""
    groups = daily.get("groups", {})

    sma20 = _sma_latest(groups, "sma_20")
    sma50 = _sma_latest(groups, "sma_50")
    sma200 = _sma_latest(groups, "sma_200")
    sma20_dir = _sma_direction(groups, "sma_20")
    sma50_dir = _sma_direction(groups, "sma_50")
    rsi = _rsi_latest(groups)
    macd = _macd_snapshot(groups)
    bb = _bollinger_snapshot(groups)
    vol = _volatility_latest(groups)
    v_bias = _volume_bias(groups)
    foreign = _foreign_snapshot(groups)

    trend = classify_trend(close, sma20, sma50, sma200)
    macd_hist = macd.get("histogram") if macd else None
    mom = classify_momentum(rsi, macd_hist)

    # Weekly confirmation (when available)
    weekly_confirm = None
    if weekly and weekly.get("groups"):
        wg = weekly["groups"]
        w_rsi = _rsi_latest(wg)
        w_macd = _macd_snapshot(wg)
        w_sma20 = _sma_latest(wg, "sma_20")
        weekly_confirm = {
            "weekly_rsi": w_rsi,
            "weekly_macd": w_macd,
            "weekly_sma20": w_sma20,
            "weekly_trend_alignment": weekly.get("trend_alignment"),
        }

    sma_vals = {"SMA20": sma20, "SMA50": sma50, "SMA200": sma200}
    levels = compute_key_levels(close, sma_vals, bb)
    strength = signal_strength(trend, mom, v_bias, foreign["stance"])

    # SMA crossover info
    crossovers = {}
    trend_data = groups.get("trend", {})
    for key in ("sma_crossover_20_50", "sma_crossover_50_200"):
        xo = trend_data.get(key)
        if xo and not is_insufficient(xo):
            crossovers[key] = xo

    returns = daily.get("returns") or {}
    return {
        "horizon": "mid_term",
        "description": "1–3 tháng (20–60 phiên)",
        "trend_bias": trend,
        "momentum": mom,
        "rsi": rsi,
        "macd": macd,
        "bollinger": bb,
        "volatility": vol,
        "volume_bias": v_bias,
        "foreign_stance": foreign["stance"],
        "foreign_cumulative": foreign["cumulative"],
        "key_levels": levels,
        "signal_strength": strength,
        "sma_values": {k: v for k, v in sma_vals.items() if v is not None},
        "sma_directions": {"SMA20": sma20_dir, "SMA50": sma50_dir},
        "crossovers": crossovers,
        "weekly_confirmation": weekly_confirm,
        "returns_20d": returns.get("20d"),
        "returns_60d": returns.get("60d"),
    }


def _long_term(daily: dict, weekly: dict | None, close: float) -> dict:
    """Long-term (trend) analysis: > 3 months, weekly timeframe."""
    d_groups = daily.get("groups", {})

    sma200 = _sma_latest(d_groups, "sma_200")
    sma200_dir = _sma_direction(d_groups, "sma_200")
    d_foreign = _foreign_snapshot(d_groups)

    # Weekly indicators are primary for long-term view
    w_sma20 = w_sma50 = w_rsi = w_macd = w_trend = None
    w_vol_bias = w_foreign_stance = w_foreign_cum = None
    w_bb = None

    if weekly and weekly.get("groups"):
        wg = weekly["groups"]
        w_sma20 = _sma_latest(wg, "sma_20")
        w_sma50 = _sma_latest(wg, "sma_50")
        w_rsi = _rsi_latest(wg)
        w_macd = _macd_snapshot(wg)
        w_bb = _bollinger_snapshot(wg)
        w_trend = weekly.get("trend_alignment")
        w_vol_bias = _volume_bias(wg)
        w_foreign = _foreign_snapshot(wg)
        w_foreign_stance = w_foreign.get("stance")
        w_foreign_cum = w_foreign.get("cumulative")

    # Trend classification prioritises weekly SMAs when available
    if w_sma20 is not None and w_sma50 is not None:
        trend = classify_trend(close, w_sma20, w_sma50, sma200)
    elif sma200 is not None:
        trend = "above_long_term_ma" if close > sma200 else "below_long_term_ma"
    else:
        trend = "insufficient_data"

    macd_hist = w_macd.get("histogram") if w_macd else None
    mom = classify_momentum(w_rsi, macd_hist)

    sma_vals: dict[str, float | None] = {"SMA200_daily": sma200}
    if w_sma20:
        sma_vals["SMA20_weekly"] = w_sma20
    if w_sma50:
        sma_vals["SMA50_weekly"] = w_sma50
    levels = compute_key_levels(close, sma_vals, w_bb)

    strength = signal_strength(
        trend, mom,
        w_vol_bias,
        w_foreign_stance or d_foreign["stance"],
    )

    returns = daily.get("returns") or {}
    return {
        "horizon": "long_term",
        "description": "> 3 tháng (khung tuần, 26–52 tuần)",
        "trend_bias": trend,
        "daily_sma200": sma200,
        "daily_sma200_direction": sma200_dir,
        "daily_trend_alignment": daily.get("trend_alignment"),
        "weekly_sma20": w_sma20,
        "weekly_sma50": w_sma50,
        "weekly_rsi": w_rsi,
        "weekly_macd": w_macd,
        "weekly_bollinger": w_bb,
        "weekly_trend_alignment": w_trend,
        "momentum": mom,
        "foreign_stance": w_foreign_stance or d_foreign["stance"],
        "foreign_cumulative": d_foreign["cumulative"],
        "key_levels": levels,
        "signal_strength": strength,
        "sma_values": {k: v for k, v in sma_vals.items() if v is not None},
        "returns_60d": returns.get("60d"),
        "returns_120d": returns.get("120d"),
    }


# ------------------------------------------------------------------ alignment


def _horizon_alignment(short: dict, mid: dict, long: dict) -> str:
    """Assess signal alignment across the three horizons."""
    strengths = [
        short.get("signal_strength", "neutral"),
        mid.get("signal_strength", "neutral"),
        long.get("signal_strength", "neutral"),
    ]

    def is_bull(s: str) -> bool:
        return "bullish" in s

    def is_bear(s: str) -> bool:
        return "bearish" in s

    bulls = sum(is_bull(s) for s in strengths)
    bears = sum(is_bear(s) for s in strengths)

    if bulls == 3:
        return "all_bullish"
    if bears == 3:
        return "all_bearish"

    # Two out of three agree
    if bulls == 2:
        if is_bear(strengths[0]):
            return "short_bearish_mid_long_bullish"
        if is_bear(strengths[2]):
            return "short_mid_bullish_long_divergent"
        return "mostly_bullish"
    if bears == 2:
        if is_bull(strengths[0]):
            return "short_bullish_mid_long_bearish"
        if is_bull(strengths[2]):
            return "short_mid_bearish_long_bullish"
        return "mostly_bearish"

    # Specific divergence patterns
    if is_bull(strengths[2]) and is_bear(strengths[0]):
        return "long_bullish_short_correction"
    if is_bear(strengths[2]) and is_bull(strengths[0]):
        return "long_bearish_short_bounce"

    return "mixed_signals"


# ------------------------------------------------------------------ public API


def horizon_analysis(
    daily_compute: dict,
    weekly_compute: dict | None,
    latest_close: float,
) -> dict:
    """Produce a three-horizon investment analysis.

    Parameters
    ----------
    daily_compute
        Output of ``engine.compute()`` on daily data.
    weekly_compute
        Output of ``engine.compute()`` on weekly data, or ``None`` if
        there are not enough weekly bars.
    latest_close
        Most recent daily closing price.

    Returns
    -------
    dict
        ``short_term``, ``mid_term``, ``long_term`` analyses plus
        ``horizon_alignment`` summary.
    """
    if latest_close is None:
        return {
            "error": "no_close_price",
            "message": "Cannot perform horizon analysis without a close price.",
        }

    short = _short_term(daily_compute, latest_close)
    mid = _mid_term(daily_compute, weekly_compute, latest_close)
    long = _long_term(daily_compute, weekly_compute, latest_close)
    alignment = _horizon_alignment(short, mid, long)

    return {
        "short_term": short,
        "mid_term": mid,
        "long_term": long,
        "horizon_alignment": alignment,
    }
