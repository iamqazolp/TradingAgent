"""Store and ingestion tests.

The feed delivers every number as a string, sometimes with thousands separators
and sometimes blank, so the casts and the rejection policy are the part worth
testing hard. The generated fixtures are used here as end-to-end input.
"""

from __future__ import annotations

import json

import pytest

from data import ingest, store

RAW = {
    "Symbol": "vnm",
    "Date": "05/01/2026",
    "PricePreviousClose": "68,500",
    "PriceClose": "67,340",
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
    "ForeignerSellValue": "28,362,000,000",
    "CurrentForeignRoom": "1,046,000,000",
}


@pytest.fixture
def conn():
    connection = store.connect(":memory:")
    yield connection
    connection.close()


# --------------------------------------------------------------------------- casts


def test_parse_record_casts_every_string_field():
    row = ingest.parse_record(RAW)
    assert row["ticker"] == "VNM"  # upper-cased
    assert row["date"] == "2026-01-05"  # dd/mm/yyyy -> ISO
    assert row["close"] == pytest.approx(67_340.0)  # commas stripped
    assert row["total_value"] == pytest.approx(272_253_473_777.0)
    assert row["total_volume"] == 4_049_590
    assert isinstance(row["total_volume"], int)
    assert row["foreign_room"] == 1_046_000_000


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2026-01-05", "2026-01-05"),
        ("2026/01/05", "2026-01-05"),
        ("05/01/2026", "2026-01-05"),
        ("05-01-2026", "2026-01-05"),
        ("20260105", "2026-01-05"),
        ("2026-01-05T00:00:00", "2026-01-05"),
    ],
)
def test_parse_date_accepts_the_formats_the_feed_uses(raw, expected):
    assert ingest.parse_date(raw) == expected


def test_parse_date_rejects_nonsense():
    with pytest.raises(ingest.IngestError, match="not a recognised date"):
        ingest.parse_date("last Tuesday")
    with pytest.raises(ingest.IngestError, match="missing Date"):
        ingest.parse_date("")


@pytest.mark.parametrize("blank", ["", "  ", "-", "N/A", "null", None])
def test_blank_volume_fields_become_zero_not_a_rejection(blank):
    # A small cap with no foreign trading reports blanks. That is ordinary.
    row = ingest.parse_record(dict(RAW, ForeignerBuyQuantity=blank, ForeignerBuyValue=blank))
    assert row["foreign_buy_volume"] == 0
    assert row["foreign_buy_value"] == pytest.approx(0.0)


@pytest.mark.parametrize("field", ["PriceClose", "PricePreviousClose"])
@pytest.mark.parametrize("bad", ["", "0", "-100", "n/a"])
def test_missing_or_invalid_prices_reject_the_row(field, bad):
    # There is no honest substitute for a close, and 0 would poison OBV and every
    # return-based metric.
    with pytest.raises(ingest.IngestError):
        ingest.parse_record(dict(RAW, **{field: bad}))


def test_missing_symbol_rejects_the_row():
    with pytest.raises(ingest.IngestError, match="missing Symbol"):
        ingest.parse_record(dict(RAW, Symbol=""))


# --------------------------------------------------------------------------- reconciliation


def test_side_totals_that_do_not_add_up_are_warnings_not_failures():
    # buy+sell = 4,049,590 exactly in RAW; nudge the total so it no longer matches.
    report = ingest.parse_records([dict(RAW, TotalVolume="4,000,000")])
    assert len(report.rows) == 1  # the row is kept
    assert any("!= total_volume" in w for w in report.warnings)


def test_foreign_volume_above_total_volume_is_a_warning():
    report = ingest.parse_records([dict(RAW, ForeignerBuyQuantity="9,999,999,999")])
    assert len(report.rows) == 1
    assert any("exceeds total_volume" in w for w in report.warnings)


def test_reconciled_row_produces_no_warnings():
    reconciled = dict(
        RAW,
        BuyQuantity="2,000,000",
        SellQuantity="2,049,590",
        BuyCount="5,000",
        SellCount="5,770",
    )
    assert ingest.parse_records([reconciled]).warnings == []


# --------------------------------------------------------------------------- envelopes


def test_extract_records_unwraps_whatever_envelope_arrives():
    record = {"Symbol": "VNM"}
    assert ingest.extract_records([record]) == [record]
    assert ingest.extract_records({"Data": [record]}) == [record]
    assert ingest.extract_records({"result": {"items": [record]}}) == [record]
    # Some ASP.NET endpoints double-encode the body as a JSON string.
    assert ingest.extract_records({"d": json.dumps([record])}) == [record]
    assert ingest.extract_records({"Success": "true", "Data": []}) == []


def test_fetch_requires_a_configured_endpoint(monkeypatch):
    monkeypatch.delenv("TA_AGENT_API_URL", raising=False)
    with pytest.raises(ingest.IngestError, match="no live endpoint configured"):
        ingest.fetch_trading_statistics("VNM", "2026-01-01", "2026-01-31")


# --------------------------------------------------------------------------- store


def test_upsert_is_idempotent(conn):
    first = ingest.ingest_records([RAW], conn)
    assert first.written == 1
    assert store.row_count(conn, "VNM") == 1

    # Re-ingesting the same day must update in place, not duplicate it.
    again = ingest.ingest_records([RAW], conn)
    assert again.written == 1
    assert store.row_count(conn, "VNM") == 1

    revised = ingest.ingest_records([dict(RAW, PriceClose="70,000")], conn)
    assert revised.written == 1
    assert store.row_count(conn, "VNM") == 1
    assert store.get_recent(conn, "VNM", 1)[0]["close"] == pytest.approx(70_000.0)


def test_duplicate_records_in_one_payload_keep_the_last_and_warn(conn):
    report = ingest.ingest_records([RAW, dict(RAW, PriceClose="70,000")], conn)
    assert len(report.rows) == 1
    assert any("duplicate record" in w for w in report.warnings)
    assert store.get_recent(conn, "VNM", 1)[0]["close"] == pytest.approx(70_000.0)


def test_get_recent_returns_trading_rows_oldest_first(conn):
    raws = [
        dict(RAW, Date=f"0{day}/01/2026", PriceClose=f"{67_000 + day}")
        for day in (5, 6, 7, 8)
    ]
    ingest.ingest_records(raws, conn)
    rows = store.get_recent(conn, "VNM", 2)
    assert [r["date"] for r in rows] == ["2026-01-07", "2026-01-08"]
    assert store.get_recent(conn, "VNM", 0) == []


def test_get_range_is_inclusive_and_case_insensitive_on_ticker(conn):
    raws = [dict(RAW, Date=f"0{day}/01/2026") for day in (5, 6, 7, 8)]
    ingest.ingest_records(raws, conn)
    rows = store.get_range(conn, "vnm", "2026-01-06", "2026-01-07")
    assert [r["date"] for r in rows] == ["2026-01-06", "2026-01-07"]
    assert store.date_bounds(conn, "VNM") == ("2026-01-05", "2026-01-08")
    assert store.list_tickers(conn) == ["VNM"]


def test_unknown_ticker_reads_as_empty_not_an_error(conn):
    assert store.get_recent(conn, "NOPE", 10) == []
    assert store.date_bounds(conn, "NOPE") == (None, None)
    assert store.row_count(conn, "NOPE") == 0


def test_stored_rows_carry_exactly_the_engine_columns(conn):
    ingest.ingest_records([RAW], conn)
    row = store.get_recent(conn, "VNM", 1)[0]
    assert set(row) == set(store.COLUMNS)


# --------------------------------------------------------------------------- fixtures


def test_edge_case_fixture_ingests_with_the_expected_rejections(conn, edge_case_payload):
    records = ingest.extract_records(edge_case_payload)
    report = ingest.ingest_records(records, conn)
    # The fixture deliberately contains rows a careful parser must refuse.
    assert report.rejected, "the edge-case fixture should exercise the reject path"
    reasons = " ".join(reason for _, reason in report.rejected)
    assert "PriceClose" in reasons or "PricePreviousClose" in reasons
    assert report.written == len(report.rows)
    assert store.row_count(conn) == len(report.rows)


def test_sample_fixture_ingests_cleanly(conn, sample_payload):
    records = ingest.extract_records(sample_payload)
    report = ingest.ingest_records(records, conn)
    assert report.rejected == []
    assert report.tickers == ["HPG", "TNG", "VNM"]
    assert report.written == len(records)
    # Every stored close is a positive float, never a string and never zero.
    for ticker in report.tickers:
        for row in store.get_recent(conn, ticker, 5):
            assert isinstance(row["close"], float) and row["close"] > 0


# --------------------------------------------------------------------------- OHLC & Timeframes


def test_parse_record_accepts_explicit_ohlc_and_validates_geometry():
    raw_ohlc = dict(
        RAW,
        PriceOpen="67,500",
        PriceHigh="69,000",
        PriceLow="67,000",
        PriceClose="68,000",
        PricePreviousClose="67,200",
    )
    row = ingest.parse_record(raw_ohlc)
    assert row["open"] == pytest.approx(67_500.0)
    assert row["high"] == pytest.approx(69_000.0)
    assert row["low"] == pytest.approx(67_000.0)
    assert row["close"] == pytest.approx(68_000.0)


def test_parse_record_rejects_inverted_high_low():
    bad_ohlc = dict(
        RAW,
        PriceHigh="65,000",
        PriceLow="69,000",
        PriceClose="68,000",
    )
    with pytest.raises(ingest.IngestError, match="cannot be lower than Low"):
        ingest.parse_record(bad_ohlc)


def test_store_timeframe_queries(conn):
    raws = [
        dict(
            RAW,
            Date=f"{day:02d}/01/2026",
            PricePreviousClose="67,000",
            PriceOpen=f"{67_000 + 100 * day}",
            PriceHigh=f"{68_000 + 100 * day}",
            PriceLow=f"{66_000 + 100 * day}",
            PriceClose=f"{67_500 + 100 * day}",
            TotalVolume="1,000",
        )
        for day in range(5, 15)
    ]
    ingest.ingest_records(raws, conn)
    # Query weekly bars
    weekly_recent = store.get_recent(conn, "VNM", 5, timeframe="1W")
    assert len(weekly_recent) == 2
    assert "open" in weekly_recent[0] and "high" in weekly_recent[0]
    assert weekly_recent[0]["high"] >= weekly_recent[0]["close"]

    # Query 3D bars
    bars_3d = store.get_range(conn, "VNM", "2026-01-05", "2026-01-14", timeframe="3D")
    assert len(bars_3d) >= 3


def test_dotenv_loading(tmp_path, monkeypatch):
    import os
    from dotenv import load_dotenv
    env_file = tmp_path / ".env"
    env_file.write_text("TA_AGENT_TEST_KEY=secret_123\nTA_AGENT_API_TOKEN=custom_token\n", encoding="utf-8")
    monkeypatch.delenv("TA_AGENT_TEST_KEY", raising=False)
    monkeypatch.delenv("TA_AGENT_API_TOKEN", raising=False)
    load_dotenv(env_file)
    assert os.environ.get("TA_AGENT_TEST_KEY") == "secret_123"
    assert os.environ.get("TA_AGENT_API_TOKEN") == "custom_token"


def test_dual_source_non_destructive_merge(conn):
    # Source 2 arrives first: Market OHLC + Volume + Buy/Sell Flow (no foreign)
    source2_market_record = {
        "Symbol": "HPG",
        "Date": "15/01/2026",
        "PricePreviousClose": "27,000",
        "PriceOpen": "27,200",
        "PriceHigh": "28,000",
        "PriceLow": "26,900",
        "PriceClose": "27,800",
        "TotalVolume": "10,000,000",
        "TotalTrade": "5,000",
        "BuyQuantity": "6,000,000",
        "SellQuantity": "4,000,000",
    }
    ingest.ingest_records([source2_market_record], conn)
    row = store.get_range(conn, "HPG", "2026-01-15", "2026-01-15")[0]
    assert row["open"] == 27_200.0
    assert row["high"] == 28_000.0
    assert row["low"] == 26_900.0
    assert row["close"] == 27_800.0
    assert row["buy_volume"] == 6_000_000
    assert row["foreign_buy_volume"] == 0

    # Source 1 arrives second: Foreign Flow & Room (no OHLC)
    source1_foreign_record = {
        "Symbol": "HPG",
        "Date": "15/01/2026",
        "PricePreviousClose": "27,000",
        "PriceClose": "27,800",
        "ForeignerBuyQuantity": "1,500,000",
        "ForeignerSellQuantity": "500,000",
        "ForeignerBuyValue": "41,700,000,000",
        "ForeignerSellValue": "13,900,000,000",
        "CurrentForeignRoom": "500,000,000",
    }
    ingest.ingest_records([source1_foreign_record], conn)
    merged = store.get_range(conn, "HPG", "2026-01-15", "2026-01-15")[0]

    # OHLC & flow from Source 2 are PRESERVED:
    assert merged["open"] == 27_200.0
    assert merged["high"] == 28_000.0
    assert merged["low"] == 26_900.0
    assert merged["close"] == 27_800.0
    assert merged["buy_volume"] == 6_000_000

    # Foreign stats from Source 1 are UPDATED:
    assert merged["foreign_buy_volume"] == 1_500_000
    assert merged["foreign_sell_volume"] == 500_000
    assert merged["foreign_buy_value"] == 41_700_000_000.0
    assert merged["foreign_room"] == 500_000_000

    # Re-running Source 2 does NOT overwrite foreign stats:
    ingest.ingest_records([source2_market_record], conn)
    re_merged = store.get_range(conn, "HPG", "2026-01-15", "2026-01-15")[0]
    assert re_merged["open"] == 27_200.0
    assert re_merged["foreign_buy_volume"] == 1_500_000
    assert re_merged["foreign_room"] == 500_000_000

