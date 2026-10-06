"""52-week (~250 trading rows) performance and statistical metrics.

Computes 52-week high, low, max drawdown from peak to trough, current distance
from extremes, 250-session average traded volume and value, and price
normalization for head-to-head comparison.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from indicators import insufficient, require


def stats_52w(
    frame: pd.DataFrame,
    window: int = 250,
) -> dict[str, Any]:
    """Calculate 52-week (~250 trading days) stats from stored frame.

    Parameters
    ----------
    frame : pd.DataFrame
        Frame with date index, containing 'close', 'total_volume', 'total_value'.
    window : int
        Lookback window in trading sessions, default 250 (~1 calendar year).
    """
    # Require most of the requested window, not a flat 20 rows. With the old
    # floor a 20-row history produced a "52-week high" and a "1-year return",
    # names that describe a statistic the data cannot support — the same
    # shorter-window substitution the engine refuses everywhere else.
    minimum = max(20, int(window * 0.8))
    marker = require(frame["close"], minimum, f"stats_52w({window})")
    if marker:
        return marker

    close = pd.to_numeric(frame["close"], errors="coerce")
    tail = close.tail(window)
    dates = [str(d) for d in tail.index]
    values = tail.to_numpy()

    if len(values) == 0:
        return insufficient("no valid close data in tail", window)

    latest_close = float(values[-1])

    # 52w High and Low
    max_idx = int(np.argmax(values))
    min_idx = int(np.argmin(values))
    high_val = float(values[max_idx])
    high_date = dates[max_idx]
    low_val = float(values[min_idx])
    low_date = dates[min_idx]

    pct_from_high = round((latest_close - high_val) / high_val * 100, 2) if high_val > 0 else 0.0
    pct_from_low = round((latest_close - low_val) / low_val * 100, 2) if low_val > 0 else 0.0

    # Max Drawdown within the window (peak-to-trough)
    # Running cumulative max
    running_max = np.maximum.accumulate(values)
    drawdowns = (values - running_max) / running_max
    max_drawdown_pct = round(float(np.min(drawdowns)) * 100, 2)

    # 250-session return
    first_close = float(values[0])
    return_window_pct = (
        round((latest_close - first_close) / first_close * 100, 2)
        if first_close > 0
        else 0.0
    )

    # Averages
    vol_tail = pd.to_numeric(frame["total_volume"], errors="coerce").tail(window)
    val_tail = pd.to_numeric(frame["total_value"], errors="coerce").tail(window)

    avg_vol_shares = float(vol_tail.mean()) if not vol_tail.empty else 0.0
    avg_val_vnd = float(val_tail.mean()) if not val_tail.empty else 0.0

    # Normalized price series (base 100 at start of window)
    norm_values = [
        round(float(v / first_close * 100), 2) if first_close > 0 else 100.0
        for v in values
    ]

    # Name the statistic after the window it was actually measured over, so a
    # consumer cannot present a 120-session extreme as a 52-week one.
    sessions = len(values)
    approx_weeks = round(sessions / 5)
    return {
        "window_sessions": sessions,
        "window_requested": window,
        "window_label_vi": f"{sessions} phiên (~{approx_weeks} tuần)",
        "is_full_52w": sessions >= 240,
        "basis_note": (
            "Đỉnh/đáy tính trên GIÁ ĐÓNG CỬA, không phải đỉnh/đáy trong phiên "
            "(feed không có high/low)."
        ),
        "date_start": dates[0],
        "date_end": dates[-1],
        "latest_close": latest_close,
        "high_52w": {
            "price": high_val,
            "date": high_date,
            "pct_from_current": pct_from_high,
        },
        "low_52w": {
            "price": low_val,
            "date": low_date,
            "pct_from_current": pct_from_low,
        },
        "max_drawdown_pct": max_drawdown_pct,
        "return_pct": return_window_pct,
        "avg_daily_volume_shares": round(avg_vol_shares, 0),
        "avg_daily_volume_mil": round(avg_vol_shares / 1e6, 2),
        "avg_daily_value_vnd": round(avg_val_vnd, 0),
        "avg_daily_value_bil": round(avg_val_vnd / 1e9, 2),
        "normalized_series_100": {
            "dates": dates,
            "values": norm_values,
        },
    }
