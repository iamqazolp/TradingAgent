"""Classic Pivot Points calculation adapted for equity technical analysis.

Calculates Pivot Point (PP), Resistance levels (R1, R2, R3), and Support
levels (S1, S2, S3) from the most recent session data, and evaluates the
position of the latest closing price relative to these levels.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from indicators import finite, insufficient, require, safe_div


def classic_pivots(frame: pd.DataFrame) -> dict[str, Any]:
    """Calculate Classic Pivot Points from the last session.

    If High and Low columns are available in the frame, they are used directly.
    If only Close and PrevClose are present (VietinBank close-only feed),
    a close-based synthetic range is adapted using the session spread and
    recent 5-session volatility.
    """
    if len(frame) < 1:
        return insufficient("no data available for pivots", 1)

    latest_row = frame.iloc[-1]
    close = finite(latest_row.get("close"))
    if close is None or close <= 0:
        return insufficient("valid close price required for pivots", 1)

    # Check for real High/Low
    has_high = "high" in frame.columns and finite(latest_row.get("high")) is not None
    has_low = "low" in frame.columns and finite(latest_row.get("low")) is not None

    if has_high and has_low:
        high = float(latest_row["high"])
        low = float(latest_row["low"])
        is_close_only_adapted = False
    else:
        # Estimate high / low from session change and 5-day close spread
        prev_close = finite(latest_row.get("prev_close")) or close
        raw_max = max(close, prev_close)
        raw_min = min(close, prev_close)
        diff = abs(close - prev_close)

        # Ensure a realistic intraday range buffer (minimum 1.5% or 5-day stdev)
        if len(frame) >= 5:
            c5 = pd.to_numeric(frame["close"].tail(5), errors="coerce")
            stdev5 = float(c5.std(ddof=0)) if len(c5) > 1 else close * 0.015
            buffer = max(diff * 0.5, stdev5 * 0.5, close * 0.01)
        else:
            buffer = max(diff * 0.5, close * 0.015)

        high = raw_max + buffer
        low = raw_min - buffer
        is_close_only_adapted = True

    # Classic Pivot Formulas
    pp = (high + low + close) / 3.0
    r1 = 2.0 * pp - low
    s1 = 2.0 * pp - high
    r2 = pp + (high - low)
    s2 = pp - (high - low)
    r3 = high + 2.0 * (pp - low)
    s3 = low - 2.0 * (high - pp)

    # Evaluate current price position
    if close > r2:
        position = "above_r2"
        position_desc = "Vượt trên kháng cự R2 (xung lực tăng mạnh)"
    elif close > r1:
        position = "between_r1_and_r2"
        position_desc = "Nằm giữa kháng cự R1 và R2"
    elif close > pp:
        position = "between_pp_and_r1"
        position_desc = "Nằm giữa Pivot Point và kháng cự R1 (thiên hướng tích cực)"
    elif close > s1:
        position = "between_s1_and_pp"
        position_desc = "Nằm giữa hỗ trợ S1 và Pivot Point (vùng cân bằng tích lũy)"
    elif close > s2:
        position = "between_s2_and_s1"
        position_desc = "Nằm giữa hỗ trợ S2 và S1 (chịu áp lực điều chỉnh)"
    elif close > s3:
        position = "between_s3_and_s2"
        position_desc = "Nằm giữa hỗ trợ S3 và S2 (vùng quá bán)"
    else:
        position = "below_s3"
        position_desc = "Rơi xuống dưới hỗ trợ S3 (mất toàn bộ ngưỡng hỗ trợ pivot)"

    # Nearest support and resistance
    supports = [
        {"name": "S1", "level": round(s1, 0), "distance_pct": round(abs(close - s1) / close * 100, 2)},
        {"name": "S2", "level": round(s2, 0), "distance_pct": round(abs(close - s2) / close * 100, 2)},
        {"name": "S3", "level": round(s3, 0), "distance_pct": round(abs(close - s3) / close * 100, 2)},
    ]
    resistances = [
        {"name": "R1", "level": round(r1, 0), "distance_pct": round(abs(r1 - close) / close * 100, 2)},
        {"name": "R2", "level": round(r2, 0), "distance_pct": round(abs(r2 - close) / close * 100, 2)},
        {"name": "R3", "level": round(r3, 0), "distance_pct": round(abs(r3 - close) / close * 100, 2)},
    ]

    return {
        "date": str(frame.index[-1]),
        "latest_close": close,
        "is_close_only_adapted": is_close_only_adapted,
        "classic": {
            "R3": round(r3, 0),
            "R2": round(r2, 0),
            "R1": round(r1, 0),
            "PP": round(pp, 0),
            "S1": round(s1, 0),
            "S2": round(s2, 0),
            "S3": round(s3, 0),
        },
        "position": position,
        "position_description": position_desc,
        "supports": supports,
        "resistances": resistances,
    }
