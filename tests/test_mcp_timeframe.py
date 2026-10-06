"""Tests for MCP server multi-timeframe ('1d' | '1h') support."""

from __future__ import annotations

import asyncio
import json
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from data import store
from data.provider import HourlyBar
from mcp_server.server import server

TICKER = "TST"


@pytest.fixture(autouse=True)
def setup_db(tmp_path, monkeypatch):
    db_path = tmp_path / "ta.sqlite"
    monkeypatch.setenv("TA_AGENT_DB", str(db_path))
    monkeypatch.setenv("TA_AGENT_AUDIT_LOG", str(tmp_path / "tool_calls.jsonl"))

    conn = store.connect(db_path)
    try:
        # Seed daily data
        store.upsert_rows(
            conn,
            [
                {
                    "ticker": TICKER,
                    "date": f"2026-10-0{i}",
                    "prev_close": 50000.0,
                    "open": 50200.0,
                    "high": 51000.0,
                    "low": 49800.0,
                    "close": 50500.0,
                    "total_trade": 1000,
                    "total_value": 10000000.0,
                    "total_volume": 200000,
                    "buy_count": 500,
                    "sell_count": 500,
                    "buy_volume": 100000,
                    "sell_volume": 100000,
                    "foreign_buy_volume": 0,
                    "foreign_sell_volume": 0,
                    "foreign_buy_value": 0.0,
                    "foreign_sell_value": 0.0,
                    "foreign_room": 1000000,
                }
                for i in range(1, 6)
            ],
        )
        # Seed hourly data (4 bars)
        hourly_bars = [
            HourlyBar(TICKER, "2026-10-06 09:00:00", "2026-10-06", 1, 50000.0, 50300.0, 49900.0, 50200.0, 50000, 2500000.0),
            HourlyBar(TICKER, "2026-10-06 10:00:00", "2026-10-06", 2, 50200.0, 50600.0, 50100.0, 50500.0, 60000, 3030000.0),
            HourlyBar(TICKER, "2026-10-06 13:00:00", "2026-10-06", 3, 50500.0, 50800.0, 50400.0, 50700.0, 40000, 2028000.0),
            HourlyBar(TICKER, "2026-10-06 14:00:00", "2026-10-06", 4, 50700.0, 51000.0, 50600.0, 50900.0, 70000, 3563000.0),
        ]
        store.upsert_hourly_bars(conn, hourly_bars)
    finally:
        conn.close()


def call(tool: str, arguments: dict) -> dict:
    result = asyncio.run(server.call_tool(tool, arguments))
    assert not result.is_error, result
    return json.loads(result.content[0].text)


def test_get_price_data_default_is_1d():
    res = call("get_price_data", {"ticker": TICKER, "lookback_days": 3})
    assert res["timeframe"] == "1d"
    assert res["row_count"] == 3


def test_get_price_data_timeframe_1h():
    res = call("get_price_data", {"ticker": TICKER, "timeframe": "1h", "lookback_days": 4})
    assert res["timeframe"] == "1h"
    assert res["row_count"] == 4
    assert res["rows"][0]["datetime"] == "2026-10-06 09:00:00"
    assert res["rows"][-1]["datetime"] == "2026-10-06 14:00:00"


def test_get_price_data_invalid_timeframe():
    with pytest.raises(ToolError, match="timeframe"):
        asyncio.run(server.call_tool("get_price_data", {"ticker": TICKER, "timeframe": "5m"}))


def test_compute_indicators_timeframe_1h():
    res = call(
        "compute_indicators",
        {"ticker": TICKER, "timeframe": "1h", "groups": ["trend"], "series_tail": 2},
    )
    assert res["ticker"] == TICKER
    assert res["timeframe"] == "1h"
    assert "trend" in res["groups"]
