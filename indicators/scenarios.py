"""Scenario generation from technical analysis state.

Produces 2–3 actionable scenarios (bullish / neutral / bearish) based on
the current horizon analysis and computed indicator values.  Each scenario
includes concrete trigger conditions, target price zones, and invalidation
levels — all derived from actual SMA / Bollinger / volatility numbers.

**Important**: scenarios are conditional ("if… then…") projections, **not**
price predictions.  The probability field is a qualitative label reflecting
how many current signals support the scenario, not a statistical estimate.
"""

from __future__ import annotations

from typing import Any


def generate_scenarios(
    daily_compute: dict,
    weekly_compute: dict | None,
    horizons: dict,
    latest_close: float,
) -> dict:
    """Generate 2–3 conditional scenarios from the current technical state.

    Returns
    -------
    dict
        ``scenarios`` (list of scenario dicts), ``dominant_scenario`` (name
        of the scenario with the most supporting signals).
    """
    if latest_close is None or latest_close <= 0:
        return {"scenarios": [], "dominant_scenario": None,
                "error": "invalid_close_price"}

    short = horizons.get("short_term", {})
    mid = horizons.get("mid_term", {})
    long = horizons.get("long_term", {})
    alignment = horizons.get("horizon_alignment", "mixed_signals")

    # Collect all available SMA / Bollinger levels for target/stop computation
    all_sma = _merge_sma_values(short, mid, long)
    bb = short.get("bollinger") or mid.get("bollinger")
    vol = short.get("volatility") or mid.get("volatility")

    scenarios = [
        _bullish_scenario(latest_close, all_sma, bb, vol, short, mid, long, alignment),
        _neutral_scenario(latest_close, all_sma, bb, vol, short, mid, long, alignment),
        _bearish_scenario(latest_close, all_sma, bb, vol, short, mid, long, alignment),
    ]

    # Determine dominant scenario
    dominant = _dominant(scenarios, alignment)

    return {
        "scenarios": scenarios,
        "dominant_scenario": dominant,
        "note": (
            "Scenarios are conditional projections (if… then…), "
            "not price predictions.  Probability labels reflect "
            "how many current signals support each scenario."
        ),
    }


# ------------------------------------------------------------------ helpers


def _merge_sma_values(short: dict, mid: dict, long: dict) -> dict[str, float]:
    """Collect all SMA values from horizon analyses into one flat dict."""
    merged: dict[str, float] = {}
    for h in (short, mid, long):
        for k, v in h.get("sma_values", {}).items():
            if v is not None:
                merged[k] = v
    return merged


def _nearest_above(close: float, levels: dict[str, float]) -> tuple[str, float] | None:
    """Closest SMA/level above close."""
    above = [(k, v) for k, v in levels.items() if v > close]
    return min(above, key=lambda x: x[1]) if above else None


def _nearest_below(close: float, levels: dict[str, float]) -> tuple[str, float] | None:
    """Closest SMA/level below close."""
    below = [(k, v) for k, v in levels.items() if v < close]
    return max(below, key=lambda x: x[1]) if below else None


def _second_above(close: float, levels: dict[str, float]) -> tuple[str, float] | None:
    """Second closest level above close."""
    above = sorted([(k, v) for k, v in levels.items() if v > close], key=lambda x: x[1])
    return above[1] if len(above) > 1 else None


def _second_below(close: float, levels: dict[str, float]) -> tuple[str, float] | None:
    """Second closest level below close."""
    below = sorted([(k, v) for k, v in levels.items() if v < close], key=lambda x: x[1], reverse=True)
    return below[1] if len(below) > 1 else None


def _vol_stop(close: float, vol_pct: float | None, multiplier: float = 2.0) -> float | None:
    """Stop-loss level based on close-to-close volatility."""
    if vol_pct is None or vol_pct <= 0:
        return None
    return round(close * (1 - vol_pct / 100 * multiplier), 0)


def _count_bullish(short: dict, mid: dict, long: dict) -> int:
    """Count how many horizons lean bullish."""
    count = 0
    for h in (short, mid, long):
        s = h.get("signal_strength", "neutral")
        if "bullish" in s:
            count += 1
    return count


def _count_bearish(short: dict, mid: dict, long: dict) -> int:
    count = 0
    for h in (short, mid, long):
        s = h.get("signal_strength", "neutral")
        if "bearish" in s:
            count += 1
    return count


def _probability(supporting: int) -> str:
    if supporting >= 3:
        return "high"
    if supporting >= 2:
        return "medium"
    return "low"


# ------------------------------------------------------------------ scenario builders


def _bullish_scenario(
    close: float, sma: dict, bb: dict | None, vol: float | None,
    short: dict, mid: dict, long: dict, alignment: str,
) -> dict:
    """Bullish (positive) scenario."""
    bulls = _count_bullish(short, mid, long)

    # Target zone: next resistance → second resistance
    r1 = _nearest_above(close, sma)
    r2 = _second_above(close, sma)
    bb_upper = bb.get("upper") if bb else None

    target_from = r1[1] if r1 else (bb_upper if bb_upper else round(close * 1.05, 0))
    target_to = r2[1] if r2 else (round(close * 1.10, 0))
    if target_to <= target_from:
        target_to = round(target_from * 1.05, 0)

    # Trigger: breaking nearest resistance with volume
    trigger_level = r1[1] if r1 else round(close * 1.03, 0)
    trigger_name = r1[0] if r1 else "kháng cự gần nhất"

    # Invalidation: losing nearest support
    s1 = _nearest_below(close, sma)
    inv_level = s1[1] if s1 else _vol_stop(close, vol) or round(close * 0.95, 0)
    inv_name = s1[0] if s1 else "hỗ trợ volatility"

    # Conditions
    conditions = []
    rsi = short.get("rsi")
    if rsi and rsi < 70:
        conditions.append(f"RSI ({rsi:.0f}) chưa quá mua, còn dư địa tăng")
    macd = short.get("macd")
    if macd and macd.get("histogram") and macd["histogram"] > 0:
        conditions.append("MACD histogram dương, xung lực tăng")
    if "bullish" in str(mid.get("trend_bias", "")):
        conditions.append("Xu hướng trung hạn tích cực")
    if long.get("foreign_stance") == "net_buying":
        conditions.append("Khối ngoại mua ròng hỗ trợ dài hạn")
    if not conditions:
        conditions.append("Giá duy trì trên đường SMA ngắn hạn")

    return {
        "name": "Kịch bản tích cực",
        "bias": "bullish",
        "probability": _probability(bulls),
        "supporting_signals": bulls,
        "conditions": "; ".join(conditions),
        "trigger": f"Vượt {trigger_name} ({trigger_level:,.0f} VND) với volume > 120% bình quân 20 phiên",
        "target_zone": {
            "from": round(target_from, 0),
            "to": round(target_to, 0),
            "upside_pct": round((target_to - close) / close * 100, 1),
        },
        "invalidation": {
            "level": round(inv_level, 0),
            "basis": inv_name,
            "description": f"Giá đóng cửa dưới {inv_name} ({inv_level:,.0f} VND)",
        },
    }


def _neutral_scenario(
    close: float, sma: dict, bb: dict | None, vol: float | None,
    short: dict, mid: dict, long: dict, alignment: str,
) -> dict:
    """Neutral (consolidation / range-bound) scenario."""
    neutrals = 3 - _count_bullish(short, mid, long) - _count_bearish(short, mid, long)
    # Neutral scenario is dominant when signals are mixed
    supporting = max(1, neutrals + (1 if "mixed" in alignment else 0))

    # Range: nearest support → nearest resistance
    s1 = _nearest_below(close, sma)
    r1 = _nearest_above(close, sma)
    bb_lower = bb.get("lower") if bb else None
    bb_upper = bb.get("upper") if bb else None

    range_low = s1[1] if s1 else (bb_lower if bb_lower else round(close * 0.97, 0))
    range_high = r1[1] if r1 else (bb_upper if bb_upper else round(close * 1.03, 0))

    conditions = []
    if bb and bb.get("squeeze"):
        conditions.append("Bollinger Bands co thắt (squeeze) — tích lũy năng lượng")
    if "mixed" in alignment or "divergent" in alignment:
        conditions.append("Tín hiệu đa khung phân kỳ, thiếu đồng thuận hướng rõ ràng")
    rsi = short.get("rsi")
    if rsi and 40 <= rsi <= 60:
        conditions.append(f"RSI ({rsi:.0f}) vùng trung tính, không hỗ trợ hướng nào")
    if not conditions:
        conditions.append("Giá dao động giữa hỗ trợ và kháng cự gần nhất")

    return {
        "name": "Kịch bản tích lũy / đi ngang",
        "bias": "neutral",
        "probability": _probability(supporting),
        "supporting_signals": supporting,
        "conditions": "; ".join(conditions),
        "trigger": "Giá tiếp tục dao động trong biên độ hẹp, volume co lại",
        "range": {
            "low": round(range_low, 0),
            "high": round(range_high, 0),
        },
        "breakout_watch": {
            "bullish_break": f"Vượt {range_high:,.0f} VND với volume đột biến",
            "bearish_break": f"Mất {range_low:,.0f} VND trên close",
        },
    }


def _bearish_scenario(
    close: float, sma: dict, bb: dict | None, vol: float | None,
    short: dict, mid: dict, long: dict, alignment: str,
) -> dict:
    """Bearish (negative) scenario."""
    bears = _count_bearish(short, mid, long)

    # Target zone: nearest support → second support
    s1 = _nearest_below(close, sma)
    s2 = _second_below(close, sma)
    bb_lower = bb.get("lower") if bb else None

    target_to = s1[1] if s1 else (bb_lower if bb_lower else round(close * 0.95, 0))
    target_from = s2[1] if s2 else round(close * 0.90, 0)
    if target_from >= target_to:
        target_from = round(target_to * 0.95, 0)

    # Trigger: breaking nearest support
    trigger_level = s1[1] if s1 else round(close * 0.97, 0)
    trigger_name = s1[0] if s1 else "hỗ trợ gần nhất"

    # Invalidation: reclaiming nearest resistance
    r1 = _nearest_above(close, sma)
    inv_level = r1[1] if r1 else round(close * 1.03, 0)
    inv_name = r1[0] if r1 else "kháng cự gần nhất"

    conditions = []
    rsi = short.get("rsi")
    if rsi and rsi < 40:
        conditions.append(f"RSI ({rsi:.0f}) nghiêng về vùng yếu")
    macd = short.get("macd")
    if macd and macd.get("histogram") and macd["histogram"] < 0:
        conditions.append("MACD histogram âm, xung lực giảm")
    if "bearish" in str(mid.get("trend_bias", "")):
        conditions.append("Xu hướng trung hạn tiêu cực")
    if long.get("foreign_stance") == "net_selling":
        conditions.append("Khối ngoại bán ròng tạo áp lực")
    if not conditions:
        conditions.append("Áp lực bán chiếm ưu thế")

    return {
        "name": "Kịch bản tiêu cực",
        "bias": "bearish",
        "probability": _probability(bears),
        "supporting_signals": bears,
        "conditions": "; ".join(conditions),
        "trigger": f"Mất {trigger_name} ({trigger_level:,.0f} VND) trên close",
        "target_zone": {
            "from": round(target_to, 0),
            "to": round(target_from, 0),
            "downside_pct": round((close - target_from) / close * 100, 1),
        },
        "invalidation": {
            "level": round(inv_level, 0),
            "basis": inv_name,
            "description": f"Giá phục hồi đóng cửa trên {inv_name} ({inv_level:,.0f} VND)",
        },
    }


# ------------------------------------------------------------------ dominant


def _dominant(scenarios: list[dict], alignment: str) -> str:
    """Identify which scenario has the most support."""
    if not scenarios:
        return "unknown"

    # If all horizons agree, that scenario is dominant
    if alignment == "all_bullish":
        return "Kịch bản tích cực"
    if alignment == "all_bearish":
        return "Kịch bản tiêu cực"

    # Otherwise pick the scenario with the most supporting signals
    best = max(scenarios, key=lambda s: s.get("supporting_signals", 0))

    # Tie-break: if neutral and another have same count, prefer neutral
    # (conservative principle when signals conflict)
    top_count = best.get("supporting_signals", 0)
    tied = [s for s in scenarios if s.get("supporting_signals", 0) == top_count]
    if len(tied) > 1:
        for s in tied:
            if s.get("bias") == "neutral":
                return s["name"]

    return best["name"]
