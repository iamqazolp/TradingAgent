"""Tests for the standalone mock data API server (mock_server)."""

from __future__ import annotations

import json
import threading
import urllib.parse
import urllib.request

import pytest

from data import ingest, store
from tests.mocks import mock_api as mock_server_module


@pytest.fixture(scope="module")
def mock_server():
    server = mock_server_module.make_server(port=0, token="test_secret_token")
    host, port = server.server_address[:2]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://{host}:{port}"
    yield base_url
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def _get(url: str, token: str | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return resp.status, data
    except urllib.error.HTTPError as exc:
        data = json.loads(exc.read().decode("utf-8"))
        return exc.code, data


def test_mock_server_health_check(mock_server):
    status, data = _get(f"{mock_server}/health")
    assert status == 200
    assert data["status"] == "ok"
    assert "/api/foreign_flow" in data["endpoints"]
    assert "/api/market_bars" in data["endpoints"]


def test_mock_server_token_auth(mock_server):
    # Missing token -> 401
    status, data = _get(f"{mock_server}/api/foreign_flow?symbol=VNM&fromDate=2026-01-01&toDate=2026-01-10")
    assert status == 401
    assert "token" in data["Message"]

    # Valid token -> 200
    status, data = _get(
        f"{mock_server}/api/foreign_flow?symbol=VNM&fromDate=2026-01-01&toDate=2026-01-10",
        token="test_secret_token",
    )
    assert status == 200
    assert data["Success"] is True
    assert len(data["Data"]) > 0


def test_mock_foreign_flow_endpoint(mock_server):
    status, data = _get(
        f"{mock_server}/api/foreign_flow?symbol=HPG&fromDate=2026-01-05&toDate=2026-01-16",
        token="test_secret_token",
    )
    assert status == 200
    records = data["Data"]
    assert len(records) >= 8  # 2 business weeks
    first = records[0]
    assert first["Symbol"] == "HPG"
    assert "Date" in first
    assert "PriceClose" in first
    assert "PricePreviousClose" in first
    assert "CurrentForeignRoom" in first


def test_mock_market_bars_endpoint_daily_and_hourly(mock_server):
    # Daily bars
    status, data_daily = _get(
        f"{mock_server}/api/market_bars?symbol=VNM&fromDate=2026-01-05&toDate=2026-01-09&timeframe=1D",
        token="test_secret_token",
    )
    assert status == 200
    daily_records = data_daily["Data"]
    assert len(daily_records) == 5
    for bar in daily_records:
        assert float(bar["PriceHigh"]) >= max(float(bar["PriceOpen"]), float(bar["PriceClose"]))
        assert float(bar["PriceLow"]) <= min(float(bar["PriceOpen"]), float(bar["PriceClose"]))
        assert int(bar["TotalVolume"]) > 0
        assert int(bar["BuyQuantity"]) + int(bar["SellQuantity"]) == int(bar["TotalVolume"])

    # Hourly bars
    status, data_hourly = _get(
        f"{mock_server}/api/market_bars?symbol=VNM&fromDate=2026-01-05&toDate=2026-01-09&timeframe=1H",
        token="test_secret_token",
    )
    assert status == 200
    hourly_records = data_hourly["Data"]
    assert len(hourly_records) == 25  # 5 sessions/day * 5 days
    assert ":" in hourly_records[0]["Date"]  # contains time component


def test_mock_server_dual_source_ingestion_end_to_end(mock_server, tmp_path, monkeypatch):
    db_file = tmp_path / "test_dual.sqlite"
    conn = store.connect(db_file)

    market_url = f"{mock_server}/api/market_bars"
    foreign_url = f"{mock_server}/api/foreign_flow"

    monkeypatch.setenv("TA_AGENT_MARKET_API_URL", market_url)
    monkeypatch.setenv("TA_AGENT_MARKET_API_TOKEN", "test_secret_token")
    monkeypatch.setenv("TA_AGENT_FOREIGN_API_URL", foreign_url)
    monkeypatch.setenv("TA_AGENT_FOREIGN_API_TOKEN", "test_secret_token")

    # Fetch both sources
    raws = ingest.fetch_trading_statistics("FPT", "2026-01-05", "2026-01-09", source="all")
    assert len(raws) > 0

    # Ingest into SQLite
    report = ingest.ingest_records(raws, conn)
    assert report.written > 0

    # Query merged daily rows
    rows = store.get_range(conn, "FPT", "2026-01-05", "2026-01-09", timeframe="1D")
    assert len(rows) == 5
    for r in rows:
        assert r["open"] > 0
        assert r["high"] >= max(r["open"], r["close"])
        assert r["low"] <= min(r["open"], r["close"])
        assert r["total_volume"] > 0
        assert r["foreign_room"] > 0

    conn.close()
