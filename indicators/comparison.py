"""Head-to-head technical comparison of multiple tickers (Pure Analysis).

Produces comparative tables and quantitative summaries across:
1. 52-week price performance & drawdowns
2. Moving average alignments & momentum metrics
3. Support/resistance levels measured from closes (see :mod:`indicators.levels`)
4. Relative technical structure evaluation, covering every compared ticker
"""

from __future__ import annotations

from typing import Any

from indicators import finite, is_insufficient, pick
from indicators.engine import rows_to_frame, _compute_core, prune_series
from indicators.levels import key_levels
from indicators.stats_52w import stats_52w

#: A ticker needs at least this many rows before comparing it means anything.
MIN_ROWS = 10


_COMPARE_DISCLAIMER = (
    "Báo cáo đối chiếu kỹ thuật thuần túy định lượng và khách quan, "
    "TUYỆT ĐỐI KHÔNG cấu thành lời khuyên hoặc khuyến nghị mua/bán cổ phiếu. "
    "Nhà đầu tư tự chịu trách nhiệm với quyết định của mình."
)


def compare_multiple_tickers(
    ticker_data: dict[str, list[dict]],
    window_days: int = 250,
    include_series: bool = False,
    detail: str = "compact",
) -> dict[str, Any]:
    """Compare 2 to 5 tickers head-to-head from raw row dicts.

    Parameters
    ----------
    ticker_data : dict[str, list[dict]]
        Mapping of ticker symbol -> list of stored row dicts.
    window_days : int
        Lookback window in trading sessions (~250 for 1 year).
    detail
        ``"compact"`` (default) omits the full per-ticker indicator dump from
        ``per_ticker_details``, which accounted for roughly four fifths of the
        response while every figure a comparison report needs is already in the
        three tables. ``"full"`` keeps it.
    """
    tickers = list(ticker_data.keys())
    if len(tickers) < 2:
        return {"error": "at_least_two_tickers_required", "tickers": tickers}

    per_ticker: dict[str, Any] = {}
    table_52w: list[dict] = []
    table_ma: list[dict] = []
    table_levels: list[dict] = []
    excluded: list[dict] = []

    for sym in tickers:
        rows = ticker_data[sym]
        if not rows or len(rows) < MIN_ROWS:
            # Reported, not dropped. Silently omitting a ticker the caller asked
            # about leaves it in `tickers` with no row anywhere else, and the
            # consumer has no way to notice.
            excluded.append({
                "ticker": sym,
                "rows_available": len(rows) if rows else 0,
                "rows_required": MIN_ROWS,
                "reason": "insufficient_history",
                "message": (
                    f"{sym}: chỉ có {len(rows) if rows else 0} phiên dữ liệu "
                    f"(cần tối thiểu {MIN_ROWS}), không thể đưa vào so sánh."
                ),
            })
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

        # 3. Support / resistance levels measured from closes, MAs and bands
        trend = ind.get("groups", {}).get("trend", {})
        mom = ind.get("groups", {}).get("momentum", {})
        lv = key_levels(frame, trend, ind.get("groups", {}).get("volatility"))

        def _get_dist(ma_key: str) -> float | None:
            obj = trend.get(ma_key, {})
            if isinstance(obj, dict) and not is_insufficient(obj):
                return obj.get("distance_pct")
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

        # Build MA & Momentum table row.
        # RSI is resolved through `pick` because the momentum group keys it by
        # window (`rsi_14`). Reading a literal "rsi" returned None for every
        # ticker, so this column was always empty and the RSI comparison
        # observation below could never fire.
        rsi_entry = pick(mom, "rsi")
        rsi_val = (
            rsi_entry.get("latest")
            if isinstance(rsi_entry, dict) and not is_insufficient(rsi_entry)
            else None
        )
        rsi_zone = (
            rsi_entry.get("zone")
            if isinstance(rsi_entry, dict) and not is_insufficient(rsi_entry)
            else None
        )
        macd_obj = trend.get("macd", {})
        macd_lat = (
            macd_obj.get("latest", {})
            if isinstance(macd_obj, dict) and not is_insufficient(macd_obj)
            else {}
        )

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
            "rsi_zone": rsi_zone,
            "macd": round(macd_lat.get("macd", 0), 2) if macd_lat.get("macd") is not None else None,
            "macd_signal": round(macd_lat.get("signal", 0), 2) if macd_lat.get("signal") is not None else None,
            "macd_hist": round(macd_lat.get("histogram", 0), 2) if macd_lat.get("histogram") is not None else None,
            # MACD is in VND, so its magnitude scales with the share price and is
            # not comparable across tickers. This normalized figure is.
            "macd_hist_pct_of_close": (
                round(macd_lat["histogram"] / latest_c * 100, 3)
                if macd_lat.get("histogram") is not None and latest_c else None
            ),
            "trend_alignment": ind.get("trend_alignment"),
        })

        # Build levels table row
        nearest_s = lv.get("nearest_support") if isinstance(lv, dict) else None
        nearest_r = lv.get("nearest_resistance") if isinstance(lv, dict) else None
        position = lv.get("position", {}) if isinstance(lv, dict) else {}
        table_levels.append({
            "ticker": sym,
            "close": latest_c,
            "nearest_support": nearest_s["level"] if nearest_s else None,
            "nearest_support_basis": nearest_s["basis"] if nearest_s else None,
            "nearest_support_distance_pct": nearest_s.get("distance_pct") if nearest_s else None,
            "nearest_resistance": nearest_r["level"] if nearest_r else None,
            "nearest_resistance_basis": nearest_r["basis"] if nearest_r else None,
            "nearest_resistance_distance_pct": nearest_r.get("distance_pct") if nearest_r else None,
            "position": position.get("reading"),
            "position_desc": position.get("description"),
            "pct_in_band": position.get("pct_in_band"),
        })

        per_ticker[sym] = {
            "stats_52w": s52,
            "levels": lv,
            "trend_alignment": ind.get("trend_alignment"),
        }
        if detail == "full":
            per_ticker[sym]["indicators"] = ind.get("groups")

    # Relative structure assessment across every compared ticker
    relative_eval = _assess_relative_strength(table_52w, table_ma, table_levels)

    result = {
        "tickers": tickers,
        "tickers_compared": [row["ticker"] for row in table_52w],
        "tickers_excluded": excluded,
        "window_days": window_days,
        "table_52w": table_52w,
        "table_ma": table_ma,
        "table_levels": table_levels,
        "relative_assessment": relative_eval,
        "per_ticker_details": per_ticker,
        "detail": detail,
        "levels_note": (
            "Mọi mốc hỗ trợ/kháng cự tính trên GIÁ ĐÓNG CỬA (feed không có high/low "
            "trong phiên) — không phải pivot theo đỉnh/đáy nến."
        ),
    }
    if not include_series:
        result = prune_series(result)
    return result


#: A gap smaller than this is noise, not a difference worth asserting.
#: Without these, a 23.9% vs 23.5% drawdown produced a confident claim that one
#: ticker was materially more volatile than the other.
_MATERIAL_GAP = {
    "return_pct": 3.0,          # percentage points of 1-year return
    "max_drawdown_pct": 3.0,    # percentage points of drawdown
    "pct_from_low": 5.0,        # percentage points of recovery off the low
    "vs_sma200_pct": 2.0,       # percentage points of distance from SMA200
    "rsi": 5.0,                 # RSI points
    "liquidity_ratio": 1.3,     # multiple of average turnover
}


def _rank_observation(
    rows: list[dict],
    field: str,
    heading: str,
    unit: str,
    *,
    higher_is: str,
    threshold: float,
    absolute: bool = False,
    decimals: int = 2,
) -> str | None:
    """Rank every ticker on one metric, asserting a difference only if material.

    Covers all N tickers rather than the first two: an earlier version indexed
    ``[0]`` and ``[1]`` only, so a three-way comparison produced a conclusion
    about two of the three and silently ignored the rest.
    """
    scored = []
    for row in rows:
        value = finite(row.get(field))
        if value is None:
            continue
        scored.append((row["ticker"], abs(value) if absolute else value))
    if len(scored) < 2:
        return None

    scored.sort(key=lambda item: -item[1])
    spread = scored[0][1] - scored[-1][1]
    listing = ", ".join(f"{name} {value:+.{decimals}f}{unit}" for name, value in scored)
    if absolute:
        listing = ", ".join(f"{name} {value:.{decimals}f}{unit}" for name, value in scored)

    if spread < threshold:
        return (
            f"{heading}: các mã gần như tương đương ({listing}) — "
            f"chênh lệch {spread:.{decimals}f}{unit} chưa đủ để coi là khác biệt."
        )
    best, worst = scored[0][0], scored[-1][0]
    return (
        f"{heading}: {best} {higher_is} nhất, {worst} thấp nhất ({listing})."
    )


def _assess_relative_strength(
    table_52w: list[dict],
    table_ma: list[dict],
    table_levels: list[dict],
) -> dict:
    """Head-to-head observations across every compared ticker."""
    observations: list[str] = []
    if len(table_52w) < 2:
        return {
            "observations": [],
            "summary": "Cần ít nhất 2 mã có đủ dữ liệu để so sánh.",
            "tickers_assessed": [row["ticker"] for row in table_52w],
        }

    ma_by_ticker = {row["ticker"]: row for row in table_ma}
    levels_by_ticker = {row["ticker"]: row for row in table_levels}

    # 1. One-year performance and risk
    for spec in (
        ("return_pct", "Hiệu suất ~250 phiên", "%", "cao", False, 2),
        ("max_drawdown_pct", "Mức sụt giảm cực đại", "%", "sâu", True, 1),
        ("pct_from_low", "Mức phục hồi từ đáy", "%", "mạnh", False, 1),
    ):
        field, heading, unit, higher_is, absolute, decimals = spec
        note = _rank_observation(
            table_52w, field, heading, unit,
            higher_is=higher_is, threshold=_MATERIAL_GAP[field],
            absolute=absolute, decimals=decimals,
        )
        if note:
            observations.append(note)

    # 2. Trend structure: who is above which moving averages
    for label, field in (("SMA20", "vs_sma20_pct"), ("SMA50", "vs_sma50_pct"), ("SMA200", "vs_sma200_pct")):
        above = [r["ticker"] for r in table_ma if finite(r.get(field)) is not None and r[field] > 0]
        below = [r["ticker"] for r in table_ma if finite(r.get(field)) is not None and r[field] <= 0]
        if above and below:
            detail = ", ".join(
                f"{r['ticker']} {r[field]:+.2f}%"
                for r in table_ma if finite(r.get(field)) is not None
            )
            observations.append(
                f"Vị thế so với {label}: {', '.join(above)} đứng trên, "
                f"{', '.join(below)} nằm dưới ({detail})."
            )

    note = _rank_observation(
        table_ma, "vs_sma200_pct", "Kênh giá dài hạn (khoảng cách SMA200)", "%",
        higher_is="tích cực", threshold=_MATERIAL_GAP["vs_sma200_pct"],
    )
    if note:
        observations.append(note)

    # 3. Momentum
    note = _rank_observation(
        table_ma, "rsi", "Xung lực RSI(14)", "",
        higher_is="cao", threshold=_MATERIAL_GAP["rsi"], absolute=True, decimals=1,
    )
    if note:
        observations.append(note)

    positive = [r["ticker"] for r in table_ma if finite(r.get("macd_hist")) is not None and r["macd_hist"] > 0]
    negative = [r["ticker"] for r in table_ma if finite(r.get("macd_hist")) is not None and r["macd_hist"] <= 0]
    if positive and negative:
        detail = ", ".join(
            f"{r['ticker']} {r['macd_hist_pct_of_close']:+.3f}% giá"
            for r in table_ma if finite(r.get("macd_hist_pct_of_close")) is not None
        )
        observations.append(
            f"Động lượng MACD: histogram dương ở {', '.join(positive)}, âm ở {', '.join(negative)}"
            + (f" (chuẩn hóa theo giá: {detail})." if detail else ".")
        )

    # 4. Position between measured support and resistance
    for row in table_levels:
        if row.get("position_desc"):
            observations.append(f"Vị thế hỗ trợ/kháng cự {row['ticker']}: {row['position_desc']}")

    # 5. Liquidity
    turnovers = [
        (row["ticker"], finite(row.get("avg_value_bil")))
        for row in table_52w
    ]
    turnovers = [(name, value) for name, value in turnovers if value and value > 0]
    if len(turnovers) >= 2:
        turnovers.sort(key=lambda item: -item[1])
        ratio = turnovers[0][1] / turnovers[-1][1]
        listing = ", ".join(f"{name} {value:.1f} tỷ" for name, value in turnovers)
        if ratio >= _MATERIAL_GAP["liquidity_ratio"]:
            observations.append(
                f"Quy mô thanh khoản: {turnovers[0][0]} cao gấp {ratio:.1f}x "
                f"{turnovers[-1][0]} ({listing} VND/phiên)."
            )
        else:
            observations.append(
                f"Quy mô thanh khoản: các mã tương đương ({listing} VND/phiên)."
            )

    return {
        "observations": observations,
        "summary": " ".join(observations),
        "tickers_assessed": [row["ticker"] for row in table_52w],
        "material_gap_thresholds": _MATERIAL_GAP,
    }
