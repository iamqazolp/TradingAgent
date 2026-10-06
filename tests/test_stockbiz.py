"""Tests for Stockbiz SOAP client, schema migration, and OHLCV ingestion."""

from __future__ import annotations

import io
import sqlite3
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from data import ingest, store
from data.stockbiz import StockbizClient, StockbizError, parse_response

# Mock SOAP responses
TRADING_STATS_XML = """<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <GetTradingStatisticsResponse xmlns="http://datafeed.stockbiz.vn/">
      <GetTradingStatisticsResult>
        <TradingStatistics>
          <Symbol>VNM</Symbol>
          <Date>2026-01-05T00:00:00</Date>
          <PricePreviousClose>68500</PricePreviousClose>
          <PriceOpen>68000</PriceOpen>
          <PriceHigh>69000</PriceHigh>
          <PriceLow>67500</PriceLow>
          <PriceClose>67340</PriceClose>
          <TotalTrade>10770</TotalTrade>
          <TotalValue>272253473777</TotalValue>
          <TotalVolume>4049590</TotalVolume>
          <BuyCount>4925</BuyCount>
          <SellCount>5845</SellCount>
          <BuyQuantity>1608970</BuyQuantity>
          <SellQuantity>2440620</SellQuantity>
          <ForeignerBuyQuantity>148547</ForeignerBuyQuantity>
          <ForeignerSellQuantity>421300</ForeignerSellQuantity>
          <ForeignerBuyValue>10001000000</ForeignerBuyValue>
          <ForeignerSellValue>28362000000</ForeignerSellValue>
          <CurrentForeignRoom>1046000000</CurrentForeignRoom>
        </TradingStatistics>
      </GetTradingStatisticsResult>
    </GetTradingStatisticsResponse>
  </soap:Body>
</soap:Envelope>
"""

HISTORICAL_QUOTES_XML = """<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <GetHistoricalQuotesResponse xmlns="http://datafeed.stockbiz.vn/">
      <GetHistoricalQuotesResult>
        <HistoricalQuote>
          <Symbol>HPG</Symbol>
          <Date>2026-01-05T00:00:00</Date>
          <Open>28000</Open>
          <High>28500</High>
          <Low>27800</Low>
          <Close>28300</Close>
          <Volume>15000000</Volume>
        </HistoricalQuote>
      </GetHistoricalQuotesResult>
    </GetHistoricalQuotesResponse>
  </soap:Body>
</soap:Envelope>
"""

MARKET_INFO_XML = """<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <GetAllMarketInfoResponse xmlns="http://datafeed.stockbiz.vn/">
      <GetAllMarketInfoResult>
        <MarketInfo>
          <Exchange>VNINDEX</Exchange>
          <Date>2026-01-05T00:00:00</Date>
          <IndexCurrent>1250.5</IndexCurrent>
          <IndexChange>5.2</IndexChange>
          <IndexPercentChange>0.42</IndexPercentChange>
          <TotalTrade>150000</TotalTrade>
          <TotalValue>18500000000000</TotalValue>
          <TotalVolume>750000000</TotalVolume>
          <Advances>230</Advances>
          <Declines>150</Declines>
          <Unchanged>60</Unchanged>
        </MarketInfo>
        <MarketInfo>
          <Exchange>HNX</Exchange>
          <Date>2026-01-05T00:00:00</Date>
          <IndexCurrent>235.1</IndexCurrent>
          <IndexChange>-1.5</IndexChange>
          <IndexPercentChange>-0.63</IndexPercentChange>
          <TotalTrade>45000</TotalTrade>
          <TotalValue>2100000000000</TotalValue>
          <TotalVolume>120000000</TotalVolume>
          <Advances>70</Advances>
          <Declines>95</Declines>
          <Unchanged>55</Unchanged>
        </MarketInfo>
      </GetAllMarketInfoResult>
    </GetAllMarketInfoResponse>
  </soap:Body>
</soap:Envelope>
"""

SOAP_FAULT_XML = """<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <soap:Fault>
      <faultcode>soap:Server</faultcode>
      <faultstring>Authentication failed: invalid credentials</faultstring>
    </soap:Fault>
  </soap:Body>
</soap:Envelope>
"""


@pytest.fixture
def client():
    return StockbizClient(username="test_user", password="test_password")


@pytest.fixture
def conn():
    connection = store.connect(":memory:")
    yield connection
    connection.close()


# --------------------------------------------------------------------------- SOAP Client Tests


def test_build_soap_envelope(client):
    inner = "<symbol>VNM</symbol>"
    envelope = client.build_soap_envelope("GetTradingStatistics", inner)
    assert 'xmlns="http://datafeed.stockbiz.vn/"' in envelope
    assert "<username>test_user</username>" in envelope
    assert "<password>test_password</password>" in envelope
    assert "<GetTradingStatistics" in envelope
    assert "<symbol>VNM</symbol>" in envelope


def test_parse_response_trading_statistics():
    records = parse_response(TRADING_STATS_XML, "TradingStatistics")
    assert len(records) == 1
    rec = records[0]
    assert rec["Symbol"] == "VNM"
    assert rec["PriceOpen"] == "68000"
    assert rec["PriceHigh"] == "69000"
    assert rec["PriceLow"] == "67500"
    assert rec["PriceClose"] == "67340"


def test_parse_response_historical_quotes():
    records = parse_response(HISTORICAL_QUOTES_XML, "HistoricalQuote")
    assert len(records) == 1
    rec = records[0]
    assert rec["Symbol"] == "HPG"
    assert rec["Open"] == "28000"
    assert rec["High"] == "28500"
    assert rec["Low"] == "27800"
    assert rec["Close"] == "28300"


def test_parse_response_market_info():
    records = parse_response(MARKET_INFO_XML, "MarketInfo")
    assert len(records) == 2
    assert records[0]["Exchange"] == "VNINDEX"
    assert records[0]["IndexCurrent"] == "1250.5"
    assert records[1]["Exchange"] == "HNX"
    assert records[1]["IndexCurrent"] == "235.1"


def test_parse_response_soap_fault():
    with pytest.raises(StockbizError, match="Authentication failed"):
        parse_response(SOAP_FAULT_XML, "TradingStatistics")


def test_client_get_trading_statistics(client):
    mock_resp = MagicMock()
    mock_resp.read.return_value = TRADING_STATS_XML.encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
        records = client.get_trading_statistics("vnm", "2026-01-01", "2026-01-05")
        assert len(records) == 1
        assert records[0]["Symbol"] == "VNM"
        req = mock_urlopen.call_args[0][0]
        assert req.headers["Soapaction"] == '"http://datafeed.stockbiz.vn/GetTradingStatistics"'
        body = req.data.decode("utf-8")
        assert "<startDate>2026-01-01T00:00:00</startDate>" in body
        assert "<endDate>2026-01-05T00:00:00</endDate>" in body


def test_client_get_latest_trading_statistics(client):
    mock_resp = MagicMock()
    mock_resp.read.return_value = TRADING_STATS_XML.encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
        records = client.get_latest_trading_statistics("VNM")
        assert len(records) == 1
        req = mock_urlopen.call_args[0][0]
        assert req.headers["Soapaction"] == '"http://datafeed.stockbiz.vn/GetLastestTradingStatistics"'


def test_client_get_all_market_info(client):
    mock_resp = MagicMock()
    mock_resp.read.return_value = MARKET_INFO_XML.encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp):
        records = client.get_all_market_info()
        assert len(records) == 2
        assert records[0]["Exchange"] == "VNINDEX"


def test_client_network_error(client):
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("Connection refused")):
        with pytest.raises(StockbizError, match="Network error"):
            client.get_trading_statistics("VNM", "2026-01-01", "2026-01-05")


def test_client_http_500_soap_fault(client):
    fp = io.BytesIO(SOAP_FAULT_XML.encode("utf-8"))
    http_error = urllib.error.HTTPError(
        url="http://datafeed.stockbiz.vn/MarketDataService.asmx",
        code=500,
        msg="Internal Server Error",
        hdrs={},
        fp=fp,
    )
    with patch("urllib.request.urlopen", side_effect=http_error):
        with pytest.raises(StockbizError, match="Authentication failed"):
            client.get_trading_statistics("VNM", "2026-01-01", "2026-01-05")


# --------------------------------------------------------------------------- Ingestion Tests


def test_cast_price_optional():
    assert ingest.cast_price_optional("68,000", "PriceOpen") == pytest.approx(68000.0)
    assert ingest.cast_price_optional("", "PriceOpen") is None
    assert ingest.cast_price_optional(None, "PriceOpen") is None
    assert ingest.cast_price_optional("N/A", "PriceOpen") is None

    with pytest.raises(ingest.IngestError, match="not a positive price"):
        ingest.cast_price_optional("0", "PriceOpen")
    with pytest.raises(ingest.IngestError, match="not a positive price"):
        ingest.cast_price_optional("-500", "PriceOpen")


def test_parse_record_with_ohlcv():
    raw = {
        "Symbol": "VNM",
        "Date": "2026-01-05",
        "PricePreviousClose": "68500",
        "PriceOpen": "68000",
        "PriceHigh": "69000",
        "PriceLow": "67500",
        "PriceClose": "67340",
    }
    row = ingest.parse_record(raw)
    assert row["ticker"] == "VNM"
    assert row["open"] == pytest.approx(68000.0)
    assert row["high"] == pytest.approx(69000.0)
    assert row["low"] == pytest.approx(67500.0)
    assert row["close"] == pytest.approx(67340.0)


def test_parse_record_backward_compatibility_close_only():
    raw = {
        "Symbol": "VNM",
        "Date": "2026-01-05",
        "PricePreviousClose": "68500",
        "PriceClose": "67340",
    }
    row = ingest.parse_record(raw)
    assert row["open"] is None
    assert row["high"] is None
    assert row["low"] is None
    assert row["close"] == pytest.approx(67340.0)


def test_fetch_stockbiz_statistics(client):
    mock_resp = MagicMock()
    mock_resp.read.return_value = TRADING_STATS_XML.encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp):
        records = ingest.fetch_stockbiz_statistics(client, "VNM", "2026-01-01", "2026-01-05")
        assert len(records) == 1
        assert records[0]["Symbol"] == "VNM"


# --------------------------------------------------------------------------- Store & Migration Tests


def test_store_migration_adds_ohlcv_columns():
    raw_conn = sqlite3.connect(":memory:")
    raw_conn.row_factory = sqlite3.Row
    # Simulate legacy table without open, high, low
    raw_conn.execute("""
        CREATE TABLE daily_prices (
            ticker TEXT NOT NULL,
            date TEXT NOT NULL,
            prev_close REAL NOT NULL,
            close REAL NOT NULL,
            total_trade INTEGER NOT NULL,
            total_value REAL NOT NULL,
            total_volume INTEGER NOT NULL,
            buy_count INTEGER NOT NULL,
            sell_count INTEGER NOT NULL,
            buy_volume INTEGER NOT NULL,
            sell_volume INTEGER NOT NULL,
            foreign_buy_volume INTEGER NOT NULL,
            foreign_sell_volume INTEGER NOT NULL,
            foreign_buy_value REAL NOT NULL,
            foreign_sell_value REAL NOT NULL,
            foreign_room INTEGER NOT NULL,
            PRIMARY KEY (ticker, date)
        )
    """)
    raw_conn.commit()

    # Call init_db to migrate
    store.init_db(raw_conn)

    cols = {row[1] for row in raw_conn.execute("PRAGMA table_info(daily_prices)").fetchall()}
    assert "open" in cols
    assert "high" in cols
    assert "low" in cols
    raw_conn.close()


def test_store_upsert_rows_with_old_and_new_dicts(conn):
    old_row = {
        "ticker": "VNM",
        "date": "2026-01-05",
        "prev_close": 68500.0,
        "close": 67340.0,
        "total_trade": 1000,
        "total_value": 10000000.0,
        "total_volume": 10000,
        "buy_count": 500,
        "sell_count": 500,
        "buy_volume": 5000,
        "sell_volume": 5000,
        "foreign_buy_volume": 0,
        "foreign_sell_volume": 0,
        "foreign_buy_value": 0.0,
        "foreign_sell_value": 0.0,
        "foreign_room": 1000000,
    }
    assert store.upsert_rows(conn, [old_row]) == 1
    saved = store.get_recent(conn, "VNM", 1)[0]
    assert saved["close"] == pytest.approx(67340.0)
    assert saved["open"] is None

    new_row = dict(old_row, date="2026-01-06", open=68000.0, high=69000.0, low=67500.0)
    assert store.upsert_rows(conn, [new_row]) == 1
    saved_new = store.get_recent(conn, "VNM", 1)[0]
    assert saved_new["open"] == pytest.approx(68000.0)
    assert saved_new["high"] == pytest.approx(69000.0)
    assert saved_new["low"] == pytest.approx(67500.0)


def test_store_upsert_and_query_market_indices(conn):
    rows = [
        {
            "exchange": "VNINDEX",
            "date": "2026-01-05",
            "index_current": 1250.5,
            "index_change": 5.2,
            "index_percent_change": 0.42,
            "total_trade": 150000,
            "total_value": 18500000000000.0,
            "total_volume": 750000000,
            "advances": 230,
            "declines": 150,
            "unchanged": 60,
        },
        {
            "exchange": "VNINDEX",
            "date": "2026-01-06",
            "index_current": 1258.0,
            "index_change": 7.5,
            "index_percent_change": 0.60,
            "total_trade": 160000,
            "total_value": 19500000000000.0,
            "total_volume": 800000000,
            "advances": 250,
            "declines": 120,
            "unchanged": 50,
        },
        {
            "exchange": "HNX",
            "date": "2026-01-06",
            "index_current": 235.1,
            "index_change": -1.5,
            "index_percent_change": -0.63,
            "total_trade": 45000,
            "total_value": 2100000000000.0,
            "total_volume": 120000000,
            "advances": 70,
            "declines": 95,
            "unchanged": 55,
        },
    ]
    assert store.upsert_market_indices(conn, rows) == 3

    # get_latest_market_indices for specific exchange
    latest_vn = store.get_latest_market_indices(conn, "VNINDEX")
    assert len(latest_vn) == 1
    assert latest_vn[0]["date"] == "2026-01-06"
    assert latest_vn[0]["index_current"] == pytest.approx(1258.0)

    # get_latest_market_indices for all exchanges
    latest_all = store.get_latest_market_indices(conn)
    assert len(latest_all) == 2
    exchanges = {r["exchange"] for r in latest_all}
    assert exchanges == {"HNX", "VNINDEX"}

    # get_market_indices_range
    vn_range = store.get_market_indices_range(conn, "VNINDEX", "2026-01-05", "2026-01-05")
    assert len(vn_range) == 1
    assert vn_range[0]["date"] == "2026-01-05"
