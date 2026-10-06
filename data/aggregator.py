"""Hourly Bar Aggregation Engine for Vietnam Stock Market (HOSE/HNX/UPCOM).

Vietnam trading session structure (Monday - Friday):
- Session 1: 09:00:00 - 09:59:59 (Bar start: 09:00:00, includes ATO 09:00-09:15)
- Session 2: 10:00:00 - 11:30:00 (Bar start: 10:00:00, morning close at 11:30)
- Lunch break: 11:30:01 - 12:59:59 (Market closed, ignored)
- Session 3: 13:00:00 - 13:59:59 (Bar start: 13:00:00, afternoon first hour)
- Session 4: 14:00:00 - 15:00:00 (Bar start: 14:00:00, includes ATC 14:30-14:45)
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Sequence
from datetime import datetime, time
from typing import Any

from data import store
from data.provider import HourlyBar, RealtimeSnapshot

logger = logging.getLogger(__name__)

# Session boundary definitions
SESSION_1_START = time(9, 0, 0)
SESSION_1_END = time(10, 0, 0)
SESSION_2_START = time(10, 0, 0)
SESSION_2_END = time(11, 30, 0)
SESSION_3_START = time(13, 0, 0)
SESSION_3_END = time(14, 0, 0)
SESSION_4_START = time(14, 0, 0)
SESSION_4_END = time(15, 0, 0)


def classify_session(dt: datetime) -> tuple[int, str, str] | None:
    """Classify a timestamp into a Vietnam trading session.

    Returns (session_index, date_str, bar_datetime_str) or None if outside market hours.
    """
    t = dt.time()
    date_str = dt.strftime("%Y-%m-%d")

    if SESSION_1_START <= t < SESSION_1_END:
        return (1, date_str, f"{date_str} 09:00:00")
    if SESSION_2_START <= t <= SESSION_2_END:
        return (2, date_str, f"{date_str} 10:00:00")
    if SESSION_3_START <= t < SESSION_3_END:
        return (3, date_str, f"{date_str} 13:00:00")
    if SESSION_4_START <= t <= SESSION_4_END:
        return (4, date_str, f"{date_str} 14:00:00")

    return None


def parse_timestamp(value: str | datetime) -> datetime:
    """Parse an ISO or space-separated timestamp string."""
    if isinstance(value, datetime):
        return value
    text = value.strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise ValueError(f"Unrecognized timestamp format: {value!r}")


class HourlyBarAggregator:
    """Aggregates realtime quotes/snapshots into 1H bars stored in SQLite."""

    def __init__(self, conn: sqlite3.Connection | None = None) -> None:
        self._conn = conn

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        return store.connect()

    def process_snapshot(
        self,
        snapshot: RealtimeSnapshot | dict,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> HourlyBar | None:
        """Process one snapshot, store it, update the forming 1H bar, and return the bar."""
        if isinstance(snapshot, dict):
            snap = RealtimeSnapshot(**snapshot)
        else:
            snap = snapshot

        target_conn = conn or self._get_conn()
        dt = parse_timestamp(snap.timestamp)
        session_info = classify_session(dt)

        if session_info is None:
            logger.debug(
                "Snapshot for %s at %s is outside trading sessions; skipped",
                snap.ticker,
                snap.timestamp,
            )
            return None

        session_index, date_str, bar_datetime = session_info

        # Record raw snapshot in store
        store.insert_snapshot(target_conn, snap)

        # Calculate prior sessions' volume and value accumulated on this day
        prior_sql = """
        SELECT COALESCE(SUM(volume), 0), COALESCE(SUM(value), 0)
        FROM hourly_bars
        WHERE ticker = ? AND date = ? AND session_index < ?
        """
        row = target_conn.execute(
            prior_sql, (snap.ticker.upper(), date_str, session_index)
        ).fetchone()
        prior_volume = int(row[0]) if row else 0
        prior_value = float(row[1]) if row else 0.0

        session_volume = max(0, snap.accumulated_volume - prior_volume)
        session_value = max(0.0, snap.accumulated_value - prior_value)

        # Check if an hourly bar already exists for this slot
        existing = store.get_hourly_bar(target_conn, snap.ticker, bar_datetime)

        if existing is None:
            new_bar = HourlyBar(
                ticker=snap.ticker.upper(),
                datetime=bar_datetime,
                date=date_str,
                session_index=session_index,
                open=snap.price,
                high=snap.price,
                low=snap.price,
                close=snap.price,
                volume=session_volume,
                value=session_value,
                is_closed=0,
            )
        else:
            new_bar = HourlyBar(
                ticker=snap.ticker.upper(),
                datetime=bar_datetime,
                date=date_str,
                session_index=session_index,
                open=existing["open"],
                high=max(existing["high"], snap.price),
                low=min(existing["low"], snap.price),
                close=snap.price,
                volume=session_volume,
                value=session_value,
                is_closed=existing.get("is_closed", 0),
            )

        # Close any prior open session bars on the same day
        close_prior_sql = """
        UPDATE hourly_bars
        SET is_closed = 1
        WHERE ticker = ? AND date = ? AND session_index < ? AND is_closed = 0
        """
        target_conn.execute(
            close_prior_sql, (snap.ticker.upper(), date_str, session_index)
        )

        # Upsert the updated forming bar
        store.upsert_hourly_bars(target_conn, [new_bar])
        return new_bar

    def process_snapshots(
        self,
        snapshots: Sequence[RealtimeSnapshot | dict],
        *,
        conn: sqlite3.Connection | None = None,
    ) -> list[HourlyBar]:
        """Process a sequence of snapshots sequentially."""
        bars: list[HourlyBar] = []
        target_conn = conn or self._get_conn()
        for snap in snapshots:
            bar = self.process_snapshot(snap, conn=target_conn)
            if bar is not None:
                bars.append(bar)
        return bars

    def close_all_sessions_for_date(
        self, ticker: str, date_str: str, *, conn: sqlite3.Connection | None = None
    ) -> int:
        """Mark all hourly bars for a given ticker and date as closed."""
        target_conn = conn or self._get_conn()
        cursor = target_conn.execute(
            "UPDATE hourly_bars SET is_closed = 1 WHERE ticker = ? AND date = ?",
            (ticker.upper(), date_str),
        )
        target_conn.commit()
        return cursor.rowcount
