"""Conditional scenarios (bullish / neutral / bearish) from the technical state.

Each scenario is an "if… then…" projection built from levels that were actually
measured — moving averages, Bollinger bands, swing closes and N-session close
extremes, all supplied by :mod:`indicators.levels`. When a role has no such
level (no resistance above the close, say), the field is ``None`` with a reason
attached.

Two earlier behaviours are deliberately gone:

* **No invented levels.** Targets, triggers and invalidation points used to fall
  back to ``close * 1.05``, ``close * 0.90`` and similar, and were published
  under a ``basis`` of "kháng cự gần nhất" — naming a level that had never been
  computed. Nothing downstream could tell an SMA from a multiple of the close.
* **No ``probability`` field.** It held ``"high"``/``"medium"``/``"low"`` while
  the consuming skill rendered it as ``X%``, so a qualitative count of agreeing
  horizons was printed as a statistical estimate. The field is now
  ``likelihood`` with an explicit ``likelihood_basis`` and an
  ``is_probability_estimate: false`` marker.
"""

from __future__ import annotations

from indicators import finite, is_insufficient

_NOT_A_PROBABILITY = (
    "Nhãn khả năng phản ánh SỐ KHUNG THỜI GIAN đang ủng hộ kịch bản, "
    "KHÔNG phải xác suất thống kê. Không được quy đổi thành phần trăm."
)


def generate_scenarios(
    daily_compute: dict,
    weekly_compute: dict | None,
    horizons: dict,
    latest_close: float,
    levels: dict | None = None,
) -> dict:
    """Build three conditional scenarios from the current technical state."""
    if latest_close is None or latest_close <= 0:
        return {
            "scenarios": [],
            "dominant_scenario": None,
            "error": "invalid_close_price",
        }

    short = horizons.get("short_term", {})
    mid = horizons.get("mid_term", {})
    long = horizons.get("long_term", {})
    alignment = horizons.get("alignment") or {}

    supports = _levels_of(levels, "supports")
    resistances = _levels_of(levels, "resistances")

    scenarios = [
        _bullish_scenario(latest_close, supports, resistances, short, mid, long),
        _neutral_scenario(latest_close, supports, resistances, short, mid, long, alignment),
        _bearish_scenario(latest_close, supports, resistances, short, mid, long),
    ]

    return {
        "scenarios": scenarios,
        "dominant_scenario": _dominant(scenarios, alignment),
        "likelihood_note": _NOT_A_PROBABILITY,
        "levels_note": (
            "Mọi mốc giá trong các kịch bản đều là mức đã tính được từ dữ liệu "
            "(MA, Bollinger, đỉnh/đáy close). Không có mốc nào được suy diễn hay làm tròn tùy ý."
        ),
    }


# ------------------------------------------------------------------ helpers


def _levels_of(levels: dict | None, side: str) -> list[dict]:
    if not isinstance(levels, dict) or is_insufficient(levels):
        return []
    found = levels.get(side)
    return found if isinstance(found, list) else []


def _nth(levels: list[dict], index: int) -> dict | None:
    return levels[index] if len(levels) > index else None


def _directions(short: dict, mid: dict, long: dict) -> list[int | None]:
    out: list[int | None] = []
    for horizon in (short, mid, long):
        strength = horizon.get("signal_strength", "neutral")
        if strength == "insufficient_data":
            out.append(None)
        elif "bullish" in strength:
            out.append(1)
        elif "bearish" in strength:
            out.append(-1)
        else:
            out.append(0)
    return out


def _likelihood(supporting: int, scored: int) -> tuple[str, str]:
    """Qualitative label plus the count it came from."""
    if scored == 0:
        return "thấp", "không có khung nào đủ dữ liệu"
    basis = f"{supporting}/{scored} khung thời gian ủng hộ"
    if supporting >= 3:
        return "cao", basis
    if supporting == 2:
        return "trung bình", basis
    return "thấp", basis


def _level_ref(entry: dict | None, missing: str) -> dict:
    """A level reference, or an explicit absence with a reason."""
    if entry is None:
        return {"level": None, "basis": None, "unavailable_reason": missing}
    return {
        "level": entry["level"],
        "basis": entry["basis"],
        "confluence": entry.get("confluence"),
        "distance_pct": entry.get("distance_pct"),
    }


def _pct_from(close: float, level: float | None) -> float | None:
    if level is None or close <= 0:
        return None
    return round((level - close) / close * 100, 2)


# ------------------------------------------------------------------ scenario builders


def _bullish_scenario(
    close: float, supports: list[dict], resistances: list[dict],
    short: dict, mid: dict, long: dict,
) -> dict:
    directions = _directions(short, mid, long)
    scored = [d for d in directions if d is not None]
    bulls = sum(1 for d in scored if d > 0)
    likelihood, basis = _likelihood(bulls, len(scored))

    trigger = _nth(resistances, 0)
    target_1 = _nth(resistances, 1)
    target_2 = _nth(resistances, 2)
    invalidation = _nth(supports, 0)

    conditions = _bullish_conditions(short, mid, long)

    return {
        "name": "Kịch bản tích cực",
        "bias": "bullish",
        "likelihood": likelihood,
        "likelihood_basis": basis,
        "is_probability_estimate": False,
        "supporting_horizons": bulls,
        "horizons_scored": len(scored),
        "conditions": conditions,
        "conditions_text": "; ".join(conditions) if conditions else None,
        "trigger": {
            **_level_ref(trigger, "không còn mức kháng cự nào phía trên giá hiện tại"),
            "condition": (
                f"Đóng cửa vượt {trigger['level']:,.0f} VND ({trigger['basis']}) "
                f"kèm khối lượng trên trung bình 20 phiên"
                if trigger else None
            ),
        },
        "target_zone": {
            "from": target_1["level"] if target_1 else None,
            "from_basis": target_1["basis"] if target_1 else None,
            "to": target_2["level"] if target_2 else None,
            "to_basis": target_2["basis"] if target_2 else None,
            "upside_pct_to_first": _pct_from(close, target_1["level"] if target_1 else None),
            "upside_pct_to_second": _pct_from(close, target_2["level"] if target_2 else None),
            "unavailable_reason": (
                None if target_1 else "chưa xác định được mức kháng cự tiếp theo từ dữ liệu"
            ),
        },
        "invalidation": {
            **_level_ref(invalidation, "không còn mức hỗ trợ nào phía dưới giá hiện tại"),
            "condition": (
                f"Kịch bản bị phủ định nếu đóng cửa dưới {invalidation['level']:,.0f} VND "
                f"({invalidation['basis']})"
                if invalidation else None
            ),
        },
    }


def _bearish_scenario(
    close: float, supports: list[dict], resistances: list[dict],
    short: dict, mid: dict, long: dict,
) -> dict:
    directions = _directions(short, mid, long)
    scored = [d for d in directions if d is not None]
    bears = sum(1 for d in scored if d < 0)
    likelihood, basis = _likelihood(bears, len(scored))

    trigger = _nth(supports, 0)
    target_1 = _nth(supports, 1)
    target_2 = _nth(supports, 2)
    invalidation = _nth(resistances, 0)

    conditions = _bearish_conditions(short, mid, long)

    return {
        "name": "Kịch bản tiêu cực",
        "bias": "bearish",
        "likelihood": likelihood,
        "likelihood_basis": basis,
        "is_probability_estimate": False,
        "supporting_horizons": bears,
        "horizons_scored": len(scored),
        "conditions": conditions,
        "conditions_text": "; ".join(conditions) if conditions else None,
        "trigger": {
            **_level_ref(trigger, "không còn mức hỗ trợ nào phía dưới giá hiện tại"),
            "condition": (
                f"Đóng cửa mất {trigger['level']:,.0f} VND ({trigger['basis']})"
                if trigger else None
            ),
        },
        "target_zone": {
            "from": target_1["level"] if target_1 else None,
            "from_basis": target_1["basis"] if target_1 else None,
            "to": target_2["level"] if target_2 else None,
            "to_basis": target_2["basis"] if target_2 else None,
            "downside_pct_to_first": _pct_from(close, target_1["level"] if target_1 else None),
            "downside_pct_to_second": _pct_from(close, target_2["level"] if target_2 else None),
            "unavailable_reason": (
                None if target_1 else "chưa xác định được mức hỗ trợ tiếp theo từ dữ liệu"
            ),
        },
        "invalidation": {
            **_level_ref(invalidation, "không còn mức kháng cự nào phía trên giá hiện tại"),
            "condition": (
                f"Kịch bản bị phủ định nếu đóng cửa trên {invalidation['level']:,.0f} VND "
                f"({invalidation['basis']})"
                if invalidation else None
            ),
        },
    }


def _neutral_scenario(
    close: float, supports: list[dict], resistances: list[dict],
    short: dict, mid: dict, long: dict, alignment: dict,
) -> dict:
    directions = _directions(short, mid, long)
    scored = [d for d in directions if d is not None]
    neutrals = sum(1 for d in scored if d == 0)
    conflicting = alignment.get("label") == "conflicting"
    supporting = neutrals + (1 if conflicting else 0)
    likelihood, basis = _likelihood(min(supporting, len(scored)), len(scored))
    if conflicting:
        basis += " (tín hiệu đa khung xung đột)"

    low = _nth(supports, 0)
    high = _nth(resistances, 0)

    conditions: list[str] = []
    bollinger = short.get("bollinger") or {}
    if bollinger.get("squeeze"):
        bandwidth = finite(bollinger.get("bandwidth_pct"))
        conditions.append(
            "Bollinger Bands co thắt"
            + (f" (bandwidth = {bandwidth:.2f}%)" if bandwidth is not None else "")
            + " — tích lũy năng lượng"
        )
    if conflicting:
        conditions.append(alignment.get("summary") or "Tín hiệu đa khung xung đột")
    rsi = finite(short.get("rsi"))
    if rsi is not None and 45 <= rsi <= 55:
        conditions.append(f"RSI(14) = {rsi:.1f} ở vùng trung tính, không ủng hộ hướng nào")

    return {
        "name": "Kịch bản tích lũy / đi ngang",
        "bias": "neutral",
        "likelihood": likelihood,
        "likelihood_basis": basis,
        "is_probability_estimate": False,
        "supporting_horizons": neutrals,
        "horizons_scored": len(scored),
        "conditions": conditions,
        "conditions_text": "; ".join(conditions) if conditions else None,
        "range": {
            "low": low["level"] if low else None,
            "low_basis": low["basis"] if low else None,
            "high": high["level"] if high else None,
            "high_basis": high["basis"] if high else None,
            "width_pct": (
                round((high["level"] - low["level"]) / close * 100, 2)
                if low and high and close > 0 else None
            ),
            "unavailable_reason": (
                None if (low and high)
                else "chưa xác định được đủ hai biên hỗ trợ/kháng cự từ dữ liệu"
            ),
        },
        "breakout_watch": {
            "bullish_break": (
                f"Đóng cửa vượt {high['level']:,.0f} VND ({high['basis']}) kèm volume đột biến"
                if high else None
            ),
            "bearish_break": (
                f"Đóng cửa mất {low['level']:,.0f} VND ({low['basis']})"
                if low else None
            ),
        },
    }


# ------------------------------------------------------------------ conditions


#: How many supporting conditions to list per scenario. The full set repeats
#: every matching component's evidence across three horizons, which made
#: `conditions` one of the largest fields for little added meaning; the
#: highest-weighted few carry the argument.
_MAX_CONDITIONS = 4


def _bullish_conditions(short: dict, mid: dict, long: dict) -> list[str]:
    return _conditions_for(short, mid, long, 1)


def _bearish_conditions(short: dict, mid: dict, long: dict) -> list[str]:
    return _conditions_for(short, mid, long, -1)


def _conditions_for(short: dict, mid: dict, long: dict, direction: int) -> list[str]:
    """Highest-weighted evidence pointing one way, deduplicated across horizons.

    Short and mid term share the daily RSI and MACD readings, so the same
    sentence arrives twice under different horizon labels. Listing both would
    overstate how much independent evidence there is.
    """
    gathered: list[tuple[float, str, str]] = []
    for horizon, name in ((short, "ngắn hạn"), (mid, "trung hạn"), (long, "dài hạn")):
        for comp in horizon.get("components", []):
            if comp.get("direction") == direction and comp.get("evidence"):
                gathered.append((comp.get("weight", 0.0), name, comp["evidence"]))

    gathered.sort(key=lambda item: -item[0])
    seen: set[str] = set()
    out: list[str] = []
    for _weight, name, evidence in gathered:
        if evidence in seen:
            continue
        seen.add(evidence)
        out.append(f"[{name}] {evidence}")
        if len(out) >= _MAX_CONDITIONS:
            break
    return out


# ------------------------------------------------------------------ dominant


def _dominant(scenarios: list[dict], alignment: dict) -> str:
    """Name the scenario with the most horizon support."""
    if not scenarios:
        return "unknown"

    label = alignment.get("label")
    if label == "all_bullish":
        return "Kịch bản tích cực"
    if label == "all_bearish":
        return "Kịch bản tiêu cực"
    if label in ("conflicting", "all_neutral"):
        return "Kịch bản tích lũy / đi ngang"

    best = max(scenarios, key=lambda s: s.get("supporting_horizons", 0))
    top = best.get("supporting_horizons", 0)
    tied = [s for s in scenarios if s.get("supporting_horizons", 0) == top]
    if len(tied) > 1:
        # Conservative tie-break: prefer consolidation when signals do not agree.
        for scenario in tied:
            if scenario.get("bias") == "neutral":
                return scenario["name"]
    return best["name"]
