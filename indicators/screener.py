"""Smart Screener, Intraday 1H, and Foreign Flow Scanner.

Provides screening, scoring (0-100), and ranking across stock universes (e.g. VN30)
based on quantitative technical strategies:
1. momentum_breakout
2. oversold_reversal
3. foreign_accumulation
4. intraday_breakout

Also provides foreign money flow scanning across the universe over rolling windows.
"""

from __future__ import annotations

from typing import Any
import pandas as pd
import numpy as np

from indicators import finite, insufficient, is_insufficient, latest, pick, safe_div
from indicators.momentum import rsi
from indicators.trend import sma, macd
from indicators.volume_flow import volume_ratio

#: Standard 30 tickers in VN30 Index
VN30_TICKERS: list[str] = [
    "ACB", "BCM", "BID", "BVH", "CTG", "FPT", "GAS", "GVR", "HDB", "HPG",
    "MBB", "MSN", "MWG", "PLX", "POW", "SAB", "SHB", "SSB", "SSI", "STB",
    "TCB", "TPB", "VCB", "VHM", "VIB", "VIC", "VJC", "VNM", "VPB", "VRE",
]

MIN_ROWS_REQUIRED = {
    "momentum_breakout": 20,
    "oversold_reversal": 15,
    "foreign_accumulation": 5,
    "intraday_breakout": 2,
}


def normalize_universe(universe: list[str] | str) -> list[str]:
    """Normalize user-supplied universe into a list of uppercase ticker symbols."""
    if isinstance(universe, str):
        u_str = universe.strip()
        if u_str.lower() == "vn30":
            return list(VN30_TICKERS)
        cleaned = u_str.strip("[]()").replace('"', "").replace("'", "")
        return [t.strip().upper() for t in cleaned.split(",") if t.strip()]
    if isinstance(universe, (list, tuple)):
        return [str(t).strip().upper() for t in universe if str(t).strip()]
    return list(VN30_TICKERS)


def _rows_to_df(rows: list[dict]) -> pd.DataFrame:
    """Safely convert rows (daily or hourly) into a clean pandas DataFrame."""
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    numeric_cols = (
        "close", "open", "high", "low", "prev_close", "volume",
        "total_volume", "total_value", "value",
        "foreign_buy_value", "foreign_sell_value",
        "foreign_buy_volume", "foreign_sell_volume", "foreign_room",
    )
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # Reconcile volume & value between daily and hourly conventions
    if "total_volume" not in df.columns and "volume" in df.columns:
        df["total_volume"] = df["volume"]
    elif "volume" not in df.columns and "total_volume" in df.columns:
        df["volume"] = df["total_volume"]

    if "total_value" not in df.columns and "value" in df.columns:
        df["total_value"] = df["value"]
    elif "value" not in df.columns and "total_value" in df.columns:
        df["value"] = df["total_value"]

    return df


def _score_momentum_breakout(frame: pd.DataFrame) -> tuple[float, list[str], dict[str, Any]]:
    """Score candidate on momentum breakout strategy (0-100).

    Combines:
    - Price relative to SMA20 (25 pts)
    - RSI sweet spot 50-70 (25 pts)
    - Volume spike > 1.2x 20d avg (25 pts)
    - MACD histogram > 0 or bullish crossover (25 pts)
    """
    close = frame["close"]
    latest_close = float(close.iloc[-1])
    key_signals: list[str] = []
    highlights: dict[str, Any] = {}

    # 1. Price vs SMA20 (25 pts)
    sma_score = 0.0
    sma20_res = sma(close, 20)
    sma20_val = None
    dist_pct = None
    if not is_insufficient(sma20_res):
        sma20_val = sma20_res.get("latest")
        if sma20_val is not None and sma20_val > 0:
            dist_pct = ((latest_close - sma20_val) / sma20_val) * 100
            if dist_pct > 0:
                sma_score = min(25.0, 20.0 + min(5.0, dist_pct))
                key_signals.append(f"Giá nằm trên SMA20 (+{dist_pct:.1f}%)")
            else:
                sma_score = 0.0
    highlights["sma20"] = sma20_val
    highlights["distance_to_sma20_pct"] = round(dist_pct, 2) if dist_pct is not None else None

    # 2. RSI sweet spot 50-70 (25 pts)
    rsi_score = 0.0
    rsi_res = rsi(close, 14)
    rsi_val = None
    if not is_insufficient(rsi_res):
        rsi_val = rsi_res.get("latest")
        if rsi_val is not None:
            if 50.0 <= rsi_val <= 70.0:
                rsi_score = 25.0
                key_signals.append(f"RSI vùng đà tăng tối ưu ({rsi_val:.1f})")
            elif 70.0 < rsi_val <= 80.0:
                rsi_score = 15.0
                key_signals.append(f"RSI mạnh tiến sát vùng quá mua ({rsi_val:.1f})")
            elif 45.0 <= rsi_val < 50.0:
                rsi_score = 10.0
            else:
                rsi_score = 0.0
    highlights["rsi_14"] = round(rsi_val, 2) if rsi_val is not None else None

    # 3. Volume spike > 1.2x 20d avg (25 pts)
    vol_score = 0.0
    ratio_val = None
    if len(frame) >= 21:
        vol_res = volume_ratio(frame, 20)
        if not is_insufficient(vol_res):
            ratio_val = vol_res.get("latest")
            if ratio_val is not None:
                if ratio_val >= 1.5:
                    vol_score = 25.0
                    key_signals.append(f"Khối lượng bùng nổ {ratio_val:.2f}x TB20 phiên")
                elif ratio_val >= 1.2:
                    vol_score = 20.0
                    key_signals.append(f"Khối lượng gia tăng {ratio_val:.2f}x TB20 phiên")
                elif ratio_val >= 1.0:
                    vol_score = 10.0
                else:
                    vol_score = 0.0
    highlights["volume_ratio_20"] = round(ratio_val, 2) if ratio_val is not None else None

    # 4. MACD histogram > 0 or crossover (25 pts)
    macd_score = 0.0
    crossover = "none"
    hist = None
    if len(close) >= 34:
        macd_res = macd(close, 12, 26, 9)
        if not is_insufficient(macd_res):
            crossover = macd_res.get("crossover", "none")
            hist = macd_res.get("latest", {}).get("histogram")
            macd_val = macd_res.get("latest", {}).get("macd")
            sig_val = macd_res.get("latest", {}).get("signal")
            if crossover == "bullish_cross":
                macd_score = 25.0
                key_signals.append("MACD cắt lên đường tín hiệu (Bullish Cross)")
            elif hist is not None and hist > 0:
                macd_score = 20.0
                key_signals.append(f"MACD histogram dương (+{hist:.2f})")
            elif macd_val is not None and sig_val is not None and macd_val > sig_val:
                macd_score = 15.0
            else:
                macd_score = 0.0
    elif len(close) >= 20:
        macd_res = macd(close, 8, 17, 9)
        if not is_insufficient(macd_res):
            crossover = macd_res.get("crossover", "none")
            hist = macd_res.get("latest", {}).get("histogram")
            if crossover == "bullish_cross" or (hist is not None and hist > 0):
                macd_score = 20.0
                key_signals.append("MACD phân kỳ dương")

    highlights["macd_histogram"] = round(hist, 2) if hist is not None else None
    highlights["macd_crossover"] = crossover

    composite_score = round(sma_score + rsi_score + vol_score + macd_score, 1)
    return composite_score, key_signals, highlights


def _score_oversold_reversal(frame: pd.DataFrame) -> tuple[float, list[str], dict[str, Any]]:
    """Score candidate on oversold reversal strategy (0-100).

    Combines:
    - RSI < 35 (35 pts)
    - Support proximity (35 pts)
    - Bullish candlestick patterns: hammer, engulfing, doji (30 pts)
    """
    close = frame["close"]
    latest_close = float(close.iloc[-1])
    key_signals: list[str] = []
    highlights: dict[str, Any] = {}

    # 1. RSI < 35 (35 pts)
    rsi_score = 0.0
    rsi_res = rsi(close, 14)
    rsi_val = None
    if not is_insufficient(rsi_res):
        rsi_val = rsi_res.get("latest")
        if rsi_val is not None:
            if rsi_val < 30.0:
                rsi_score = 35.0
                key_signals.append(f"RSI quá bán sâu ({rsi_val:.1f} < 30)")
            elif rsi_val < 35.0:
                rsi_score = 25.0
                key_signals.append(f"RSI chạm vùng quá bán ({rsi_val:.1f} < 35)")
            elif rsi_val < 40.0:
                rsi_score = 15.0
    highlights["rsi_14"] = round(rsi_val, 2) if rsi_val is not None else None

    # 2. Support proximity (35 pts)
    supp_score = 0.0
    w = min(20, len(close))
    low_w = float(close.tail(w).min())
    dist_supp_pct = ((latest_close - low_w) / low_w) * 100 if low_w > 0 else 0.0
    if 0.0 <= dist_supp_pct <= 2.5:
        supp_score = 35.0
        key_signals.append(f"Giá sát ngưỡng hỗ trợ đáy {w} phiên (+{dist_supp_pct:.1f}%)")
    elif dist_supp_pct <= 5.0:
        supp_score = 20.0
        key_signals.append(f"Giá gần vùng hỗ trợ {w} phiên (+{dist_supp_pct:.1f}%)")
    highlights["support_level"] = low_w
    highlights["distance_to_support_pct"] = round(dist_supp_pct, 2)

    # 3. Bullish Candlestick Pattern (30 pts)
    candlestick_score = 0.0
    pattern_name = None
    open_val = finite(frame["open"].iloc[-1]) if "open" in frame else None
    high_val = finite(frame["high"].iloc[-1]) if "high" in frame else None
    low_val = finite(frame["low"].iloc[-1]) if "low" in frame else None

    if open_val is not None and high_val is not None and low_val is not None and high_val > low_val:
        body = abs(latest_close - open_val)
        candle_range = high_val - low_val
        upper_shadow = high_val - max(open_val, latest_close)
        lower_shadow = min(open_val, latest_close) - low_val

        # Hammer
        if lower_shadow >= 2 * body and upper_shadow <= 0.25 * candle_range and lower_shadow > 0.4 * candle_range:
            candlestick_score = 30.0
            pattern_name = "Hammer"
            key_signals.append("Nến Hammer rút chân đảo chiều mạnh tại hỗ trợ")
        # Bullish Engulfing
        elif len(frame) > 1 and "open" in frame and pd.notna(frame["open"].iloc[-2]):
            prev_o = float(frame["open"].iloc[-2])
            prev_c = float(frame["close"].iloc[-2])
            if prev_c < prev_o and latest_close > open_val and open_val <= prev_c and latest_close >= prev_o:
                candlestick_score = 30.0
                pattern_name = "Bullish Engulfing"
                key_signals.append("Mô hình nến Bullish Engulfing đảo chiều tăng")
            elif body / candle_range <= 0.1:
                candlestick_score = 20.0
                pattern_name = "Doji"
                key_signals.append("Nến Doji ngừng giảm tại hỗ trợ")
            elif latest_close > open_val:
                candlestick_score = 10.0
                pattern_name = "Bullish Bar"
        elif body / candle_range <= 0.1:
            candlestick_score = 20.0
            pattern_name = "Doji"
            key_signals.append("Nến Doji ngừng giảm tại hỗ trợ")
    else:
        # Close-only proxy: bounce after decline
        if len(close) >= 3:
            c1 = float(close.iloc[-2])
            c2 = float(close.iloc[-3])
            if c1 < c2 and latest_close > c1:
                bounce_pct = ((latest_close - c1) / c1) * 100
                if bounce_pct >= 1.0:
                    candlestick_score = 25.0
                    pattern_name = "Reversal Bounce"
                    key_signals.append(f"Nến đảo chiều bật tăng (+{bounce_pct:.1f}%) từ đáy")
                else:
                    candlestick_score = 10.0
                    pattern_name = "Minor Bounce"

    highlights["candlestick_pattern"] = pattern_name

    composite_score = round(rsi_score + supp_score + candlestick_score, 1)
    return composite_score, key_signals, highlights


def _score_foreign_accumulation(frame: pd.DataFrame) -> tuple[float, list[str], dict[str, Any]]:
    """Score candidate on foreign accumulation strategy (0-100).

    Combines:
    - 5d positive foreign net value (40 pts)
    - Foreign buy participation % (35 pts)
    - Stable or expanding room (25 pts)
    """
    key_signals: list[str] = []
    highlights: dict[str, Any] = {}

    w = min(5, len(frame))
    sub = frame.tail(w)

    buy_val = pd.to_numeric(sub.get("foreign_buy_value", 0), errors="coerce").fillna(0.0)
    sell_val = pd.to_numeric(sub.get("foreign_sell_value", 0), errors="coerce").fillna(0.0)
    total_val = pd.to_numeric(sub.get("total_value", 0), errors="coerce").fillna(0.0)

    net_val_5d = float((buy_val - sell_val).sum())
    total_val_5d = float(total_val.sum())
    total_buy_val_5d = float(buy_val.sum())
    net_val_bil = round(net_val_5d / 1e9, 2)

    # 1. 5d net foreign value (40 pts)
    net_score = 0.0
    if net_val_5d > 0:
        if net_val_bil >= 50.0:
            net_score = 40.0
            key_signals.append(f"Khối ngoại gom ròng đột biến {w} phiên (+{net_val_bil:,.1f} tỷ VND)")
        elif net_val_bil >= 10.0:
            net_score = 30.0
            key_signals.append(f"Khối ngoại mua ròng {w} phiên (+{net_val_bil:,.1f} tỷ VND)")
        else:
            net_score = 20.0
            key_signals.append(f"Khối ngoại mua ròng nhẹ {w} phiên (+{net_val_bil:,.1f} tỷ VND)")

    # 2. Foreign buy participation % (35 pts)
    part_score = 0.0
    part_pct = round((total_buy_val_5d / total_val_5d * 100), 2) if total_val_5d > 0 else 0.0
    if part_pct >= 25.0:
        part_score = 35.0
        key_signals.append(f"Tỷ trọng mua ngoại chiếm áp đảo ({part_pct:.1f}% thanh khoản)")
    elif part_pct >= 15.0:
        part_score = 25.0
        key_signals.append(f"Tỷ trọng mua ngoại tích cực ({part_pct:.1f}% thanh khoản)")
    elif part_pct >= 8.0:
        part_score = 15.0

    # 3. Foreign room status (25 pts)
    room_score = 0.0
    room_s = pd.to_numeric(sub.get("foreign_room", 0), errors="coerce").fillna(0)
    latest_room = int(room_s.iloc[-1]) if len(room_s) > 0 else 0
    if latest_room > 1_000_000:
        room_score = 25.0
        key_signals.append("Room ngoại dồi dào, không chịu áp lực cạn room")
    elif latest_room > 200_000:
        room_score = 15.0
    elif latest_room > 0:
        room_score = 5.0
        key_signals.append(f"Cảnh báo: Room ngoại chỉ còn {latest_room:,} cp")
    else:
        room_score = 0.0

    highlights["foreign_net_value_5d_bil"] = net_val_bil
    highlights["foreign_buy_participation_pct"] = part_pct
    highlights["foreign_room"] = latest_room
    highlights["room_status"] = "ample" if latest_room > 1_000_000 else ("limited" if latest_room > 0 else "exhausted")

    composite_score = round(net_score + part_score + room_score, 1)
    return composite_score, key_signals, highlights


def _score_intraday_breakout(frame: pd.DataFrame) -> tuple[float, list[str], dict[str, Any]]:
    """Score candidate on intraday 1H breakout strategy (0-100).

    Checks if recent 1h sessions broke above session 1/2 highs with volume acceleration.
    """
    key_signals: list[str] = []
    highlights: dict[str, Any] = {}

    latest_row = frame.iloc[-1]
    latest_close = float(latest_row["close"])
    latest_vol = float(latest_row.get("volume", latest_row.get("total_volume", 0)) or 0)

    # Group bars for latest trading day
    day_key = "date" if "date" in frame else None
    if day_key and len(frame) >= 3:
        latest_date = frame[day_key].iloc[-1]
        day_bars = frame[frame[day_key] == latest_date]
    else:
        day_bars = frame.tail(5)

    if len(day_bars) < 2:
        return 0.0, ["Không đủ nến 1H trong ngày để xác định breakout"], {"is_breakout": False}

    # Morning session: first 1 or 2 bars
    morning_bars = day_bars.iloc[: min(2, len(day_bars) - 1)]
    morning_high_series = morning_bars["high"] if "high" in morning_bars and morning_bars["high"].notna().any() else morning_bars["close"]
    morning_high = float(morning_high_series.max())
    morning_vol_series = morning_bars["volume"] if "volume" in morning_bars else morning_bars.get("total_volume", pd.Series([1.0]))
    morning_avg_vol = float(morning_vol_series.mean()) if len(morning_vol_series) > 0 else 1.0

    # 1. Price breakout over morning high (45 pts)
    price_score = 0.0
    dist_breakout_pct = 0.0
    if latest_close > morning_high:
        dist_breakout_pct = ((latest_close - morning_high) / morning_high) * 100 if morning_high > 0 else 0.0
        price_score = min(45.0, 35.0 + min(10.0, dist_breakout_pct * 5))
        key_signals.append(f"Nến 1H chiều bứt phá qua đỉnh sáng (+{dist_breakout_pct:.1f}%)")

    # 2. Volume acceleration (35 pts)
    vol_score = 0.0
    vol_acc = (latest_vol / morning_avg_vol) if morning_avg_vol > 0 else 1.0
    if vol_acc >= 1.5:
        vol_score = 35.0
        key_signals.append(f"Khối lượng 1H bùng nổ {vol_acc:.2f}x so với phiên sáng")
    elif vol_acc >= 1.2:
        vol_score = 25.0
        key_signals.append(f"Khối lượng 1H gia tăng {vol_acc:.2f}x so với phiên sáng")
    elif vol_acc >= 1.0:
        vol_score = 10.0

    # 3. Intraday upward trend (20 pts)
    first_open = float(day_bars.iloc[0].get("open") or day_bars.iloc[0]["close"])
    trend_score = 20.0 if latest_close > first_open else 0.0

    highlights["morning_high"] = morning_high
    highlights["latest_1h_close"] = latest_close
    highlights["volume_acceleration"] = round(vol_acc, 2)
    highlights["is_breakout"] = (price_score > 0)

    composite_score = round(price_score + vol_score + trend_score, 1)
    return composite_score, key_signals, highlights


def screen_and_rank(
    universe_data: dict[str, list[dict]],
    strategy: str = "momentum_breakout",
    timeframe: str = "1d",
    top_n: int = 3,
) -> dict[str, Any]:
    """Screen and rank a universe of tickers by a given technical strategy.

    Parameters
    ----------
    universe_data : dict[str, list[dict]]
        Mapping of symbol -> list of row dicts.
    strategy : str
        One of 'momentum_breakout', 'oversold_reversal', 'foreign_accumulation', 'intraday_breakout'.
    timeframe : str
        '1d' or '1h'.
    top_n : int
        Number of top candidates to return (1 to 10).
    """
    universe_size = len(universe_data)
    candidates: list[dict[str, Any]] = []

    strategy_func = {
        "momentum_breakout": _score_momentum_breakout,
        "oversold_reversal": _score_oversold_reversal,
        "foreign_accumulation": _score_foreign_accumulation,
        "intraday_breakout": _score_intraday_breakout,
    }.get(strategy)

    if strategy_func is None:
        raise ValueError(f"Unknown strategy: '{strategy}'")

    min_rows = MIN_ROWS_REQUIRED.get(strategy, 5)

    for sym, rows in universe_data.items():
        if not rows or len(rows) < min_rows:
            candidates.append({
                "symbol": sym,
                "score": 0.0,
                "price": float(rows[-1]["close"]) if rows else 0.0,
                "price_change_pct": 0.0,
                "key_signals": [f"Không đủ dữ liệu ({len(rows) if rows else 0}/{min_rows} phiên)"],
                "highlights": {"insufficient_data": True, "rows_available": len(rows) if rows else 0},
            })
            continue

        try:
            frame = _rows_to_df(rows)
            latest_c = float(frame["close"].iloc[-1])
            prev_c = float(frame["prev_close"].iloc[-1]) if "prev_close" in frame and pd.notna(frame["prev_close"].iloc[-1]) else (float(frame["close"].iloc[-2]) if len(frame) > 1 else latest_c)
            change_pct = round(((latest_c - prev_c) / prev_c) * 100, 2) if prev_c > 0 else 0.0

            score, key_signals, highlights = strategy_func(frame)

            candidates.append({
                "symbol": sym,
                "score": score,
                "price": latest_c,
                "price_change_pct": change_pct,
                "key_signals": key_signals,
                "highlights": highlights,
            })
        except Exception as exc:
            candidates.append({
                "symbol": sym,
                "score": 0.0,
                "price": float(rows[-1]["close"]) if rows else 0.0,
                "price_change_pct": 0.0,
                "key_signals": [f"Lỗi tính toán: {str(exc)}"],
                "highlights": {"error": str(exc)},
            })

    # Sort descending by score, tie-break by price_change_pct descending, then symbol
    candidates.sort(key=lambda x: (x["score"], x["price_change_pct"]), reverse=True)

    ranked_candidates = [
        {
            "rank": i + 1,
            "symbol": c["symbol"],
            "score": c["score"],
            "price": c["price"],
            "price_change_pct": c["price_change_pct"],
            "key_signals": c["key_signals"],
            "highlights": c["highlights"],
        }
        for i, c in enumerate(candidates[:top_n])
    ]

    return {
        "universe_size": universe_size,
        "scanned_count": len(universe_data),
        "strategy_used": strategy,
        "timeframe": timeframe,
        "ranked_candidates": ranked_candidates,
    }


def scan_foreign_flow(
    universe_data: dict[str, list[dict]],
    window_days: int = 5,
    top_n: int = 5,
) -> dict[str, Any]:
    """Scan foreign money flow and room dynamics across a universe of tickers.

    Parameters
    ----------
    universe_data : dict[str, list[dict]]
        Mapping of symbol -> list of row dicts.
    window_days : int
        Observation window in trading sessions.
    top_n : int
        Number of top net buyers and sellers to return.
    """
    universe_size = len(universe_data)
    items: list[dict[str, Any]] = []
    room_warnings: list[dict[str, Any]] = []

    for sym, rows in universe_data.items():
        if not rows:
            continue
        try:
            frame = _rows_to_df(rows)
            sub = frame.tail(window_days)
            buy_val = pd.to_numeric(sub.get("foreign_buy_value", 0), errors="coerce").fillna(0.0).sum()
            sell_val = pd.to_numeric(sub.get("foreign_sell_value", 0), errors="coerce").fillna(0.0).sum()
            buy_vol = int(pd.to_numeric(sub.get("foreign_buy_volume", 0), errors="coerce").fillna(0).sum())
            sell_vol = int(pd.to_numeric(sub.get("foreign_sell_volume", 0), errors="coerce").fillna(0).sum())
            total_val = float(pd.to_numeric(sub.get("total_value", 0), errors="coerce").fillna(0.0).sum())

            net_val = float(buy_val - sell_val)
            net_vol = int(buy_vol - sell_vol)
            buy_part = round((buy_val / total_val * 100), 2) if total_val > 0 else 0.0
            sell_part = round((sell_val / total_val * 100), 2) if total_val > 0 else 0.0

            room_s = pd.to_numeric(sub.get("foreign_room", 0), errors="coerce").fillna(0)
            latest_room = int(room_s.iloc[-1]) if len(room_s) > 0 else 0
            is_warning = (latest_room < 500_000)

            if is_warning:
                room_warnings.append({
                    "symbol": sym,
                    "room_remaining": latest_room,
                    "warning": f"Room ngoại của {sym} chỉ còn {latest_room:,} cổ phiếu (nguy cơ cạn room).",
                })

            items.append({
                "symbol": sym,
                "net_value_vnd": net_val,
                "net_value_billion": round(net_val / 1e9, 2),
                "net_volume": net_vol,
                "buy_participation_pct": buy_part,
                "sell_participation_pct": sell_part,
                "room_remaining": latest_room,
                "room_exhaustion_warning": is_warning,
            })
        except Exception:
            continue

    # Top net buyers: highest positive net value
    buyers = sorted([it for it in items if it["net_value_vnd"] > 0], key=lambda x: x["net_value_vnd"], reverse=True)
    if not buyers:
        buyers = sorted(items, key=lambda x: x["net_value_vnd"], reverse=True)

    top_buyers = [
        {
            "rank": i + 1,
            "symbol": it["symbol"],
            "net_value_vnd": it["net_value_vnd"],
            "net_value_billion": it["net_value_billion"],
            "net_volume": it["net_volume"],
            "buy_participation_pct": it["buy_participation_pct"],
            "room_remaining": it["room_remaining"],
            "room_exhaustion_warning": it["room_exhaustion_warning"],
        }
        for i, it in enumerate(buyers[:top_n])
    ]

    # Top net sellers: lowest negative net value (most sold)
    sellers = sorted([it for it in items if it["net_value_vnd"] < 0], key=lambda x: x["net_value_vnd"])
    if not sellers:
        sellers = sorted(items, key=lambda x: x["net_value_vnd"])

    top_sellers = [
        {
            "rank": i + 1,
            "symbol": it["symbol"],
            "net_value_vnd": it["net_value_vnd"],
            "net_value_billion": it["net_value_billion"],
            "net_volume": it["net_volume"],
            "sell_participation_pct": it["sell_participation_pct"],
            "room_remaining": it["room_remaining"],
        }
        for i, it in enumerate(sellers[:top_n])
    ]

    return {
        "universe_size": universe_size,
        "scanned_count": len(items),
        "window_days": window_days,
        "top_net_buyers": top_buyers,
        "top_net_sellers": top_sellers,
        "room_warnings": room_warnings,
    }
