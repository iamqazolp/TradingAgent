"""Head-to-head technical comparison of multiple tickers (Pure Analysis).

Produces comparative tables and quantitative summaries across:
1. 52-week price performance & drawdowns
2. Moving average alignments & momentum metrics
3. Classic Pivot Points and key support/resistance levels
4. Relative technical structure evaluation
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from indicators.engine import rows_to_frame, _compute_core, prune_series
from indicators.pivots import classic_pivots
from indicators.stats_52w import stats_52w


_COMPARE_DISCLAIMER = (
    "Báo cáo đối chiếu kỹ thuật thuần túy định lượng và khách quan, "
    "TUYỆT ĐỐI KHÔNG cấu thành lời khuyên hoặc khuyến nghị mua/bán cổ phiếu. "
    "Nhà đầu tư tự chịu trách nhiệm với quyết định của mình."
)


def compare_multiple_tickers(
    ticker_data: dict[str, list[dict]],
    window_days: int = 250,
    include_series: bool = False,
) -> dict[str, Any]:
    """Compare 2 to 5 tickers head-to-head from raw row dicts.

    Parameters
    ----------
    ticker_data : dict[str, list[dict]]
        Mapping of ticker symbol -> list of stored row dicts.
    window_days : int
        Lookback window in trading sessions (~250 for 1 year).
    """
    tickers = list(ticker_data.keys())
    if len(tickers) < 2:
        return {"error": "at_least_two_tickers_required", "tickers": tickers}

    per_ticker: dict[str, Any] = {}
    table_52w: list[dict] = []
    table_ma: list[dict] = []
    table_pivots: list[dict] = []

    for sym in tickers:
        rows = ticker_data[sym]
        if not rows or len(rows) < 10:
            continue
        frame = rows_to_frame(rows)
        latest_c = float(frame["close"].iloc[-1])

        # 1. 52w stats
        s52 = stats_52w(frame, window=window_days)

        # 2. Indicators (trend, momentum, volume_flow, foreign_flow)
        ind = _compute_core(
            frame,
            ("trend", "momentum", "volume_flow", "foreign_flow", "volatility"),
            series_tail=10,
        )

        # 3. Pivots
        piv = classic_pivots(frame)

        # Extract MA distances
        trend = ind.get("groups", {}).get("trend", {})
        mom = ind.get("groups", {}).get("momentum", {})
        vf = ind.get("groups", {}).get("volume_flow", {})
        ff = ind.get("groups", {}).get("foreign_flow", {})

        def _get_dist(ma_key: str) -> float | None:
            obj = trend.get(ma_key, {})
            if isinstance(obj, dict):
                return obj.get("distance_pct")
            return None

        def _get_val(ma_key: str) -> float | None:
            obj = trend.get(ma_key, {})
            if isinstance(obj, dict):
                return obj.get("latest")
            return None

        # Build 52w table row
        h52 = s52.get("high_52w", {})
        l52 = s52.get("low_52w", {})
        table_52w.append({
            "ticker": sym,
            "latest_close": latest_c,
            "return_pct": s52.get("return_pct"),
            "high_52w": h52.get("price"),
            "high_date": h52.get("date"),
            "pct_from_high": h52.get("pct_from_current"),
            "low_52w": l52.get("price"),
            "low_date": l52.get("date"),
            "pct_from_low": l52.get("pct_from_current"),
            "max_drawdown_pct": s52.get("max_drawdown_pct"),
            "avg_volume_mil": s52.get("avg_daily_volume_mil"),
            "avg_value_bil": s52.get("avg_daily_value_bil"),
        })

        # Build MA & Momentum table row
        rsi_val = mom.get("rsi", {}).get("latest") if isinstance(mom.get("rsi"), dict) else None
        macd_obj = trend.get("macd", {})
        macd_lat = macd_obj.get("latest", {}) if isinstance(macd_obj, dict) else {}

        table_ma.append({
            "ticker": sym,
            "close": latest_c,
            "vs_sma20_pct": _get_dist("sma_20"),
            "vs_sma50_pct": _get_dist("sma_50"),
            "vs_sma100_pct": _get_dist("sma_100"),
            "vs_sma200_pct": _get_dist("sma_200"),
            "vs_ema20_pct": _get_dist("ema_20"),
            "vs_ema50_pct": _get_dist("ema_50"),
            "vs_ema200_pct": _get_dist("ema_200"),
            "rsi": round(rsi_val, 2) if rsi_val is not None else None,
            "macd": round(macd_lat.get("macd", 0), 2) if macd_lat.get("macd") is not None else None,
            "macd_signal": round(macd_lat.get("signal", 0), 2) if macd_lat.get("signal") is not None else None,
            "macd_hist": round(macd_lat.get("histogram", 0), 2) if macd_lat.get("histogram") is not None else None,
        })

        # Build Pivot table row
        p_classic = piv.get("classic", {})
        table_pivots.append({
            "ticker": sym,
            "close": latest_c,
            "R2": p_classic.get("R2"),
            "R1": p_classic.get("R1"),
            "PP": p_classic.get("PP"),
            "S1": p_classic.get("S1"),
            "S2": p_classic.get("S2"),
            "S3": p_classic.get("S3"),
            "position": piv.get("position"),
            "position_desc": piv.get("position_description"),
        })

        per_ticker[sym] = {
            "stats_52w": s52,
            "pivots": piv,
            "trend_alignment": ind.get("trend_alignment"),
            "indicators": ind.get("groups"),
        }

    # Relative structure assessment
    relative_eval = _assess_relative_strength(table_52w, table_ma, table_pivots)

    result = {
        "tickers": tickers,
        "window_days": window_days,
        "table_52w": table_52w,
        "table_ma": table_ma,
        "table_pivots": table_pivots,
        "relative_assessment": relative_eval,
        "per_ticker_details": per_ticker,
        "disclaimer": _COMPARE_DISCLAIMER,
    }
    if not include_series:
        result = prune_series(result)
    return result


def _assess_relative_strength(
    table_52w: list[dict],
    table_ma: list[dict],
    table_pivots: list[dict],
) -> dict:
    observations = []
    if len(table_52w) >= 2:
        t1 = table_52w[0]
        t2 = table_52w[1]
        m1 = table_ma[0] if len(table_ma) > 0 else {}
        m2 = table_ma[1] if len(table_ma) > 1 else {}
        p1 = table_pivots[0] if len(table_pivots) > 0 else {}
        p2 = table_pivots[1] if len(table_pivots) > 1 else {}

        # 1. 52w Performance & Drawdown
        ret1 = t1.get("return_pct") or 0.0
        ret2 = t2.get("return_pct") or 0.0
        if ret1 != ret2:
            better_ret = t1['ticker'] if ret1 > ret2 else t2['ticker']
            worse_ret = t2['ticker'] if ret1 > ret2 else t1['ticker']
            observations.append(
                f"Hiệu suất 52 tuần: {better_ret} vượt trội hơn {worse_ret} ({max(ret1, ret2):+.2f}% vs {min(ret1, ret2):+.2f}%)."
            )

        dd1 = abs(t1.get("max_drawdown_pct") or 0)
        dd2 = abs(t2.get("max_drawdown_pct") or 0)
        if dd1 != dd2:
            higher_dd = t1['ticker'] if dd1 > dd2 else t2['ticker']
            lower_dd = t2['ticker'] if dd1 > dd2 else t1['ticker']
            observations.append(
                f"Biên độ điều chỉnh: {higher_dd} có mức sụt giảm cực đại sâu hơn {lower_dd} ({max(dd1, dd2):.1f}% vs {min(dd1, dd2):.1f}%), thể hiện độ nhạy biến động cao hơn."
            )

        # 2. Recovery from 52w Low
        rec1 = t1.get("pct_from_low") or 0
        rec2 = t2.get("pct_from_low") or 0
        if rec1 != rec2:
            stronger_rec = t1['ticker'] if rec1 > rec2 else t2['ticker']
            weaker_rec = t2['ticker'] if rec1 > rec2 else t1['ticker']
            observations.append(
                f"Khả năng phục hồi sau đáy: {stronger_rec} phục hồi mạnh hơn {weaker_rec} ({max(rec1, rec2):+.1f}% vs {min(rec1, rec2):+.1f}%)."
            )

        # 3. MA Alignments & Positions
        s20_1, s20_2 = m1.get("vs_sma20_pct"), m2.get("vs_sma20_pct")
        s50_1, s50_2 = m1.get("vs_sma50_pct"), m2.get("vs_sma50_pct")
        s200_1, s200_2 = m1.get("vs_sma200_pct"), m2.get("vs_sma200_pct")

        if s20_1 is not None and s20_2 is not None:
            if s20_1 > 0 and s20_2 <= 0:
                observations.append(f"Ngắn hạn: {t1['ticker']} giữ được trên SMA20 ({s20_1:+.1f}%), trong khi {t2['ticker']} nằm dưới SMA20 ({s20_2:+.1f}%).")
            elif s20_2 > 0 and s20_1 <= 0:
                observations.append(f"Ngắn hạn: {t2['ticker']} giữ được trên SMA20 ({s20_2:+.1f}%), trong khi {t1['ticker']} nằm dưới SMA20 ({s20_1:+.1f}%).")

        if s200_1 is not None and s200_2 is not None:
            better_200 = t1['ticker'] if s200_1 > s200_2 else t2['ticker']
            worse_200 = t2['ticker'] if s200_1 > s200_2 else t1['ticker']
            b_val = max(s200_1, s200_2)
            w_val = min(s200_1, s200_2)
            observations.append(
                f"Kênh giá dài hạn: {better_200} duy trì cấu trúc vượt trội hơn {worse_200} so với SMA200 ({b_val:+.1f}% vs {w_val:+.1f}%)."
            )

        # 4. Momentum (RSI & MACD)
        rsi1, rsi2 = m1.get("rsi"), m2.get("rsi")
        if rsi1 is not None and rsi2 is not None:
            diff_rsi = abs(rsi1 - rsi2)
            if diff_rsi >= 3.0:
                higher_rsi = t1['ticker'] if rsi1 > rsi2 else t2['ticker']
                lower_rsi = t2['ticker'] if rsi1 > rsi2 else t1['ticker']
                observations.append(f"Xung lực RSI(14): {higher_rsi} nhỉnh hơn {lower_rsi} ({max(rsi1, rsi2):.1f} vs {min(rsi1, rsi2):.1f}).")

        hist1, hist2 = m1.get("macd_hist"), m2.get("macd_hist")
        if hist1 is not None and hist2 is not None:
            if hist1 > 0 >= hist2:
                observations.append(f"Động lượng MACD: Histogram {t1['ticker']} dương ({hist1:+.1f}), trong khi {t2['ticker']} đang chịu xung lực âm ({hist2:+.1f}).")
            elif hist2 > 0 >= hist1:
                observations.append(f"Động lượng MACD: Histogram {t2['ticker']} dương ({hist2:+.1f}), trong khi {t1['ticker']} đang chịu xung lực âm ({hist1:+.1f}).")

        # 5. Pivot Points positioning
        pos1, pos2 = p1.get("position_desc"), p2.get("position_desc")
        if pos1 and pos2:
            observations.append(f"Vị thế Pivot: {t1['ticker']} {pos1.lower()}; {t2['ticker']} {pos2.lower()}.")

        # 6. Liquidity (Turnover)
        val1 = t1.get("avg_value_bil") or 0.0
        val2 = t2.get("avg_value_bil") or 0.0
        if val1 > 0 and val2 > 0:
            high_val_t = t1['ticker'] if val1 > val2 else t2['ticker']
            low_val_t = t2['ticker'] if val1 > val2 else t1['ticker']
            ratio = max(val1, val2) / min(val1, val2) if min(val1, val2) > 0 else 1.0
            if ratio >= 1.3:
                observations.append(f"Quy mô thanh khoản: {high_val_t} có giá trị khớp lệnh bình quân cao gấp {ratio:.1f}x so với {low_val_t} ({max(val1, val2):.1f} tỷ vs {min(val1, val2):.1f} tỷ VND/phiên).")

    return {
        "observations": observations,
        "summary": " ".join(observations),
    }
