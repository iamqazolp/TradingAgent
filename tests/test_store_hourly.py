"""Tests for hourly bars and realtime snapshots in SQLite store."""

from __future__ import annotations

import sqlite3
import pytest

from data import store
from data.provider import HourlyBar, RealtimeSnapshot


@pytest.fixture
def conn():
    connection = store.connect(":memory:")
    yield connection
    connection.close()


def test_schema_creates_hourly_tables(conn):
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = {r[0] for r in cursor.fetchall()}
    assert "hourly_bars" in tables
    assert "realtime_snapshots" in tables


def test_upsert_and_get_recent_hourly(conn):
    bars = [
        HourlyBar(
            ticker="VNM",
            datetime="2026-10-06 09:00:00",
            date="2026-10-06",
            session_index=1,
            open=68000.0,
            high=68500.0,
            low=67900.0,
            close=68200.0,
            volume=500000,
            value=34000000000.0,
            is_closed=1,
        ),
        HourlyBar(
            ticker="VNM",
            datetime="2026-10-06 10:00:00",
            date="2026-10-06",
            session_index=2,
            open=68200.0,
            high=68900.0,
            low=68100.0,
            close=68800.0,
            volume=800000,
            value=55000000000.0,
            is_closed=1,
        ),
    ]
    assert store.upsert_hourly_bars(conn, bars) == 2
    assert store.hourly_row_count(conn, "VNM") == 2

    # Query recent
    recent = store.get_recent_hourly(conn, "VNM", 10)
    assert len(recent) == 2
    assert recent[0]["datetime"] == "2026-10-06 09:00:00"
    assert recent[1]["datetime"] == "2026-10-06 10:00:00"
    assert recent[1]["close"] == pytest.approx(68800.0)


def test_hourly_upsert_is_idempotent(conn):
    bar = HourlyBar(
        ticker="HPG",
        datetime="2026-10-06 09:00:00",
        date="2026-10-06",
        session_index=1,
        open=28000.0,
        high=28500.0,
        low=27900.0,
        close=28300.0,
        volume=1000000,
        value=28000000000.0,
        is_closed=0,
    )
    store.upsert_hourly_bars(conn, [bar])
    assert store.hourly_row_count(conn, "HPG") == 1

    # Update with new high and volume
    updated_bar = HourlyBar(
        ticker="HPG",
        datetime="2026-10-06 09:00:00",
        date="2026-10-06",
        session_index=1,
        open=28000.0,
        high=28900.0,
        low=27900.0,
        close=28700.0,
        volume=1500000,
        value=42000000000.0,
        is_closed=1,
    )
    store.upsert_hourly_bars(conn, [updated_bar])
    assert store.hourly_row_count(conn, "HPG") == 1

    queried = store.get_hourly_bar(conn, "HPG", "2026-10-06 09:00:00")
    assert queried["high"] == pytest.approx(28900.0)
    assert queried["close"] == pytest.approx(28700.0)
    assert queried["is_closed"] == 1


def test_get_hourly_range(conn):
    bars = [
        HourlyBar("VNM", f"2026-10-06 {h:02d}:00:00", "2026-10-06", idx, 68000.0, 68500.0, 67500.0, 68000.0, 100000, 6800000000.0)
        for idx, h in enumerate([9, 10, 13, 14], start=1)
    ]
    store.upsert_hourly_bars(conn, bars)

    res = store.get_hourly_range(conn, "VNM", "2026-10-06 09:30:00", "2026-10-06 13:30:00")
    assert len(res) == 2
    assert res[0]["datetime"] == "2026-10-06 10:00:00"
    assert res[1]["datetime"] == "2026-10-06 13:00:00"


def test_insert_and_get_snapshots(conn):
    snap1 = RealtimeSnapshot("VNM", "2026-10-06 09:15:00", 68500.0, 100000, 6850000000.0)
    snap2 = RealtimeSnapshot("VNM", "2026-10-06 09:30:00", 68700.0, 250000, 17150000000.0)
    store.insert_snapshot(conn, snap1)
    store.insert_snapshot(conn, snap2)

    snaps = store.get_snapshots(conn, "VNM", "2026-10-06")
    assert len(snaps) == 2
    assert snaps[0]["price"] == pytest.approx(68500.0)
    assert snaps[1]["price"] == pytest.approx(68700.0)
