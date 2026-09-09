"""Technical perspectives and observation levels per horizon (Pure Analysis).

Provides objective technical observations and key price levels for
short-term, mid-term, and long-term horizons without any investment or
buy/sell advice.

Strictly follows:
- Pure technical analysis & quantitative assessment
- NO BUY/SELL RECOMMENDATIONS
- Key support/resistance levels & confirmation triggers
"""

from __future__ import annotations

from typing import Any


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
) -> dict:
    """Provide technical perspectives and observation levels for each horizon.

    Parameters
    ----------
    horizons : dict
        Output of horizon_analysis.
    scenarios : dict
        Output of generate_scenarios.
    latest_close : float
        Most recent closing price.
    daily_compute : dict
        Daily indicator output.
    """
    if latest_close is None or latest_close <= 0:
        return {"error": "invalid_close_price", "horizons": {}, "disclaimer": _DISCLAIMER}

    short_h = horizons.get("short_term", {})
    mid_h = horizons.get("mid_term", {})
    long_h = horizons.get("long_term", {})
    alignment = horizons.get("horizon_alignment", "mixed_signals")

    vol = short_h.get("volatility") or mid_h.get("volatility")
    scenario_list = scenarios.get("scenarios", [])

    return {
        "short_term": _short_perspective(short_h, latest_close, vol, scenario_list),
        "mid_term": _mid_perspective(mid_h, latest_close, vol, scenario_list),
        "long_term": _long_perspective(long_h, latest_close, vol, scenario_list),
        "horizon_alignment": alignment,
        "technical_summary": _technical_summary(alignment),
        "vn_market_rules": _VN_MARKET_RULES,
        "disclaimer": _DISCLAIMER,
    }


def _vol_distance(close: float, vol_pct: float | None, sigma: float = 2.0) -> float:
    if vol_pct is None or vol_pct <= 0:
        return close * 0.05
    return close * (vol_pct / 100) * sigma


def _nearest_support(horizon: dict) -> dict | None:
    levels = horizon.get("key_levels", {}).get("support", [])
    return levels[0] if levels else None


def _nearest_resistance(horizon: dict) -> dict | None:
    levels = horizon.get("key_levels", {}).get("resistance", [])
    return levels[0] if levels else None


def _short_perspective(
    horizon: dict, close: float, vol: float | None, scenarios: list[dict],
) -> dict:
    strength = horizon.get("signal_strength", "neutral")
    support = _nearest_support(horizon)
    resistance = _nearest_resistance(horizon)

    s_level = support["level"] if support else round(close - _vol_distance(close, vol, 1.0), 0)
    r_level = resistance["level"] if resistance else round(close * 1.05, 0)
    vol_floor = round(close - _vol_distance(close, vol, 2.0), 0)

    if "bullish" in strength:
        state = "Phục hồi ngắn hạn, giữ trên các ngưỡng hỗ trợ gần"
        conf = f"Cần duy trì trên vùng hỗ trợ {s_level:,.0f} VND và kiểm định mốc cản {r_level:,.0f} VND"
        risk = f"Áp lực điều chỉnh gia tăng nếu giá đóng cửa xuyên thủng ngưỡng {vol_floor:,.0f} VND"
    elif "bearish" in strength:
        state = "Điều chỉnh ngắn hạn, chịu áp lực bán"
        conf = f"Cần tín hiệu hấp thụ cung và đóng cửa vượt lại mốc cản {r_level:,.0f} VND kèm khối lượng"
        risk = f"Rủi ro tiếp tục dò đáy nếu không giữ được mốc hỗ trợ {s_level:,.0f} VND"
    else:
        state = "Đi ngang tích lũy trong biên độ hẹp"
        conf = f"Theo dõi phản ứng giá tại vùng cận trên {r_level:,.0f} VND và cận dưới {s_level:,.0f} VND"
        risk = f"Biến động có thể mở rộng bất ngờ nếu dải Bollinger Bands bung nén"

    return {
        "horizon": "short_term",
        "technical_state": state,
        "support_zone": {"level": s_level, "volatility_floor": vol_floor},
        "resistance_zone": {"level": r_level},
        "confirmation_signal": conf,
        "risk_factors": risk,
        "signal_strength": strength,
    }


def _mid_perspective(
    horizon: dict, close: float, vol: float | None, scenarios: list[dict],
) -> dict:
    strength = horizon.get("signal_strength", "neutral")
    sma_values = horizon.get("sma_values", {})
    sma50 = sma_values.get("SMA50")
    sma200 = sma_values.get("SMA200")
    weekly_confirm = horizon.get("weekly_confirmation") or {}

    mid_support = sma50 if sma50 and sma50 < close else (sma200 if sma200 and sma200 < close else round(close * 0.95, 0))
    mid_resistance = sma50 if sma50 and sma50 > close else (sma200 if sma200 and sma200 > close else round(close * 1.08, 0))

    if "bullish" in strength:
        state = "Xu hướng trung hạn tích cực, cấu trúc MA hướng lên"
        conf = f"Xu hướng trung hạn duy trì khi giá vận động trên SMA50 ({mid_support:,.0f} VND)"
        risk = "Tín hiệu suy yếu nếu xuất hiện giao cắt tử thần (death cross) hoặc RSI trung hạn rơi dưới 40"
    elif "bearish" in strength:
        state = "Xu hướng trung hạn tiêu cực, giá dưới các đường trung bình lớn"
        conf = f"Cần lấy lại mốc SMA50/SMA200 quanh {mid_resistance:,.0f} VND để cân bằng lại xu hướng"
        risk = f"Áp lực bán trung hạn tiếp tục duy trì nếu dòng vốn ngoại tiếp tục rút ròng"
    else:
        state = "Trạng thái trung hạn giằng co, chờ đợi tín hiệu bứt phá"
        conf = f"Cần một phiên bứt phá khỏi vùng cản {mid_resistance:,.0f} VND kèm thanh khoản vượt mức bình quân"
        risk = f"Rủi ro trượt dốc nếu đánh mất ngưỡng đỡ {mid_support:,.0f} VND"

    return {
        "horizon": "mid_term",
        "technical_state": state,
        "support_zone": {"level": round(mid_support, 0)},
        "resistance_zone": {"level": round(mid_resistance, 0)},
        "confirmation_signal": conf,
        "risk_factors": risk,
        "weekly_context": weekly_confirm,
        "signal_strength": strength,
    }


def _long_perspective(
    horizon: dict, close: float, vol: float | None, scenarios: list[dict],
) -> dict:
    strength = horizon.get("signal_strength", "neutral")
    sma200 = horizon.get("daily_sma200")
    w_trend = horizon.get("weekly_trend_alignment")
    w_rsi = horizon.get("weekly_rsi")

    if sma200:
        pos_str = "trên SMA200 (kênh giá dài hạn giữ vững)" if close > sma200 else "dưới SMA200 (kênh giá dài hạn chịu áp lực)"
    else:
        pos_str = "chưa đủ dữ liệu 200 phiên"

    state = f"Cấu trúc dài hạn: giá đang nằm {pos_str}"
    conf = f"Cần quan sát cấu trúc nến tuần và chỉ báo RSI tuần ({w_rsi or 'N/A'})"
    risk = "Rủi ro phân kỳ âm dài hạn hoặc dòng vốn ngoại bán ròng kéo dài"

    return {
        "horizon": "long_term",
        "technical_state": state,
        "sma200_level": sma200,
        "weekly_trend": w_trend,
        "confirmation_signal": conf,
        "risk_factors": risk,
        "signal_strength": strength,
    }


def _technical_summary(alignment: str) -> str:
    if alignment == "all_bullish":
        return "Cả ba tầm nhìn ngắn, trung và dài hạn đều đồng thuận tích cực."
    if alignment == "all_bearish":
        return "Cả ba tầm nhìn đều chịu áp lực tiêu cực, rủi ro điều chỉnh kỹ thuật ở mức cao."
    return "Tín hiệu kỹ thuật giữa các khung thời gian có sự phân hóa, cần theo dõi các mốc kiểm định."
