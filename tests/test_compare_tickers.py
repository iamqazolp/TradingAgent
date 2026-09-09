"""Unit and integration tests for multi-ticker comparison."""

from __future__ import annotations

import pytest

from indicators.comparison import compare_multiple_tickers
from tests.conftest import build_rows


def test_compare_multiple_tickers_basic():
    rows_a = build_rows(ticker="AAA", close=[10_000.0 + i * 50 for i in range(50)])
    rows_b = build_rows(ticker="BBB", close=[20_000.0 - i * 30 for i in range(50)])

    res = compare_multiple_tickers({"AAA": rows_a, "BBB": rows_b}, window_days=40)
    assert res["tickers"] == ["AAA", "BBB"]
    assert "table_52w" in res
    assert len(res["table_52w"]) == 2
    assert "table_ma" in res
    assert len(res["table_ma"]) == 2
    assert "table_pivots" in res
    assert len(res["table_pivots"]) == 2
    assert "relative_assessment" in res
    assert "disclaimer" in res


def test_mcp_server_compare_tickers(monkeypatch):
    from data import store
    from mcp_server.server import compare_tickers

    rows_hpg = build_rows(ticker="HPG", count=50)
    rows_vnm = build_rows(ticker="VNM", count=50)

    monkeypatch.setattr(store, "list_tickers", lambda conn: ["HPG", "VNM"])
    monkeypatch.setattr(
        store,
        "get_recent",
        lambda conn, ticker, lookback: rows_hpg if ticker == "HPG" else rows_vnm,
    )

    result = compare_tickers(tickers=["HPG", "VNM"], lookback_days=40)
    assert "table_52w" in result
    assert "table_ma" in result
    assert "table_pivots" in result
    assert len(result["table_52w"]) == 2
