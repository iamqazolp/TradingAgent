"""Weekly bar aggregation from daily trading data.

Aggregates daily rows into weekly bars so the indicator engine can compute
on the weekly timeframe without code changes.  The weekly frame preserves
the same column set as the daily frame.

Aggregation rules
-----------------
- **close**: last close of the week (last trading day)
- **prev_close**: close of the prior week (derived via ``shift``)
- **Sum fields**: total_trade, total_value, total_volume, buy/sell counts
  and volumes, foreign buy/sell volumes and values
- **Snapshot (last)**: foreign_room
- **Extra column**: ``trading_days_in_week`` for thin-week detection
"""

from __future__ import annotations

import pandas as pd


#: Fields aggregated by sum across the trading week.
_SUM_COLS: tuple[str, ...] = (
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

#: Fields taking the last value in the week (point-in-time snapshots).
_LAST_COLS: tuple[str, ...] = ("close", "foreign_room")


def aggregate_weekly(frame: pd.DataFrame) -> pd.DataFrame:
    """Aggregate a date-indexed daily frame into weekly bars.

    Parameters
    ----------
    frame
        Daily data with a string date index (``YYYY-MM-DD``), ascending.
        Must contain at least ``close``, ``foreign_room``, and all
        ``_SUM_COLS``.

    Returns
    -------
    pd.DataFrame
        Weekly bars with the same numeric columns as the daily frame plus
        ``trading_days_in_week``, indexed by the **actual last trading
        date** of each ISO week (string ``YYYY-MM-DD``).

    Notes
    -----
    Weeks with zero trading days (full holidays) are silently dropped.
    The first week has ``prev_close = NaN`` because there is no prior
    week; the indicator engine's ``insufficient_data`` mechanism handles
    this gracefully.
    """
    if frame.empty:
        return frame.copy()

    df = frame.copy()

    # Ensure DatetimeIndex for ISO-week grouping
    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index)

    # Build an ISO year–week key for each row
    iso = df.index.isocalendar()  # DataFrame: year, week, day
    df["_iso_year"] = iso["year"].values
    df["_iso_week"] = iso["week"].values

    records: list[dict] = []
    for (_year, _week), group in df.groupby(["_iso_year", "_iso_week"], sort=False):
        group = group.sort_index()
        row: dict = {"_last_date": group.index[-1]}

        # Snapshot fields: last trading day of the week
        for col in _LAST_COLS:
            if col in group.columns:
                row[col] = group[col].iloc[-1]

        # Sum fields: aggregate across the whole week
        for col in _SUM_COLS:
            if col in group.columns:
                row[col] = group[col].sum()

        row["trading_days_in_week"] = len(group)
        records.append(row)

    if not records:
        return pd.DataFrame()

    weekly = pd.DataFrame(records).sort_values("_last_date")

    # Index = actual last trading date (not the theoretical Friday)
    weekly.index = weekly["_last_date"].dt.strftime("%Y-%m-%d")
    weekly.index.name = "date"
    weekly = weekly.drop(columns=["_last_date"])

    # prev_close = close of the prior week, used by the indicator engine
    # for return calculations and OBV sign detection
    weekly["prev_close"] = weekly["close"].shift(1)

    return weekly


def weekly_quality_flags(
    weekly: pd.DataFrame,
    thin_threshold: int = 3,
) -> list[dict]:
    """Return quality flags for weeks that may produce unreliable aggregates.

    A *thin week* (fewer than ``thin_threshold`` trading days) is typically
    caused by holidays (Tết, national holidays) or partial data.  Weekly
    volume/flow/count aggregates on those weeks are not directly comparable
    to normal five-day weeks and should be interpreted with caution.
    """
    if "trading_days_in_week" not in weekly.columns:
        return []

    flags: list[dict] = []
    for date_str, row in weekly.iterrows():
        days = int(row["trading_days_in_week"])
        if days < thin_threshold:
            flags.append(
                {
                    "week_ending": str(date_str),
                    "trading_days": days,
                    "flag": "thin_week",
                    "note": (
                        f"Only {days} trading day(s) this week — weekly volume "
                        "and flow aggregates may be misleading compared to "
                        "full five-day weeks."
                    ),
                }
            )
    return flags
