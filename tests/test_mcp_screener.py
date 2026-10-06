"""Tests for screen_and_rank and scan_foreign_flow MCP tools."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from data import store
from mcp_server.server import server
from tests.conftest import build_rows

TICKER = "TST"


def seeded_rows() -> list[dict]:
    """40 rows of a steadily rising, internally consistent history."""
    closes = [10_000.0 + 100 * i for i in range(40)]
    return build_rows(
        ticker=TICKER,
        close=closes,
        total_volume=[1_000] * 40,
        buy_volume=[600] * 40,
        sell_volume=[400] * 40,
        foreign_room=[1_000_000 - 100 * i for i in range(40)],
        foreign_buy_volume=[100] * 40,
        foreign_sell_volume=[0] * 40,
        foreign_buy_value=[1_000_000.0] * 40,
        foreign_sell_value=[0.0] * 40,
    )


@pytest.fixture(autouse=True)
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the server at a throwaway database and audit log."""
    db_path = tmp_path / "ta.sqlite"
    monkeypatch.setenv("TA_AGENT_DB", str(db_path))
    monkeypatch.setenv("TA_AGENT_AUDIT_LOG", str(tmp_path / "tool_calls.jsonl"))
    conn = store.connect(db_path)
    try:
        store.upsert_rows(conn, seeded_rows())
    finally:
        conn.close()
    return tmp_path


def call(tool: str, arguments: dict) -> dict:
    result = asyncio.run(server.call_tool(tool, arguments))
    assert not result.is_error, result
    return json.loads(result.content[0].text)


def audit_entries(tmp_path: Path) -> list[dict]:
    path = tmp_path / "tool_calls.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_screen_and_rank_tool_with_db(isolated_env: Path):
    conn = store.connect(isolated_env / "ta.sqlite")
    try:
        rows_hpg = build_rows(
            count=40,
            ticker="HPG",
            close=[25_000.0 + 100 * i for i in range(40)],
            total_volume=[5_000] * 40,
        )
        store.upsert_rows(conn, rows_hpg)
    finally:
        conn.close()

    result = call("screen_and_rank", {"universe": ["HPG"], "strategy": "momentum_breakout", "top_n": 3})
    assert result["universe_size"] == 1
    assert result["strategy_used"] == "momentum_breakout"
    assert len(result["ranked_candidates"]) == 1
    assert result["ranked_candidates"][0]["symbol"] == "HPG"

    # Check audit log
    entries = audit_entries(isolated_env)
    assert any(e["tool"] == "screen_and_rank" for e in entries)


def test_screen_and_rank_tool_vn30_fallback(isolated_env: Path):
    # If vn30 is passed but only TST is in DB, fallback to available tickers in DB
    result = call("screen_and_rank", {"universe": "vn30", "strategy": "momentum_breakout", "top_n": 3})
    assert "ranked_candidates" in result
    assert result["universe_size"] >= 1
    assert result["ranked_candidates"][0]["symbol"] == "TST"


def test_scan_foreign_flow_tool(isolated_env: Path):
    conn = store.connect(isolated_env / "ta.sqlite")
    try:
        rows_vnm = build_rows(
            count=20,
            ticker="VNM",
            foreign_buy_value=[10_000_000_000.0] * 20,
            foreign_sell_value=[2_000_000_000.0] * 20,
            foreign_room=[100_000] * 20,  # low room warning
        )
        store.upsert_rows(conn, rows_vnm)
    finally:
        conn.close()

    result = call("scan_foreign_flow", {"universe": "VNM", "window_days": 5, "top_n": 5})
    assert "top_net_buyers" in result
    assert "top_net_sellers" in result
    assert len(result["top_net_buyers"]) >= 1
    assert result["top_net_buyers"][0]["symbol"] == "VNM"
    assert any(w["symbol"] == "VNM" for w in result["room_warnings"])

    # Check audit log
    entries = audit_entries(isolated_env)
    assert any(e["tool"] == "scan_foreign_flow" for e in entries)


def test_screener_tool_validation():
    with pytest.raises(ToolError):
        asyncio.run(server.call_tool("screen_and_rank", {"strategy": "invalid_strategy"}))

    with pytest.raises(ToolError):
        asyncio.run(server.call_tool("scan_foreign_flow", {"top_n": 50}))
