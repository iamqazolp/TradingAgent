"""The live-feed path, exercised over real HTTP against `scripts/mock_feed.py`.

Nothing here touches the real endpoint. The point is that switching to it needs
no code change: `TA_AGENT_API_URL`, an optional `TA_AGENT_API_TOKEN` and, if the
endpoint spells its query parameters differently, `TA_AGENT_API_PARAMS`. These
tests also pin the failure messages, because a wrong token or a renamed
parameter is the most likely thing to go wrong at that seam and the operator has
to be able to read what happened.
"""

from __future__ import annotations

import importlib.util
import json
import threading
from pathlib import Path

import pytest

from data import ingest, store

REPO_ROOT = Path(__file__).resolve().parent.parent


from tests.mocks import mock_feed

RECORDS = [
    {
        "Symbol": "VNM",
        "Date": f"{day:02d}/01/2026",
        "PricePreviousClose": "68,500" if day == 5 else f"{67_000 + 100 * (day - 6):,}",
        "PriceClose": f"{67_000 + 100 * (day - 5):,}",
        "TotalTrade": "10,770",
        "TotalValue": "272,253,473,777",
        "TotalVolume": "4,049,590",
        "BuyCount": "4,925",
        "SellCount": "5,845",
        "BuyQuantity": "1,608,970",
        "SellQuantity": "2,440,620",
        "ForeignerBuyQuantity": "148,547",
        "ForeignerSellQuantity": "421,300",
        "ForeignerBuyValue": "10,001,000,000",
        "ForeignerSellValue": "28,300,000,000",
        "CurrentForeignRoom": "358,656,570",
    }
    for day in (5, 6, 7, 8)
]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv("TA_AGENT_MARKET_API_URL", raising=False)
    monkeypatch.delenv("TA_AGENT_FOREIGN_API_URL", raising=False)


@pytest.fixture
def feed():
    """A running mock endpoint, parameterised through `request.param`-style kwargs."""

    servers = []

    def start(**kwargs):
        # Port 0: the OS picks a free one, so parallel runs cannot collide.
        httpd = mock_feed.make_server(0, RECORDS, **kwargs)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        servers.append((httpd, thread))
        host, port = httpd.server_address[:2]
        return f"http://{host}:{port}/GetTradingStatistics"

    yield start
    for httpd, thread in servers:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "ta.sqlite"
    monkeypatch.setenv("TA_AGENT_DB", str(path))
    return path


def test_a_live_fetch_ingests_and_reads_back(feed, db, monkeypatch):
    monkeypatch.setenv("TA_AGENT_API_URL", feed())
    raws = ingest.fetch_trading_statistics("vnm", "2026-01-01", "2026-01-31")
    assert len(raws) == 4
    report = ingest.ingest_records(raws, db_path=db)
    assert report.written == 4 and report.rejected == []

    conn = store.connect(db)
    try:
        rows = store.get_recent(conn, "VNM", 4)
    finally:
        conn.close()
    assert [row["date"] for row in rows] == [
        "2026-01-05",
        "2026-01-06",
        "2026-01-07",
        "2026-01-08",
    ]
    # dd/mm/yyyy in, ISO out; comma-separated strings in, floats out.
    assert rows[-1]["close"] == pytest.approx(67_300.0)
    assert rows[0]["total_value"] == pytest.approx(272_253_473_777.0)


def test_the_requested_date_range_is_what_the_endpoint_receives(feed, monkeypatch):
    monkeypatch.setenv("TA_AGENT_API_URL", feed())
    raws = ingest.fetch_trading_statistics("VNM", "2026-01-06", "2026-01-07")
    assert [r["Date"] for r in raws] == ["06/01/2026", "07/01/2026"]


def test_a_bearer_token_is_sent_when_configured(feed, monkeypatch):
    url = feed(token="s3cret")
    monkeypatch.setenv("TA_AGENT_API_URL", url)
    monkeypatch.setenv("TA_AGENT_API_TOKEN", "s3cret")
    assert len(ingest.fetch_trading_statistics("VNM", "2026-01-01", "2026-01-31")) == 4


def test_a_wrong_token_names_the_variable_to_fix(feed, monkeypatch):
    monkeypatch.setenv("TA_AGENT_API_URL", feed(token="s3cret"))
    monkeypatch.setenv("TA_AGENT_API_TOKEN", "wrong")
    with pytest.raises(ingest.IngestError) as exc:
        ingest.fetch_trading_statistics("VNM", "2026-01-01", "2026-01-31")
    message = str(exc.value)
    assert "HTTP 401" in message
    assert "TA_AGENT_API_TOKEN" in message
    assert "bearer token" in message  # the endpoint's own words are relayed


def test_differently_named_query_parameters_are_remapped(feed, monkeypatch):
    url = feed(param_style="alt")
    monkeypatch.setenv("TA_AGENT_API_URL", url)
    monkeypatch.delenv("TA_AGENT_API_PARAMS", raising=False)
    with pytest.raises(ingest.IngestError, match="TA_AGENT_API_PARAMS"):
        ingest.fetch_trading_statistics("VNM", "2026-01-01", "2026-01-31")

    monkeypatch.setenv(
        "TA_AGENT_API_PARAMS",
        json.dumps({"symbol": "stockCode", "fromDate": "from", "toDate": "to"}),
    )
    assert len(ingest.fetch_trading_statistics("VNM", "2026-01-01", "2026-01-31")) == 4


def test_the_double_encoded_aspnet_envelope_is_unwrapped(feed, monkeypatch):
    monkeypatch.setenv("TA_AGENT_API_URL", feed(envelope_style="aspnet"))
    raws = ingest.fetch_trading_statistics("VNM", "2026-01-01", "2026-01-31")
    assert len(raws) == 4
    assert raws[0]["Symbol"] == "VNM"


def test_an_unreachable_endpoint_is_a_readable_error(monkeypatch):
    # Port 9 (discard) with nothing listening: connection refused, not a traceback.
    monkeypatch.setenv("TA_AGENT_API_URL", "http://127.0.0.1:9/GetTradingStatistics")
    with pytest.raises(ingest.IngestError, match="cannot reach"):
        ingest.fetch_trading_statistics("VNM", "2026-01-01", "2026-01-31")


def test_an_html_response_is_reported_as_not_json(feed, monkeypatch, tmp_path):
    # The classic live-feed failure: a login or WAF page with HTTP 200.
    class HtmlHandler(mock_feed.Handler):
        def do_GET(self):  # noqa: N802
            body = b"<html><body>Session expired, please sign in</body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    from http.server import ThreadingHTTPServer

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), HtmlHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = httpd.server_address[:2]
        monkeypatch.setenv("TA_AGENT_API_URL", f"http://{host}:{port}/GetTradingStatistics")
        with pytest.raises(ingest.IngestError) as exc:
            ingest.fetch_trading_statistics("VNM", "2026-01-01", "2026-01-31")
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
    assert "not JSON" in str(exc.value)
    assert "Session expired" in str(exc.value)


def test_no_endpoint_configured_says_what_to_set(monkeypatch):
    monkeypatch.delenv("TA_AGENT_API_URL", raising=False)
    with pytest.raises(ingest.IngestError, match="TA_AGENT_API_URL"):
        ingest.fetch_trading_statistics("VNM", "2026-01-01", "2026-01-31")


def test_a_live_fetch_is_idempotent_on_re_ingest(feed, db, monkeypatch):
    monkeypatch.setenv("TA_AGENT_API_URL", feed())
    raws = ingest.fetch_trading_statistics("VNM", "2026-01-01", "2026-01-31")
    ingest.ingest_records(raws, db_path=db)
    ingest.ingest_records(raws, db_path=db)
    conn = store.connect(db)
    try:
        assert store.row_count(conn, "VNM") == 4
    finally:
        conn.close()


def test_the_cli_reports_a_feed_failure_without_a_traceback(feed, db, monkeypatch, capsys):
    monkeypatch.setenv("TA_AGENT_API_URL", feed(token="s3cret"))
    monkeypatch.delenv("TA_AGENT_API_TOKEN", raising=False)
    code = ingest.main(["--ticker", "VNM", "--start", "2026-01-01", "--end", "2026-01-31"])
    assert code == 1
    assert "ingest failed" in capsys.readouterr().err
