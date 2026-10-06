"""Tests for historical ("as of a date") indicator lookups.

Without `as_of` the only value a caller could get was the latest session's, so
"RSI của VNM ngày 2026-01-02" was answered with today's RSI — a confidently
wrong number. These tests pin down that `as_of` returns the value that stood on
the requested date, that it never sees a row after that date, and that a
non-trading day or an out-of-range date is reported rather than guessed.
"""

from __future__ import annotations

import pytest

from data import store
from tests.conftest import build_rows


@pytest.fixture
def seeded_db(tmp_path, monkeypatch):
    """A store holding 300 sessions of one ticker."""
    db = tmp_path / "as_of.sqlite"
    monkeypatch.setenv("TA_AGENT_DB", str(db))
    rows = build_rows(
        ticker="AAA",
        close=[20_000.0 + (i % 31) * 220 - (i % 9) * 80 for i in range(300)],
    )
    conn = store.connect(db)
    try:
        store.upsert_rows(conn, rows)
        dates = [r["date"] for r in rows]
    finally:
        conn.close()
    return dates


def test_as_of_returns_the_value_that_stood_on_that_date(seeded_db):
    """The as_of reading must equal the rolling series value at that date."""
    from mcp_server.server import compute_indicators

    dates = seeded_db
    target = dates[-15]

    # Full run, with a series long enough to contain the target date.
    full = compute_indicators(ticker="AAA", groups=["momentum"], series_tail=30)
    series = full["groups"]["momentum"]["rsi_14"]["series"]
    assert target in series["dates"], "fixture does not cover the target date"
    expected = series["values"][series["dates"].index(target)]

    scoped = compute_indicators(
        ticker="AAA", groups=["momentum"], as_of=target, series_tail=0
    )
    actual = scoped["groups"]["momentum"]["rsi_14"]["latest"]
    assert actual == pytest.approx(expected, abs=1e-9)

    # And it is NOT the latest value, which is what a caller used to get.
    latest = full["groups"]["momentum"]["rsi_14"]["latest"]
    assert actual != pytest.approx(latest, abs=1e-6)


def test_as_of_never_reads_a_row_after_the_date(seeded_db):
    """No lookahead: the frame must end on the requested date."""
    from mcp_server.server import compute_indicators

    target = seeded_db[-40]
    result = compute_indicators(
        ticker="AAA", groups=["trend", "momentum"], as_of=target, series_tail=5
    )
    assert result["date_range"]["end"] == target
    for group in result["groups"].values():
        for entry in group.values():
            if isinstance(entry, dict) and isinstance(entry.get("series"), dict):
                assert max(entry["series"]["dates"]) <= target


def test_as_of_reports_the_effective_date(seeded_db):
    from mcp_server.server import compute_indicators

    target = seeded_db[-10]
    result = compute_indicators(ticker="AAA", groups=["momentum"], as_of=target)
    meta = result["as_of"]
    assert meta["as_of_requested"] == target
    assert meta["as_of_effective"] == target
    assert meta["is_trading_day"] is True
    assert "TẠI NGÀY" in meta["note"]


def test_non_trading_day_falls_back_and_says_so(seeded_db):
    """A weekend or holiday must be reported, not silently treated as valid."""
    from mcp_server.server import compute_indicators

    dates = set(seeded_db)
    # Find a calendar date inside the range that is not a trading day.
    from datetime import date, timedelta

    start = date.fromisoformat(seeded_db[0])
    end = date.fromisoformat(seeded_db[-1])
    gap = None
    cursor = start + timedelta(days=1)
    while cursor < end:
        if cursor.isoformat() not in dates:
            gap = cursor.isoformat()
            break
        cursor += timedelta(days=1)
    assert gap, "fixture has no non-trading calendar day to test"

    result = compute_indicators(ticker="AAA", groups=["momentum"], as_of=gap)
    meta = result["as_of"]
    assert meta["as_of_requested"] == gap
    assert meta["as_of_effective"] < gap
    assert meta["is_trading_day"] is False
    assert "không phải phiên giao dịch" in meta["note"]
    assert result["date_range"]["end"] == meta["as_of_effective"]


def test_date_before_all_history_is_refused_not_guessed(seeded_db):
    from mcp_server.server import compute_indicators

    result = compute_indicators(ticker="AAA", groups=["momentum"], as_of="1999-01-04")
    assert result["error"] == "no_rows_before_as_of"
    assert result["stored_range"]["start"] == seeded_db[0]
    assert "Không có phiên giao dịch nào" in result["message"]
    assert "groups" not in result


def test_as_of_applies_to_flow_summary(seeded_db):
    from mcp_server.server import get_flow_summary

    target = seeded_db[-20]
    result = get_flow_summary(ticker="AAA", as_of=target)
    assert result["as_of"]["as_of_effective"] == target
    assert result["date_range"]["end"] == target


def test_as_of_applies_to_multi_horizon(seeded_db):
    from mcp_server.server import analyze_multi_horizon

    target = seeded_db[-25]
    result = analyze_multi_horizon(ticker="AAA", scope="short_term", as_of=target)
    assert result["as_of"]["as_of_effective"] == target
    assert result["daily"]["date_range"]["end"] == target
    assert "TẠI NGÀY" in result["as_of"]["note"]


def test_as_of_rejects_a_nonsense_date(seeded_db):
    from mcp.server.mcpserver.exceptions import ToolError
    from mcp_server.server import compute_indicators

    for bad in ("2026-13-45", "02/01/2026", "yesterday"):
        with pytest.raises(ToolError):
            compute_indicators(ticker="AAA", groups=["momentum"], as_of=bad)


def test_as_of_with_explicit_rows_is_refused():
    """as_of only means something when the server does the loading."""
    from mcp.server.mcpserver.exceptions import ToolError
    from mcp_server.server import compute_indicators

    rows = build_rows(ticker="AAA", count=30)
    payload = [{k: v for k, v in row.items()} for row in rows]
    with pytest.raises(ToolError, match="as_of"):
        compute_indicators(rows=payload, groups=["momentum"], as_of="2026-01-02")


def test_without_as_of_no_as_of_block_is_emitted(seeded_db):
    """The field must not appear when it was not asked for."""
    from mcp_server.server import compute_indicators

    result = compute_indicators(ticker="AAA", groups=["momentum"])
    assert "as_of" not in result
