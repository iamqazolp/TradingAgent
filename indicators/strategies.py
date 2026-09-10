"""Technical perspectives and observation levels per horizon (pure analysis).

Objective technical observations and key price levels for the short, mid and
long term. No buy/sell advice, no entry, take-profit or stop-loss
recommendation: those are the root agent's business, and this layer must not
put words in its mouth.

Every level quoted here comes from :mod:`indicators.levels` or from a moving
average computed by the trend group. An earlier version filled gaps with
``close * 0.95`` / ``close * 1.08`` and a "volatility floor" of
``close − 2σ``, which read as measured support in the payload; those fallbacks
are gone, and an absent level is now reported as absent.
"""

from __future__ import annotations

from indicators import finite, is_insufficient

_DISCLAIMER = (
    "Báo cáo thuần túy là phân tích kỹ thuật và dòng tiền định lượng khách quan, "
    "TUYỆT ĐỐI KHÔNG cấu thành lời khuyên hoặc khuyến nghị mua/bán chứng khoán dưới bất kỳ hình thức nào. "
    "Mọi mốc giá chỉ mang tính tham chiếu kỹ thuật."
)


_VN_MARKET_RULES = {
    "t_plus": "T+2.5 — cổ phiếu mua T0 về tài khoản chiều T+2. Không thể lướt sóng trong ngày.",
    "no_short_selling": True,
    "price_limits": "HOSE ±7%, HNX ±10%, UPCoM ±15%",
    "lot_size": "1 lô = 100 cổ phiếu. Lệnh lẻ chỉ khớp ATC.",
    "trading_hours": "Phiên sáng 09:00–11:30, phiên chiều 13:00–14:30, ATC 14:30–14:45",
}


def suggest_strategies(
    horizons: dict,
    scenarios: dict,
    latest_close: float,
    daily_compute: dict,
    levels: dict | None = None,
) -> dict:
    """Technical perspective and observation levels for each horizon."""
    if latest_close is None or latest_close <= 0:
        return {"error": "invalid_close_price", "horizons": {}, "disclaimer": _DISCLAIMER}

    short_h = horizons.get("short_term", {})
    mid_h = horizons.get("mid_term", {})
    long_h = horizons.get("long_term", {})
    alignment = horizons.get("alignment") or {}

    return {
        "short_term": _perspective(short_h, latest_close, levels, "ngắn hạn"),
        "mid_term": _perspective(mid_h, latest_close, levels, "trung hạn"),
        "long_term": _perspective(long_h, latest_close, levels, "dài hạn"),
        "horizon_alignment": horizons.get("horizon_alignment", alignment.get("label")),
        "technical_summary": alignment.get("summary")
            or "Chưa đủ dữ liệu để tổng hợp đồng thuận đa khung.",
        "shared_input_caveat": alignment.get("shared_input_caveat"),
        "vn_market_rules": _VN_MARKET_RULES,
        "disclaimer": _DISCLAIMER,
    }


def _nearest(levels: dict | None, side: str) -> dict | None:
    if not isinstance(levels, dict) or is_insufficient(levels):
        return None
    entry = levels.get(f"nearest_{side}")
    return entry if isinstance(entry, dict) else None


def _horizon_ma_levels(horizon: dict, close: float) -> dict[str, list[dict]]:
    """Support/resistance drawn from this horizon's own moving averages."""
    supports: list[dict] = []
    resistances: list[dict] = []
    for name, value in (horizon.get("sma_values") or {}).items():
        level = finite(value)
        if level is None or level <= 0:
            continue
        entry = {
            "level": round(level, 0),
            "basis": name,
            "distance_pct": round(abs(level - close) / close * 100, 2) if close else None,
        }
        (supports if level < close else resistances).append(entry)
    supports.sort(key=lambda item: -item["level"])
    resistances.sort(key=lambda item: item["level"])
    return {"support": supports, "resistance": resistances}


def _perspective(
    horizon: dict, close: float, levels: dict | None, label: str
) -> dict:
    """One horizon's technical state, levels to watch and confirmation triggers."""
    strength = horizon.get("signal_strength", "neutral")
    ma_levels = _horizon_ma_levels(horizon, close)

    # Prefer this horizon's own MA levels; fall back to the report-wide nearest
    # level, which is still measured, just not horizon-specific.
    support = (ma_levels["support"] or [None])[0] or _nearest(levels, "support")
    resistance = (ma_levels["resistance"] or [None])[0] or _nearest(levels, "resistance")

    if "bullish" in strength:
        state = f"Cấu trúc {label} nghiêng tích cực"
    elif "bearish" in strength:
        state = f"Cấu trúc {label} nghiêng tiêu cực"
    elif strength == "insufficient_data":
        state = f"Chưa đủ dữ liệu để đánh giá cấu trúc {label}"
    else:
        state = f"Cấu trúc {label} trung tính, chưa có hướng rõ ràng"

    confirmation = _confirmation(strength, support, resistance)
    risks = _risks(horizon, strength, support, resistance)

    return {
        "horizon": horizon.get("horizon"),
        "label_vi": horizon.get("label_vi"),
        "technical_state": state,
        "signal_strength": strength,
        "confidence": horizon.get("confidence"),
        "confidence_reason": horizon.get("confidence_reason"),
        "support_zone": support or {"level": None, "unavailable_reason":
                                    "không có mức hỗ trợ nào tính được dưới giá hiện tại"},
        "resistance_zone": resistance or {"level": None, "unavailable_reason":
                                         "không có mức kháng cự nào tính được trên giá hiện tại"},
        "levels_to_watch": ma_levels,
        "confirmation_signal": confirmation,
        "risk_factors": risks,
        "invalidation": horizon.get("invalidation"),
        "conflicts": horizon.get("conflicts", []),
    }


def _confirmation(strength: str, support: dict | None, resistance: dict | None) -> str:
    """What would confirm the current reading, in level terms."""
    if "bullish" in strength:
        if resistance:
            return (
                f"Cần đóng cửa vượt {resistance['level']:,.0f} VND ({resistance['basis']}) "
                "kèm khối lượng trên trung bình 20 phiên để xác nhận"
            )
        return "Giá đã trên mọi mức kháng cự tính được; theo dõi khả năng giữ nhịp kèm thanh khoản"
    if "bearish" in strength:
        if resistance:
            return (
                f"Cần lấy lại {resistance['level']:,.0f} VND ({resistance['basis']}) "
                "trên giá đóng cửa để cân bằng lại cấu trúc"
            )
        return "Chưa xác định được mức kháng cự tham chiếu để xác nhận đảo chiều"
    if support and resistance:
        return (
            f"Theo dõi phản ứng giá tại {resistance['level']:,.0f} VND ({resistance['basis']}) "
            f"và {support['level']:,.0f} VND ({support['basis']})"
        )
    return "Chưa đủ mức tham chiếu hai chiều để nêu tín hiệu xác nhận"


def _risks(horizon: dict, strength: str, support: dict | None, resistance: dict | None) -> list[str]:
    """Concrete, numbered risk factors for this horizon."""
    out: list[str] = []

    invalidation = horizon.get("invalidation")
    if invalidation and invalidation.get("condition"):
        out.append(invalidation["condition"])
    elif "bullish" in strength and support:
        out.append(
            f"Rủi ro điều chỉnh nếu đóng cửa dưới {support['level']:,.0f} VND ({support['basis']})"
        )
    elif "bearish" in strength and support:
        out.append(
            f"Rủi ro dò đáy tiếp nếu không giữ được {support['level']:,.0f} VND ({support['basis']})"
        )

    for conflict in horizon.get("conflicts", []):
        out.append(f"Tín hiệu xung đột: {conflict['description']}")

    missing = horizon.get("groups_missing") or []
    if missing:
        out.append("Độ tin cậy bị giới hạn do thiếu dữ liệu: " + ", ".join(missing))

    volatility = horizon.get("volatility") or {}
    annualized = finite(volatility.get("annualized_pct"))
    if annualized is not None:
        stop_distance = finite(volatility.get("suggested_stop_distance_pct"))
        note = (
            f"Biến động close-to-close {annualized:.1f}%/năm (thay thế ATR, không phải ATR)"
        )
        if stop_distance is not None:
            note += f"; khoảng cách kỹ thuật tham chiếu {stop_distance:.2f}%"
        out.append(note)

    return out
