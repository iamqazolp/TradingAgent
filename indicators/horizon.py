"""Multi-horizon analysis: short / mid / long-term technical views.

This module is an *interpretation layer* over the indicator engine. It reads the
output dicts from :func:`engine.compute` (scalar fields only, never Series) and
classifies the current technical state for three investment horizons.

Horizons and their inputs
-------------------------
The point of three horizons is three *different* readings, so each one is built
from windows appropriate to its own timeframe. An earlier version read the same
daily RSI, MACD, Bollinger, volume bias and foreign stance into all three and
varied only the SMA pair, which meant "all three horizons agree" was one signal
counted three times.

======================  ==========================================================
Ngắn hạn (1–4 tuần)     EMA12 / SMA20, RSI(14), MACD ngày, close percentile 20d,
                        imbalance 5 phiên, volume ratio, khối ngoại 20d, streak
Trung hạn (1–3 tháng)   SMA20/50/100, giao cắt 20/50, RSI(14), MACD ngày,
                        percentile 60d, phân kỳ OBV 60d, khối ngoại 60d, xác
                        nhận khung tuần
Dài hạn (> 3 tháng)     SMA20/50 tuần, SMA200 ngày, giao cắt 50/200, RSI tuần,
                        MACD tuần, percentile 126d, khối ngoại 120d, return 250d
======================  ==========================================================

Every horizon returns a ``components`` list: one entry per indicator group that
contributed, each with a ``direction`` (+1 / 0 / −1), a ``weight``, and a
Vietnamese ``evidence`` string that already contains the number. Components with
no data are reported with ``direction: null`` and excluded from the score rather
than counted as neutral, so a thin history lowers *confidence* instead of
dragging the verdict toward the middle.

Consumers should render ``components``, ``confidence_reason``, ``conflicts`` and
``invalidation`` as given. They are computed here precisely so that a small
model does not have to re-derive them.
"""

from __future__ import annotations

from typing import Any

from indicators import finite, is_insufficient, pick

# --------------------------------------------------------------------------- extractors


def _entry(groups: dict, group: str, name: str) -> dict | None:
    """One indicator entry, or None when absent or insufficient."""
    found = pick(groups.get(group), name)
    if found is None or is_insufficient(found) or not isinstance(found, dict):
        return None
    return found


def _scalar(groups: dict, group: str, name: str, field: str = "latest") -> Any:
    entry = _entry(groups, group, name)
    return None if entry is None else entry.get(field)


def _sma_latest(groups: dict, key: str) -> float | None:
    return finite(_scalar(groups, "trend", key))


def _sma_direction(groups: dict, key: str) -> str | None:
    return _scalar(groups, "trend", key, "direction")


def _ema_latest(groups: dict, key: str) -> float | None:
    return finite(_scalar(groups, "trend", key))


def _rsi_latest(groups: dict) -> float | None:
    """Latest RSI.

    Resolved through :func:`pick` because the momentum group keys RSI by its
    window (``rsi_14`` by default, ``rsi_9`` if a caller overrides
    ``rsi_window``). Reading a hardcoded ``"rsi"`` returned None on every call
    and silently removed momentum from all three horizon verdicts.
    """
    return finite(_scalar(groups, "momentum", "rsi"))


def _rsi_zone(groups: dict) -> str | None:
    return _scalar(groups, "momentum", "rsi", "zone")


def _macd_snapshot(groups: dict) -> dict | None:
    macd = _entry(groups, "trend", "macd")
    if macd is None:
        return None
    latest = macd.get("latest") or {}
    return {
        "macd": finite(latest.get("macd")),
        "signal": finite(latest.get("signal")),
        "histogram": finite(latest.get("histogram")),
        "crossover": macd.get("crossover"),
    }


def _bollinger_snapshot(groups: dict) -> dict | None:
    band = _entry(groups, "volatility", "bollinger")
    if band is None:
        return None
    latest = band.get("latest") or {}
    return {
        "upper": finite(latest.get("upper")),
        "middle": finite(latest.get("middle")),
        "lower": finite(latest.get("lower")),
        "percent_b_pct": finite(latest.get("percent_b_pct")),
        "bandwidth_pct": finite(latest.get("bandwidth_pct")),
        "position": band.get("position"),
        "squeeze": band.get("squeeze"),
    }


def _volatility_snapshot(groups: dict) -> dict | None:
    """Close-to-close volatility, with both units named explicitly.

    The group reports ``latest`` in *daily* percent and ``latest_annualized_pct``
    annualized; exposing a bare ``volatility`` number invited callers to print
    the daily figure under an annualized label.
    """
    entry = _entry(groups, "volatility", "close_to_close_vol")
    if entry is None:
        return None
    return {
        "daily_pct": finite(entry.get("latest_daily_pct") or entry.get("latest")),
        "annualized_pct": finite(entry.get("latest_annualized_pct")),
        "suggested_stop_distance_pct": finite(entry.get("suggested_stop_distance_pct")),
        "is_atr_substitute": True,
        "label": "biến động close-to-close (thay thế ATR, không phải ATR)",
    }


def _percentile(groups: dict, window_key: str) -> dict | None:
    """Close percentile for one window from ``close_percentile_by_window``."""
    by_window = (groups.get("momentum") or {}).get("close_percentile_by_window")
    if not isinstance(by_window, dict):
        return None
    entry = by_window.get(window_key)
    if not isinstance(entry, dict) or entry.get("value") is None:
        return None
    return entry


def _foreign_window(groups: dict, window_key: str) -> dict | None:
    """One window of foreign net value (``20d`` / ``60d`` / ``120d``)."""
    entry = _entry(groups, "foreign_flow", "foreign_net_value")
    if entry is None:
        return None
    windows = entry.get("windows")
    if not isinstance(windows, dict):
        return None
    found = windows.get(window_key)
    return found if isinstance(found, dict) else None


def _foreign_overall(groups: dict) -> dict:
    entry = _entry(groups, "foreign_flow", "foreign_net_value")
    if entry is None:
        return {"stance": None, "cumulative_bil_vnd": None}
    return {
        "stance": entry.get("stance"),
        "cumulative_bil_vnd": finite(entry.get("cumulative_bil_vnd")),
    }


def _volume_imbalance(groups: dict) -> dict | None:
    entry = _entry(groups, "volume_flow", "buy_sell_volume_imbalance")
    if entry is None:
        return None
    return {
        "latest_rolling_avg": finite(entry.get("latest_rolling_avg")),
        "bias": entry.get("bias"),
        "window": entry.get("window"),
    }


def _volume_bias(groups: dict) -> str | None:
    snapshot = _volume_imbalance(groups)
    return None if snapshot is None else snapshot.get("bias")


def _obv_divergence(groups: dict, window_key: str) -> dict | None:
    holder = (groups.get("volume_flow") or {}).get("obv_divergence")
    if not isinstance(holder, dict):
        return None
    entry = holder.get(f"divergence_{window_key}")
    if not isinstance(entry, dict) or is_insufficient(entry):
        return None
    return entry


def _crossover(groups: dict, key: str) -> dict | None:
    entry = (groups.get("trend") or {}).get(key)
    if not isinstance(entry, dict) or is_insufficient(entry):
        return None
    if entry.get("last_event") in (None, "none"):
        return None
    return entry


def _returns(daily: dict) -> dict:
    returns = daily.get("returns")
    return returns if isinstance(returns, dict) else {}


# --------------------------------------------------------------------------- components


def component(
    group: str,
    label: str,
    direction: int | None,
    evidence: str | None,
    weight: float = 1.0,
    missing_reason: str | None = None,
) -> dict:
    """One scored piece of evidence for a horizon verdict.

    ``direction`` is +1 bullish, −1 bearish, 0 explicitly neutral, or None when
    the underlying indicator had no data. None is *not* the same as 0: a missing
    RSI should reduce confidence, whereas an RSI of exactly 50 is a real neutral
    reading.
    """
    return {
        "group": group,
        "label": label,
        "direction": direction,
        "weight": weight,
        "evidence": evidence,
        "missing_reason": missing_reason,
    }


_STRENGTH_BANDS: tuple[tuple[float, str], ...] = (
    (0.50, "strong_bullish"),
    (0.25, "moderate_bullish"),
    (0.10, "lean_bullish"),
    (-0.10, "neutral"),
    (-0.25, "lean_bearish"),
    (-0.50, "moderate_bearish"),
)


def _label_for_score(score: float) -> str:
    for threshold, label in _STRENGTH_BANDS:
        if score >= threshold:
            return label
    return "strong_bearish"


def _aggregate(components: list[dict]) -> dict:
    """Score a horizon from its components, with coverage-based confidence."""
    total_weight = sum(c["weight"] for c in components)
    scored = [c for c in components if c["direction"] is not None]
    available_weight = sum(c["weight"] for c in scored)
    missing = [c for c in components if c["direction"] is None]

    if available_weight == 0:
        return {
            "signal_strength": "insufficient_data",
            "score": None,
            "coverage_pct": 0.0,
            "agreement_pct": None,
            "confidence": "thấp",
            "confidence_reason": "Không có nhóm chỉ báo nào đủ dữ liệu cho khung này.",
            "groups_bullish": 0,
            "groups_bearish": 0,
            "groups_neutral": 0,
            "groups_missing": [c["label"] for c in missing],
            "conflicts": [],
            "conflict_summary": None,
        }

    weighted = sum(c["direction"] * c["weight"] for c in scored)
    score = weighted / available_weight
    directional = [c for c in scored if c["direction"] != 0]
    directional_weight = sum(c["weight"] for c in directional)
    agreement = (
        abs(sum(c["direction"] * c["weight"] for c in directional)) / directional_weight
        if directional_weight
        else 0.0
    )
    coverage = available_weight / total_weight if total_weight else 0.0

    bullish = [c for c in scored if c["direction"] > 0]
    bearish = [c for c in scored if c["direction"] < 0]
    neutral = [c for c in scored if c["direction"] == 0]

    # Opposing pairs, strongest first and capped: the full cross product of
    # bulls x bears inflates a 3-vs-3 split into nine "conflicts" and buries the
    # one that matters.
    pairs = [
        (up, down)
        for up in bullish
        for down in bearish
        if up["weight"] >= 0.5 and down["weight"] >= 0.5
    ]
    pairs.sort(key=lambda pair: -(pair[0]["weight"] + pair[1]["weight"]))
    # Only the group names and a description: the numeric evidence for each side
    # is already carried once in `components`, and repeating it here made
    # conflicts one of the largest fields in the payload.
    conflicts = [
        {
            "bullish_group": up["label"],
            "bearish_group": down["label"],
            "description": f"{up['label']} nghiêng tăng, {down['label']} nghiêng giảm",
            "combined_weight": round(up["weight"] + down["weight"], 2),
        }
        for up, down in pairs[:3]
    ]
    conflict_summary = None
    if bullish and bearish:
        conflict_summary = (
            "Nghiêng tăng: " + ", ".join(c["label"] for c in bullish)
            + " | Nghiêng giảm: " + ", ".join(c["label"] for c in bearish)
        )

    if coverage >= 0.75 and agreement >= 0.60:
        confidence = "cao"
    elif coverage >= 0.50 and agreement >= 0.40:
        confidence = "trung bình"
    else:
        confidence = "thấp"

    reason_parts = [
        f"{len(bullish)} nhóm tăng / {len(bearish)} nhóm giảm / {len(neutral)} nhóm trung tính",
        f"độ phủ dữ liệu {coverage * 100:.0f}%",
        f"độ đồng thuận {agreement * 100:.0f}%",
    ]
    if missing:
        reason_parts.append("thiếu dữ liệu: " + ", ".join(c["label"] for c in missing))
    if bullish and bearish:
        reason_parts.append(
            f"{len(bullish)} nhóm tăng xung đột với {len(bearish)} nhóm giảm"
        )

    return {
        "signal_strength": _label_for_score(score),
        "score": round(score, 3),
        "coverage_pct": round(coverage * 100, 1),
        "agreement_pct": round(agreement * 100, 1),
        "confidence": confidence,
        "confidence_reason": "; ".join(reason_parts) + ".",
        "groups_bullish": len(bullish),
        "groups_bearish": len(bearish),
        "groups_neutral": len(neutral),
        "groups_missing": [c["label"] for c in missing],
        "conflicts": conflicts,
        "conflict_summary": conflict_summary,
    }


# --------------------------------------------------------------------------- shared classifiers


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
        return f"{base}_macd_positive" if macd_hist > 0 else f"{base}_macd_negative"
    return base


def _trend_direction(bias: str) -> int | None:
    """Map a trend label to a score direction."""
    if bias == "insufficient_data":
        return None
    if "bullish" in bias:
        return 1
    if "bearish" in bias:
        return -1
    return 0


def compute_key_levels(
    close: float,
    sma_values: dict[str, float | None],
    bollinger: dict | None,
) -> dict:
    """Nearest support and resistance from the supplied MA and band levels.

    Retained for the per-horizon MA view. Whole-report support/resistance now
    comes from :mod:`indicators.levels`, which also uses swing structure and
    close extremes.
    """
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
                "basis": "Bollinger dưới",
                "distance_pct": round(abs(close - lower) / close * 100, 2),
            })
        if upper is not None and upper > close and close != 0:
            resistances.append({
                "level": round(upper, 0),
                "basis": "Bollinger trên",
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
    """Aggregate a label from four coarse inputs.

    Kept for callers that only have these four summaries. The per-horizon
    verdicts use :func:`_aggregate` over weighted components instead, which
    reports coverage and conflicts as well as a label.
    """
    _SCORES = {
        "strong_bullish": 2, "moderate_bullish": 1, "bullish": 1,
        "weak_bullish": 0.5, "above_long_term_ma": 0.5,
        "overbought": 0.5, "neutral": 0, "insufficient_data": 0,
        "oversold": -0.5, "weak_bearish": -0.5, "below_long_term_ma": -0.5,
        "moderate_bearish": -1, "bearish": -1, "strong_bearish": -2,
    }
    total = _SCORES.get(trend_bias, 0)
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


# --------------------------------------------------------------------------- shared builders


def _rsi_component(groups: dict, weight: float, label: str, timeframe: str) -> dict:
    rsi = _rsi_latest(groups)
    if rsi is None:
        return component(
            "momentum", label, None, None, weight,
            missing_reason=f"RSI {timeframe} chưa đủ dữ liệu",
        )
    zone = _rsi_zone(groups) or "neutral"
    if rsi >= 70:
        direction, note = 0, "vùng quá mua, rủi ro điều chỉnh ngắn hạn"
    elif rsi <= 30:
        direction, note = 0, "vùng quá bán, có thể bật hồi kỹ thuật"
    elif rsi >= 55:
        direction, note = 1, "nghiêng tích cực"
    elif rsi <= 45:
        direction, note = -1, "nghiêng tiêu cực"
    else:
        direction, note = 0, "vùng trung tính"
    return component(
        "momentum", label, direction,
        f"RSI(14) {timeframe} = {rsi:.1f} ({zone}) — {note}", weight,
    )


def _macd_component(groups: dict, weight: float, label: str, timeframe: str) -> dict:
    macd = _macd_snapshot(groups)
    if macd is None or macd.get("histogram") is None:
        return component(
            "momentum", label, None, None, weight,
            missing_reason=f"MACD {timeframe} chưa đủ dữ liệu",
        )
    hist = macd["histogram"]
    crossover = macd.get("crossover")
    direction = 1 if hist > 0 else (-1 if hist < 0 else 0)
    cross_note = {
        "bullish_cross": ", vừa cắt lên đường signal",
        "bearish_cross": ", vừa cắt xuống đường signal",
    }.get(crossover, "")
    return component(
        "momentum", label, direction,
        f"MACD {timeframe}: line = {macd['macd']:.1f}, signal = {macd['signal']:.1f}, "
        f"histogram = {hist:+.1f}{cross_note}", weight,
    )


def _percentile_component(groups: dict, window_key: str, weight: float, label: str) -> dict:
    entry = _percentile(groups, window_key)
    if entry is None:
        return component(
            "position", label, None, None, weight,
            missing_reason=f"chưa đủ {window_key} phiên để tính vị trí giá",
        )
    value = entry["value"]
    pct = value * 100
    if value >= 0.7:
        direction, note = 1, "nằm ở nửa trên biên độ"
    elif value <= 0.3:
        direction, note = -1, "nằm ở nửa dưới biên độ"
    else:
        direction, note = 0, "ở giữa biên độ"
    return component(
        "position", label, direction,
        f"Giá đứng ở {pct:.0f}% biên độ close {window_key} "
        f"({entry['range_low']:,.0f}–{entry['range_high']:,.0f} VND) — {note}", weight,
    )


def _foreign_component(groups: dict, window_key: str, weight: float, label: str) -> dict:
    entry = _foreign_window(groups, window_key)
    if entry is None:
        return component(
            "foreign_flow", label, None, None, weight,
            missing_reason=f"chưa đủ {window_key} phiên dữ liệu khối ngoại",
        )
    net = finite(entry.get("net_value_bil"))
    if net is None:
        return component(
            "foreign_flow", label, None, None, weight,
            missing_reason=f"giá trị ròng khối ngoại {window_key} không xác định",
        )
    direction = 1 if net > 0 else (-1 if net < 0 else 0)
    verb = "mua ròng" if net > 0 else ("bán ròng" if net < 0 else "cân bằng")
    summary = entry.get("summary") or ""
    return component(
        "foreign_flow", label, direction,
        f"Khối ngoại {window_key}: {verb} {abs(net):,.2f} tỷ VND"
        + (f" ({summary})" if summary else ""), weight,
    )


def _structure_component(
    close: float,
    levels: dict[str, float | None],
    directions: dict[str, str | None],
    weight: float,
    label: str,
) -> dict:
    """Price vs a set of moving averages, plus their slope."""
    present = {name: value for name, value in levels.items() if value is not None}
    if not present:
        return component(
            "trend", label, None, None, weight,
            missing_reason="chưa đủ dữ liệu cho các đường trung bình của khung này",
        )
    above = [name for name, value in present.items() if close > value]
    below = [name for name, value in present.items() if close <= value]
    ordered = sorted(present.items(), key=lambda item: -item[1])
    detail = ", ".join(
        f"{name} {value:,.0f} VND ({(close - value) / value * 100:+.2f}%"
        + (f", {directions[name]}" if directions.get(name) else "")
        + ")"
        for name, value in ordered
    )
    if len(above) == len(present):
        direction, note = 1, f"giá trên toàn bộ {len(present)} đường"
    elif len(below) == len(present):
        direction, note = -1, f"giá dưới toàn bộ {len(present)} đường"
    else:
        direction, note = 0, f"giá trên {len(above)}/{len(present)} đường"
    return component("trend", label, direction, f"{note}: {detail}", weight)


def _crossover_component(groups: dict, key: str, weight: float, label: str) -> dict:
    entry = _crossover(groups, key)
    if entry is None:
        return component(
            "trend", label, None, None, weight,
            missing_reason="chưa ghi nhận giao cắt nào trong khoảng dữ liệu",
        )
    event = entry["last_event"]
    direction = 1 if event == "golden_cross" else -1
    name = "giao cắt vàng (golden cross)" if direction == 1 else "giao cắt tử thần (death cross)"
    return component("trend", label, direction, f"{label}: {name} ngày {entry['date']}", weight)


def _invalidation(bias_direction: int, levels: dict | None) -> dict | None:
    """The level whose loss (or reclaim) would overturn the verdict.

    A neutral verdict has no single side to invalidate, so it reports both
    boundaries of the range instead of borrowing the bullish wording.
    """
    if not isinstance(levels, dict) or is_insufficient(levels):
        return None
    support = levels.get("nearest_support")
    resistance = levels.get("nearest_resistance")

    if bias_direction > 0:
        if not support:
            return None
        return {
            "level": support["level"],
            "basis": support["basis"],
            "distance_pct": support.get("distance_pct"),
            "condition": (
                f"Nhận định tích cực bị vô hiệu nếu giá ĐÓNG CỬA dưới "
                f"{support['level']:,.0f} VND ({support['basis']})"
            ),
        }
    if bias_direction < 0:
        if not resistance:
            return None
        return {
            "level": resistance["level"],
            "basis": resistance["basis"],
            "distance_pct": resistance.get("distance_pct"),
            "condition": (
                f"Nhận định tiêu cực bị vô hiệu nếu giá ĐÓNG CỬA trên "
                f"{resistance['level']:,.0f} VND ({resistance['basis']})"
            ),
        }

    # Neutral: name whichever boundaries exist, in both directions.
    if not support and not resistance:
        return None
    parts = []
    if resistance:
        parts.append(f"đóng cửa trên {resistance['level']:,.0f} VND ({resistance['basis']})")
    if support:
        parts.append(f"đóng cửa dưới {support['level']:,.0f} VND ({support['basis']})")
    return {
        "level": None,
        "basis": "hai biên vùng đi ngang",
        "upper_level": resistance["level"] if resistance else None,
        "upper_basis": resistance["basis"] if resistance else None,
        "lower_level": support["level"] if support else None,
        "lower_basis": support["basis"] if support else None,
        "condition": (
            "Nhận định trung tính bị vô hiệu nếu " + " hoặc ".join(parts)
        ),
    }


# --------------------------------------------------------------------------- per-horizon


def _short_term(daily: dict, close: float, levels: dict | None) -> dict:
    """Ngắn hạn (swing): 1–4 tuần, 5–20 phiên. Daily indicators, short windows."""
    groups = daily.get("groups", {})

    ema12 = _ema_latest(groups, "ema_12")
    sma20 = _sma_latest(groups, "sma_20")
    macd = _macd_snapshot(groups)
    bb = _bollinger_snapshot(groups)
    vol = _volatility_snapshot(groups)
    imbalance = _volume_imbalance(groups)
    returns = _returns(daily)

    components = [
        _structure_component(
            close,
            {"EMA12": ema12, "SMA20": sma20},
            {"EMA12": None, "SMA20": _sma_direction(groups, "sma_20")},
            1.0,
            "Cấu trúc giá ngắn hạn (EMA12/SMA20)",
        ),
        _rsi_component(groups, 0.8, "Động lượng RSI ngày", "ngày"),
        _macd_component(groups, 0.8, "Động lượng MACD ngày", "ngày"),
        _percentile_component(groups, "20d", 0.5, "Vị trí giá trong biên độ 20 phiên"),
        _foreign_component(groups, "20d", 0.6, "Dòng vốn ngoại 20 phiên"),
    ]

    # Matched-volume imbalance over its own short window.
    if imbalance is None or imbalance.get("latest_rolling_avg") is None:
        components.append(component(
            "volume_flow", "Cân bằng cung cầu khớp lệnh", None, None, 0.6,
            missing_reason="chưa đủ dữ liệu khối lượng mua/bán",
        ))
    else:
        avg = imbalance["latest_rolling_avg"]
        bias = imbalance.get("bias")
        direction = 1 if bias == "buy_side" else (-1 if bias == "sell_side" else 0)
        components.append(component(
            "volume_flow", "Cân bằng cung cầu khớp lệnh", direction,
            f"Mất cân đối mua/bán {imbalance.get('window')} phiên = {avg:+.3f} ({bias})", 0.6,
        ))

    # Return streak: a short-term-only read.
    streak = _entry(groups, "momentum", "return_streak")
    if streak is None or not streak.get("direction"):
        components.append(component(
            "momentum", "Chuỗi phiên tăng/giảm", None, None, 0.3,
            missing_reason="chưa đủ dữ liệu chuỗi phiên",
        ))
    else:
        count = abs(int(streak.get("streak") or 0))
        way = streak.get("direction")
        direction = 1 if way == "up" else (-1 if way == "down" else 0)
        components.append(component(
            "momentum", "Chuỗi phiên tăng/giảm", direction,
            f"Chuỗi {count} phiên {'tăng' if way == 'up' else 'giảm' if way == 'down' else 'đi ngang'} "
            f"liên tiếp ({streak.get('flag')})", 0.3,
        ))

    verdict = _aggregate(components)
    bias_direction = 1 if "bullish" in verdict["signal_strength"] else (
        -1 if "bearish" in verdict["signal_strength"] else 0
    )
    sma_vals = {"EMA12": ema12, "SMA20": sma20, "SMA50": _sma_latest(groups, "sma_50")}

    return {
        "horizon": "short_term",
        "label_vi": "Ngắn hạn",
        "description": "1–4 tuần (5–20 phiên)",
        "inputs_used": "EMA12/SMA20, RSI(14) ngày, MACD ngày, percentile 20 phiên, "
                       "mất cân đối khớp lệnh 5 phiên, khối ngoại 20 phiên, chuỗi phiên",
        "trend_bias": classify_trend(close, ema12 or sma20, sma20 or ema12),
        "momentum": classify_momentum(_rsi_latest(groups), macd.get("histogram") if macd else None),
        "rsi": _rsi_latest(groups),
        "rsi_zone": _rsi_zone(groups),
        "macd": macd,
        "bollinger": bb,
        "volatility": vol,
        "volume_bias": imbalance.get("bias") if imbalance else None,
        "foreign_20d": _foreign_window(groups, "20d"),
        "returns_5d": returns.get("5d"),
        "returns_20d": returns.get("20d"),
        "sma_values": {k: v for k, v in sma_vals.items() if v is not None},
        "key_levels": compute_key_levels(close, sma_vals, bb),
        "components": components,
        "invalidation": _invalidation(bias_direction, levels),
        **verdict,
    }


def _mid_term(daily: dict, weekly: dict | None, close: float, levels: dict | None) -> dict:
    """Trung hạn (position): 1–3 tháng, 20–60 phiên. Mid SMAs + weekly confirmation."""
    groups = daily.get("groups", {})
    weekly_groups = (weekly or {}).get("groups") or {}

    sma20 = _sma_latest(groups, "sma_20")
    sma50 = _sma_latest(groups, "sma_50")
    sma100 = _sma_latest(groups, "sma_100")
    sma200 = _sma_latest(groups, "sma_200")
    macd = _macd_snapshot(groups)
    bb = _bollinger_snapshot(groups)
    vol = _volatility_snapshot(groups)
    returns = _returns(daily)

    components = [
        _structure_component(
            close,
            {"SMA20": sma20, "SMA50": sma50, "SMA100": sma100},
            {
                "SMA20": _sma_direction(groups, "sma_20"),
                "SMA50": _sma_direction(groups, "sma_50"),
                "SMA100": _sma_direction(groups, "sma_100"),
            },
            1.0,
            "Cấu trúc giá trung hạn (SMA20/50/100)",
        ),
        _crossover_component(groups, "sma_crossover_20_50", 0.6, "Giao cắt SMA20/50"),
        _rsi_component(groups, 0.6, "Động lượng RSI ngày", "ngày"),
        _macd_component(groups, 0.6, "Động lượng MACD ngày", "ngày"),
        _percentile_component(groups, "60d", 0.5, "Vị trí giá trong biên độ 60 phiên"),
        _foreign_component(groups, "60d", 0.7, "Dòng vốn ngoại 60 phiên"),
    ]

    # OBV divergence over the mid-term window.
    divergence = _obv_divergence(groups, "60")
    if divergence is None:
        components.append(component(
            "volume_flow", "Phân kỳ OBV 60 phiên", None, None, 0.4,
            missing_reason="chưa đủ 61 phiên để đo phân kỳ OBV",
        ))
    else:
        status = divergence.get("status")
        direction = {
            "bullish_divergence": 1, "bearish_divergence": -1, "in_sync": 0,
        }.get(status, 0)
        components.append(component(
            "volume_flow", "Phân kỳ OBV 60 phiên", direction,
            divergence.get("description"), 0.4,
        ))

    # Weekly confirmation: an input the short-term view deliberately does not use.
    weekly_confirm = None
    if weekly_groups:
        w_rsi = _rsi_latest(weekly_groups)
        w_macd = _macd_snapshot(weekly_groups)
        w_sma20 = _sma_latest(weekly_groups, "sma_20")
        weekly_confirm = {
            "weekly_rsi": w_rsi,
            "weekly_rsi_zone": _rsi_zone(weekly_groups),
            "weekly_macd": w_macd,
            "weekly_sma20": w_sma20,
            "weekly_trend_alignment": (weekly or {}).get("trend_alignment"),
        }
        if w_sma20 is not None:
            direction = 1 if close > w_sma20 else -1
            components.append(component(
                "weekly", "Xác nhận khung tuần (SMA20 tuần)", direction,
                f"Giá {close:,.0f} VND {'trên' if direction == 1 else 'dưới'} SMA20 tuần "
                f"{w_sma20:,.0f} VND ({(close - w_sma20) / w_sma20 * 100:+.2f}%)", 0.6,
            ))
        else:
            components.append(component(
                "weekly", "Xác nhận khung tuần (SMA20 tuần)", None, None, 0.6,
                missing_reason="chưa đủ 20 tuần dữ liệu",
            ))
    else:
        components.append(component(
            "weekly", "Xác nhận khung tuần (SMA20 tuần)", None, None, 0.6,
            missing_reason="chưa đủ dữ liệu khung tuần",
        ))

    verdict = _aggregate(components)
    bias_direction = 1 if "bullish" in verdict["signal_strength"] else (
        -1 if "bearish" in verdict["signal_strength"] else 0
    )
    sma_vals = {"SMA20": sma20, "SMA50": sma50, "SMA100": sma100, "SMA200": sma200}

    crossovers = {}
    for key in ("sma_crossover_20_50", "sma_crossover_50_200"):
        entry = (groups.get("trend") or {}).get(key)
        if isinstance(entry, dict) and not is_insufficient(entry):
            crossovers[key] = entry

    return {
        "horizon": "mid_term",
        "label_vi": "Trung hạn",
        "description": "1–3 tháng (20–60 phiên)",
        "inputs_used": "SMA20/50/100, giao cắt SMA20/50, RSI(14) ngày, MACD ngày, "
                       "percentile 60 phiên, phân kỳ OBV 60 phiên, khối ngoại 60 phiên, "
                       "xác nhận SMA20 khung tuần",
        "trend_bias": classify_trend(close, sma20, sma50, sma200),
        "momentum": classify_momentum(_rsi_latest(groups), macd.get("histogram") if macd else None),
        "rsi": _rsi_latest(groups),
        "rsi_zone": _rsi_zone(groups),
        "macd": macd,
        "bollinger": bb,
        "volatility": vol,
        "foreign_60d": _foreign_window(groups, "60d"),
        "foreign_overall": _foreign_overall(groups),
        "returns_20d": returns.get("20d"),
        "returns_60d": returns.get("60d"),
        "sma_values": {k: v for k, v in sma_vals.items() if v is not None},
        "sma_directions": {
            "SMA20": _sma_direction(groups, "sma_20"),
            "SMA50": _sma_direction(groups, "sma_50"),
            "SMA100": _sma_direction(groups, "sma_100"),
        },
        "crossovers": crossovers,
        "weekly_confirmation": weekly_confirm,
        "key_levels": compute_key_levels(close, sma_vals, bb),
        "components": components,
        "invalidation": _invalidation(bias_direction, levels),
        **verdict,
    }


def _long_term(daily: dict, weekly: dict | None, close: float, levels: dict | None) -> dict:
    """Dài hạn (trend): > 3 tháng. Weekly indicators primary, daily SMA200 secondary."""
    groups = daily.get("groups", {})
    weekly_groups = (weekly or {}).get("groups") or {}

    sma200 = _sma_latest(groups, "sma_200")
    sma200_dir = _sma_direction(groups, "sma_200")
    w_sma20 = _sma_latest(weekly_groups, "sma_20") if weekly_groups else None
    w_sma50 = _sma_latest(weekly_groups, "sma_50") if weekly_groups else None
    w_rsi = _rsi_latest(weekly_groups) if weekly_groups else None
    w_macd = _macd_snapshot(weekly_groups) if weekly_groups else None
    w_bb = _bollinger_snapshot(weekly_groups) if weekly_groups else None
    returns = _returns(daily)

    components = [
        _structure_component(
            close,
            {"SMA20 tuần": w_sma20, "SMA50 tuần": w_sma50},
            {},
            1.0,
            "Cấu trúc giá khung tuần (SMA20/50 tuần)",
        ),
    ]

    # Daily SMA200: position and slope, the long-term anchor.
    if sma200 is None:
        components.append(component(
            "trend", "Vị thế so với SMA200 ngày", None, None, 0.9,
            missing_reason="chưa đủ 200 phiên dữ liệu",
        ))
    else:
        direction = 1 if close > sma200 else -1
        slope_note = f", SMA200 đang {sma200_dir}" if sma200_dir and sma200_dir != "unknown" else ""
        components.append(component(
            "trend", "Vị thế so với SMA200 ngày", direction,
            f"Giá {close:,.0f} VND {'trên' if direction == 1 else 'dưới'} SMA200 "
            f"{sma200:,.0f} VND ({(close - sma200) / sma200 * 100:+.2f}%){slope_note}", 0.9,
        ))

    components.append(_crossover_component(groups, "sma_crossover_50_200", 0.7, "Giao cắt SMA50/200"))

    if weekly_groups:
        components.append(_rsi_component(weekly_groups, 0.6, "Động lượng RSI tuần", "tuần"))
        components.append(_macd_component(weekly_groups, 0.6, "Động lượng MACD tuần", "tuần"))
    else:
        components.append(component(
            "momentum", "Động lượng RSI tuần", None, None, 0.6,
            missing_reason="chưa đủ dữ liệu khung tuần",
        ))
        components.append(component(
            "momentum", "Động lượng MACD tuần", None, None, 0.6,
            missing_reason="chưa đủ dữ liệu khung tuần",
        ))

    components.append(_percentile_component(groups, "126d", 0.5, "Vị trí giá trong biên độ 126 phiên"))
    components.append(_foreign_component(groups, "120d", 0.7, "Dòng vốn ngoại 120 phiên"))

    # Long-horizon return.
    long_return = returns.get("250d", returns.get("120d"))
    return_window = "250" if "250d" in returns else ("120" if "120d" in returns else None)
    if long_return is None:
        components.append(component(
            "trend", "Hiệu suất dài hạn", None, None, 0.4,
            missing_reason="chưa đủ 120 phiên dữ liệu",
        ))
    else:
        direction = 1 if long_return > 0 else (-1 if long_return < 0 else 0)
        components.append(component(
            "trend", "Hiệu suất dài hạn", direction,
            f"Hiệu suất {return_window} phiên = {long_return:+.2f}%", 0.4,
        ))

    verdict = _aggregate(components)
    bias_direction = 1 if "bullish" in verdict["signal_strength"] else (
        -1 if "bearish" in verdict["signal_strength"] else 0
    )

    if w_sma20 is not None and w_sma50 is not None:
        trend_bias = classify_trend(close, w_sma20, w_sma50, sma200)
    elif sma200 is not None:
        trend_bias = "above_long_term_ma" if close > sma200 else "below_long_term_ma"
    else:
        trend_bias = "insufficient_data"

    sma_vals: dict[str, float | None] = {"SMA200_daily": sma200}
    if w_sma20 is not None:
        sma_vals["SMA20_weekly"] = w_sma20
    if w_sma50 is not None:
        sma_vals["SMA50_weekly"] = w_sma50

    return {
        "horizon": "long_term",
        "label_vi": "Dài hạn",
        "description": "> 3 tháng (khung tuần, 26–52 tuần)",
        "inputs_used": "SMA20/50 khung tuần, SMA200 ngày, giao cắt SMA50/200, RSI tuần, "
                       "MACD tuần, percentile 126 phiên, khối ngoại 120 phiên, hiệu suất 250 phiên",
        "trend_bias": trend_bias,
        "momentum": classify_momentum(w_rsi, w_macd.get("histogram") if w_macd else None),
        "daily_sma200": sma200,
        "daily_sma200_direction": sma200_dir,
        "daily_trend_alignment": daily.get("trend_alignment"),
        "weekly_sma20": w_sma20,
        "weekly_sma50": w_sma50,
        "weekly_rsi": w_rsi,
        "weekly_rsi_zone": _rsi_zone(weekly_groups) if weekly_groups else None,
        "weekly_macd": w_macd,
        "weekly_bollinger": w_bb,
        "weekly_trend_alignment": (weekly or {}).get("trend_alignment"),
        "foreign_120d": _foreign_window(groups, "120d"),
        "foreign_overall": _foreign_overall(groups),
        "returns_120d": returns.get("120d"),
        "returns_250d": returns.get("250d"),
        "sma_values": {k: v for k, v in sma_vals.items() if v is not None},
        "key_levels": compute_key_levels(close, sma_vals, w_bb),
        "components": components,
        "invalidation": _invalidation(bias_direction, levels),
        **verdict,
    }


# --------------------------------------------------------------------------- alignment


def _horizon_alignment(short: dict, mid: dict, long: dict) -> dict:
    """Assess agreement across the three horizons.

    Returns a structured verdict rather than one of a dozen compound string
    labels, several of which were unreachable. The caller gets each horizon's
    direction explicitly, so it can describe any combination without this
    function having to enumerate them.
    """
    order = (("short_term", short), ("mid_term", mid), ("long_term", long))

    def direction_of(horizon: dict) -> int | None:
        strength = horizon.get("signal_strength", "neutral")
        if strength == "insufficient_data":
            return None
        if "bullish" in strength:
            return 1
        if "bearish" in strength:
            return -1
        return 0

    directions = {name: direction_of(h) for name, h in order}
    scored = [d for d in directions.values() if d is not None]
    bulls = sum(1 for d in scored if d > 0)
    bears = sum(1 for d in scored if d < 0)
    neutrals = sum(1 for d in scored if d == 0)

    if not scored:
        label, summary = "insufficient_data", "Không đủ dữ liệu để đánh giá đồng thuận đa khung."
    elif bulls == len(scored):
        label, summary = "all_bullish", "Cả ba khung thời gian đều nghiêng tích cực."
    elif bears == len(scored):
        label, summary = "all_bearish", "Cả ba khung thời gian đều nghiêng tiêu cực."
    elif bulls and bears:
        rising = [n for n, d in directions.items() if d == 1]
        falling = [n for n, d in directions.items() if d == -1]
        label = "conflicting"
        summary = (
            f"Các khung xung đột: {', '.join(_VI[n] for n in rising)} nghiêng tăng trong khi "
            f"{', '.join(_VI[n] for n in falling)} nghiêng giảm."
        )
    elif bulls:
        label, summary = "mostly_bullish", (
            f"{bulls}/{len(scored)} khung nghiêng tích cực, phần còn lại trung tính."
        )
    elif bears:
        label, summary = "mostly_bearish", (
            f"{bears}/{len(scored)} khung nghiêng tiêu cực, phần còn lại trung tính."
        )
    else:
        label, summary = "all_neutral", "Cả ba khung đều trung tính, chưa có hướng rõ ràng."

    # Confidence in the *alignment* rests on the horizons that actually form the
    # majority reading, not on every horizon: one low-confidence dissenter should
    # not erase two high-confidence agreeing horizons. Taking the minimum across
    # all three did exactly that.
    rank = {"cao": 2, "trung bình": 1, "thấp": 0}
    majority = 1 if bulls > bears else (-1 if bears > bulls else 0)
    agreeing = [h for name, h in order if directions[name] == majority]
    basis = agreeing or [h for _, h in order]
    weakest = min(
        (h.get("confidence", "thấp") for h in basis),
        key=lambda c: rank.get(c, 0),
    )

    return {
        "label": label,
        "summary": summary,
        "directions": directions,
        "horizons_bullish": bulls,
        "horizons_bearish": bears,
        "horizons_neutral": neutrals,
        "horizons_scored": len(scored),
        "confidence": weakest,
        "confidence_basis": (
            "mức tin cậy thấp nhất trong các khung cùng chiều với kết luận"
            if agreeing else "mức tin cậy thấp nhất trong cả ba khung"
        ),
        "per_horizon_confidence": {
            name: h.get("confidence") for name, h in order
        },
        "shared_input_caveat": (
            "Ngắn hạn và trung hạn cùng dùng RSI/MACD khung ngày, nên đồng thuận giữa hai "
            "khung này không phải hai bằng chứng độc lập. Dài hạn dựa chủ yếu vào khung tuần."
        ),
    }


_VI = {"short_term": "ngắn hạn", "mid_term": "trung hạn", "long_term": "dài hạn"}


# --------------------------------------------------------------------------- public API


def horizon_analysis(
    daily_compute: dict,
    weekly_compute: dict | None,
    latest_close: float,
    levels: dict | None = None,
) -> dict:
    """Produce a three-horizon technical analysis.

    Parameters
    ----------
    daily_compute
        Output of ``engine.compute()`` on daily rows.
    weekly_compute
        Output of ``engine.compute()`` on weekly bars, or None when there are
        too few weeks. The long-term view degrades to daily SMA200 in that case
        and says so through ``groups_missing``.
    latest_close
        Most recent daily close.
    levels
        Output of :func:`indicators.levels.key_levels`, used to attach a real
        invalidation level to each horizon. Optional.

    Returns
    -------
    dict
        ``short_term``, ``mid_term``, ``long_term``, plus ``horizon_alignment``.
    """
    if latest_close is None:
        return {
            "error": "no_close_price",
            "message": "Cannot perform horizon analysis without a close price.",
        }

    short = _short_term(daily_compute, latest_close, levels)
    mid = _mid_term(daily_compute, weekly_compute, latest_close, levels)
    long = _long_term(daily_compute, weekly_compute, latest_close, levels)
    alignment = _horizon_alignment(short, mid, long)

    return {
        "short_term": short,
        "mid_term": mid,
        "long_term": long,
        "horizon_alignment": alignment["label"],
        "alignment": alignment,
    }
