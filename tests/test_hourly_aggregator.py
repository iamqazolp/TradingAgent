"""Tests for HourlyBarAggregator and Vietnam trading sessions."""

from __future__ import annotations

import sqlite3
import pytest

from data import store
from data.aggregator import HourlyBarAggregator, classify_session, parse_timestamp
from data.provider import RealtimeSnapshot


@pytest.fixture
def conn():
    connection = store.connect(":memory:")
    yield connection
    connection.close()


def test_session_classification():
    # Session 1: 09:00 - 10:00 (ATO)
    s1 = classify_session(parse_timestamp("2026-10-06 09:15:30"))
    assert s1 == (1, "2026-10-06", "2026-10-06 09:00:00")

    # Session 2: 10:00 - 11:30
    s2 = classify_session(parse_timestamp("2026-10-06 11:29:59"))
    assert s2 == (2, "2026-10-06", "2026-10-06 10:00:00")

    # Lunch break: 11:30:01 - 12:59:59 -> None
    lunch = classify_session(parse_timestamp("2026-10-06 12:00:00"))
    assert lunch is None

    # Session 3: 13:00 - 14:00
    s3 = classify_session(parse_timestamp("2026-10-06 13:45:00"))
    assert s3 == (3, "2026-10-06", "2026-10-06 13:00:00")

    # Session 4: 14:00 - 15:00 (ATC)
    s4 = classify_session(parse_timestamp("2026-10-06 14:40:00"))
    assert s4 == (4, "2026-10-06", "2026-10-06 14:00:00")

    # Outside market hours
    after = classify_session(parse_timestamp("2026-10-06 15:05:00"))
    assert after is None


def test_incremental_aggregation_same_session(conn):
    agg = HourlyBarAggregator(conn)

    # 1st snapshot of Session 1
    snap1 = RealtimeSnapshot("HPG", "2026-10-06 09:05:00", 28000.0, 100000, 2800000000.0)
    bar1 = agg.process_snapshot(snap1)
    assert bar1.open == pytest.approx(28000.0)
    assert bar1.high == pytest.approx(28000.0)
    assert bar1.low == pytest.approx(28000.0)
    assert bar1.close == pytest.approx(28000.0)
    assert bar1.volume == 100000

    # 2nd snapshot (new high)
    snap2 = RealtimeSnapshot("HPG", "2026-10-06 09:20:00", 28500.0, 300000, 8450000000.0)
    bar2 = agg.process_snapshot(snap2)
    assert bar2.open == pytest.approx(28000.0)
    assert bar2.high == pytest.approx(28500.0)
    assert bar2.low == pytest.approx(28000.0)
    assert bar2.close == pytest.approx(28500.0)
    assert bar2.volume == 300000

    # 3rd snapshot (new low)
    snap3 = RealtimeSnapshot("HPG", "2026-10-06 09:45:00", 27800.0, 450000, 12600000000.0)
    bar3 = agg.process_snapshot(snap3)
    assert bar3.open == pytest.approx(28000.0)
    assert bar3.high == pytest.approx(28500.0)
    assert bar3.low == pytest.approx(27800.0)
    assert bar3.close == pytest.approx(27800.0)
    assert bar3.volume == 450000


def test_session_transition_calculates_differential_volume(conn):
    agg = HourlyBarAggregator(conn)

    # Complete Session 1
    agg.process_snapshot(RealtimeSnapshot("HPG", "2026-10-06 09:50:00", 28200.0, 500000, 14100000000.0))

    # Move to Session 2 (10:00) with accumulated volume 750,000
    bar_s2 = agg.process_snapshot(RealtimeSnapshot("HPG", "2026-10-06 10:15:00", 28300.0, 750000, 21175000000.0))
    assert bar_s2.session_index == 2
    assert bar_s2.datetime == "2026-10-06 10:00:00"
    # Session 2's own volume = 750,000 - 500,000 = 250,000
    assert bar_s2.volume == 250000

    # Check that Session 1 bar was marked closed
    s1_bar = store.get_hourly_bar(conn, "HPG", "2026-10-06 09:00:00")
    assert s1_bar["is_closed"] == 1
