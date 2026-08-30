"""Timeframe resampler: aggregates base 1H / 1D bars into target timeframes.

Supported timeframes:
  - 1H : 1 Hour (base intraday)
  - 4H : 4 Hours
  - 1D : 1 Day (standard daily)
  - 3D : 3 Trading Days
  - 1W : 1 Week (ending Friday)
  - 1M : 1 Month (calendar month-end)
  - 1Y : 1 Year (calendar year-end)

Aggregation conventions:
  - open         : first bar's open
  - high         : max of high across bars
  - low          : min of low across bars
  - close        : last bar's close
  - prev_close   : first bar's prev_close (or prior period's close)
  - flows/counts : sum across all bars in period
  - foreign_room : last bar's foreign_room (snapshot)
"""

from __future__ import annotations

import re
from typing import Any, Literal

import numpy as np
import pandas as pd

TIMEFRAMES = ("1H", "4H", "1D", "3D", "1W", "1M", "1Y")

TimeframeStr = Literal["1H", "4H", "1D", "3D", "1W", "1M", "1Y"]


def normalize_timeframe(tf: str | None) -> TimeframeStr:
    """Normalize user input string to canonical timeframe code."""
    if not tf:
        return "1D"
    cleaned = tf.strip().upper()
    mapping: dict[str, TimeframeStr] = {
        "1H": "1H",
        "H": "1H",
        "HOUR": "1H",
        "HOURLY": "1H",
        "60M": "1H",
        "60MIN": "1H",
        "4H": "4H",
        "4HOUR": "4H",
        "240M": "4H",
        "240MIN": "4H",
        "1D": "1D",
        "D": "1D",
        "DAY": "1D",
        "DAILY": "1D",
        "3D": "3D",
        "3DAY": "3D",
        "3DAYS": "3D",
        "1W": "1W",
        "W": "1W",
        "WEEK": "1W",
        "WEEKLY": "1W",
        "1M": "1M",
        "M": "1M",
        "MONTH": "1M",
        "MONTHLY": "1M",
        "1Y": "1Y",
        "Y": "1Y",
        "YEAR": "1Y",
        "YEARLY": "1Y",
        "1A": "1Y",
        "A": "1Y",
        "ANNUAL": "1Y",
    }
    if cleaned in mapping:
        return mapping[cleaned]
    raise ValueError(
        f"unsupported timeframe {tf!r}; supported timeframes are {', '.join(TIMEFRAMES)}"
    )


def resample_bars(df: pd.DataFrame, timeframe: str = "1D") -> pd.DataFrame:
    """Resample an OHLCV + order flow DataFrame into the target timeframe.

    Expects a DataFrame indexed by date/timestamp (string or DatetimeIndex), sorted ascending.
    """
    tf = normalize_timeframe(timeframe)
    if df.empty or len(df) == 0:
        return df.copy()

    # Ensure index is datetime for time-based resampling
    work = df.copy()
    raw_index = work.index
    if not isinstance(work.index, pd.DatetimeIndex):
        work.index = pd.to_datetime(work.index)

    # Check if input is purely daily or has intraday timestamps
    is_intraday = (work.index.hour != 0).any() or (work.index.minute != 0).any()

    # Ensure required price columns exist with sensible fallbacks if missing
    if "open" not in work.columns:
        work["open"] = work["prev_close"] if "prev_close" in work.columns else work["close"]
    if "high" not in work.columns:
        work["high"] = work[["open", "close"]].max(axis=1)
    if "low" not in work.columns:
        work["low"] = work[["open", "close"]].min(axis=1)

    if tf == "1H":
        # 1H base: if already 1H or daily, format index and return
        return _finalize_frame(work, is_intraday=is_intraday)

    if tf == "4H":
        if not is_intraday:
            # Daily data cannot be upsampled to 4H; return daily as closest available
            return _finalize_frame(work, is_intraday=False)
        # Resample intraday to 4-hour bars per day
        resampled = _resample_time_grouper(work, freq="4h")
        return _finalize_frame(resampled, is_intraday=True)

    if tf == "1D":
        if is_intraday:
            # Aggregate intraday into daily bars
            resampled = _resample_by_date(work)
            return _finalize_frame(resampled, is_intraday=False)
        return _finalize_frame(work, is_intraday=False)

    # For multi-day timeframes (3D, 1W, 1M, 1Y), first ensure we have daily bars
    daily = _resample_by_date(work) if is_intraday else work

    if tf == "3D":
        # Fixed 3-day period grouping (anchored to calendar epoch for query boundary stability)
        resampled = _resample_period_grouper(daily, freq="3D")
        return _finalize_frame(resampled, is_intraday=False)

    if tf == "1W":
        # Weekly bars (Friday week-end)
        resampled = _resample_period_grouper(daily, freq="W-FRI")
        return _finalize_frame(resampled, is_intraday=False)

    if tf == "1M":
        # Monthly bars (Calendar month-end)
        resampled = _resample_month_grouper(daily)
        return _finalize_frame(resampled, is_intraday=False)

    if tf == "1Y":
        # Yearly bars (Calendar year-end)
        resampled = _resample_year_grouper(daily)
        return _finalize_frame(resampled, is_intraday=False)

    return _finalize_frame(work, is_intraday=is_intraday)


def _aggregate_group(sub_df: pd.DataFrame, target_date_str: str) -> dict[str, Any]:
    """Aggregate a block of consecutive bars into a single bar."""
    if sub_df.empty:
        return {}

    first_row = sub_df.iloc[0]
    last_row = sub_df.iloc[-1]

    out: dict[str, Any] = {
        "date": target_date_str,
        "prev_close": float(first_row["prev_close"]) if "prev_close" in sub_df else float(first_row["close"]),
        "open": float(first_row["open"]) if "open" in sub_df else float(first_row["close"]),
        "high": float(sub_df["high"].max()) if "high" in sub_df else float(sub_df["close"].max()),
        "low": float(sub_df["low"].min()) if "low" in sub_df else float(sub_df["close"].min()),
        "close": float(last_row["close"]),
    }

    # Sum flow and volume fields
    sum_cols = (
        "total_trade",
        "total_value",
        "total_volume",
        "buy_count",
        "sell_count",
        "buy_volume",
        "sell_volume",
        "foreign_buy_volume",
        "foreign_sell_volume",
        "foreign_buy_value",
        "foreign_sell_value",
    )
    for col in sum_cols:
        if col in sub_df.columns:
            out[col] = sub_df[col].sum()

    # Snapshot fields take the last observation
    if "foreign_room" in sub_df.columns:
        out["foreign_room"] = last_row["foreign_room"]
    if "ticker" in sub_df.columns:
        out["ticker"] = last_row["ticker"]

    return out


def _resample_by_date(df: pd.DataFrame) -> pd.DataFrame:
    """Aggregate intraday hourly bars into 1-day bars."""
    groups = df.groupby(df.index.date)
    records = []
    for dt, group in groups:
        date_str = dt.strftime("%Y-%m-%d")
        records.append(_aggregate_group(group, date_str))
    if not records:
        return pd.DataFrame()
    out = pd.DataFrame(records).set_index("date")
    out.index = pd.to_datetime(out.index)
    return out


def _group_trading_days(df: pd.DataFrame, days: int = 3) -> pd.DataFrame:
    """Aggregate daily bars into consecutive N-trading-day bars."""
    n = len(df)
    if n == 0:
        return pd.DataFrame()
    group_ids = np.arange(n) // days
    records = []
    for _, group in df.groupby(group_ids):
        last_date = group.index[-1].strftime("%Y-%m-%d")
        records.append(_aggregate_group(group, last_date))
    out = pd.DataFrame(records).set_index("date")
    out.index = pd.to_datetime(out.index)
    return out


def _resample_period_grouper(df: pd.DataFrame, freq: str) -> pd.DataFrame:
    """Resample by fixed frequency (e.g. W-FRI) preserving trading dates."""
    groups = df.groupby(pd.Grouper(freq=freq))
    records = []
    for _, group in groups:
        if group.empty:
            continue
        last_date = group.index[-1].strftime("%Y-%m-%d")
        records.append(_aggregate_group(group, last_date))
    if not records:
        return pd.DataFrame()
    out = pd.DataFrame(records).set_index("date")
    out.index = pd.to_datetime(out.index)
    return out


def _resample_month_grouper(df: pd.DataFrame) -> pd.DataFrame:
    """Resample by calendar month."""
    groups = df.groupby([df.index.year, df.index.month])
    records = []
    for _, group in groups:
        if group.empty:
            continue
        last_date = group.index[-1].strftime("%Y-%m-%d")
        records.append(_aggregate_group(group, last_date))
    if not records:
        return pd.DataFrame()
    out = pd.DataFrame(records).set_index("date")
    out.index = pd.to_datetime(out.index)
    return out


def _resample_year_grouper(df: pd.DataFrame) -> pd.DataFrame:
    """Resample by calendar year."""
    groups = df.groupby(df.index.year)
    records = []
    for _, group in groups:
        if group.empty:
            continue
        last_date = group.index[-1].strftime("%Y-%m-%d")
        records.append(_aggregate_group(group, last_date))
    if not records:
        return pd.DataFrame()
    out = pd.DataFrame(records).set_index("date")
    out.index = pd.to_datetime(out.index)
    return out


def _resample_time_grouper(df: pd.DataFrame, freq: str = "4h") -> pd.DataFrame:
    """Resample intraday bars within each trading day."""
    records = []
    for _, day_group in df.groupby(df.index.date):
        for _, block in day_group.groupby(pd.Grouper(freq=freq)):
            if block.empty:
                continue
            last_stamp = block.index[-1].strftime("%Y-%m-%d %H:%M:%S")
            records.append(_aggregate_group(block, last_stamp))
    if not records:
        return pd.DataFrame()
    out = pd.DataFrame(records).set_index("date")
    out.index = pd.to_datetime(out.index)
    return out


def _finalize_frame(df: pd.DataFrame, is_intraday: bool = False) -> pd.DataFrame:
    """Format index back to standard string representation and fix prev_close links."""
    if df.empty:
        return df
    out = df.copy()

    # Format index strings: ISO date or datetime
    if isinstance(out.index, pd.DatetimeIndex):
        if is_intraday:
            out.index = pd.Index([dt.strftime("%Y-%m-%d %H:%M:%S") for dt in out.index], name="date")
        else:
            out.index = pd.Index([dt.strftime("%Y-%m-%d") for dt in out.index], name="date")

    # Reconnect prev_close across aggregated bars so return/OBV continuity is exact
    if len(out) > 1 and "prev_close" in out.columns and "close" in out.columns:
        closes = out["close"].to_numpy()
        prev_closes = out["prev_close"].to_numpy().copy()
        for i in range(1, len(out)):
            prev_closes[i] = closes[i - 1]
        out["prev_close"] = prev_closes

    return out
