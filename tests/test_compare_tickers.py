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
    assert res["tickers_compared"] == ["AAA", "BBB"]
    assert "table_52w" in res
    assert len(res["table_52w"]) == 2
    assert "table_ma" in res
    assert len(res["table_ma"]) == 2
    assert "table_levels" in res
    assert len(res["table_levels"]) == 2
    assert "relative_assessment" in res


def test_compare_populates_rsi_for_every_ticker():
    """RSI must reach the comparison table.

    The momentum group keys RSI by window (`rsi_14`), so a consumer reading a
    literal "rsi" got None for every ticker and the RSI column was silently
    always empty.
    """
    rows_a = build_rows(ticker="AAA", close=[10_000.0 + i * 50 for i in range(60)])
    rows_b = build_rows(ticker="BBB", close=[20_000.0 - i * 30 for i in range(60)])

    res = compare_multiple_tickers({"AAA": rows_a, "BBB": rows_b}, window_days=50)
    for row in res["table_ma"]:
        assert row["rsi"] is not None, f"RSI missing for {row['ticker']}"
        assert 0 <= row["rsi"] <= 100
        assert row["rsi_zone"] in ("oversold", "neutral", "overbought")


def test_compare_assesses_every_ticker_not_just_the_first_two():
    """A 3-way comparison must mention all three tickers.

    The assessment used to index [0] and [1] only, so the third ticker appeared
    in the tables and in no observation at all.
    """
    data = {
        "AAA": build_rows(ticker="AAA", close=[10_000.0 + i * 50 for i in range(60)]),
        "BBB": build_rows(ticker="BBB", close=[20_000.0 - i * 30 for i in range(60)]),
        "CCC": build_rows(ticker="CCC", close=[15_000.0 + (i % 7) * 40 for i in range(60)]),
    }
    res = compare_multiple_tickers(data, window_days=50)

    assert res["relative_assessment"]["tickers_assessed"] == ["AAA", "BBB", "CCC"]
    blob = res["relative_assessment"]["summary"]
    for ticker in ("AAA", "BBB", "CCC"):
        assert ticker in blob, f"{ticker} absent from the relative assessment"


def test_compare_reports_excluded_tickers_instead_of_dropping_them():
    """A ticker with too little history is reported, not silently omitted."""
    data = {
        "AAA": build_rows(ticker="AAA", close=[10_000.0 + i * 50 for i in range(60)]),
        "BBB": build_rows(ticker="BBB", close=[20_000.0 - i * 30 for i in range(60)]),
        "TINY": build_rows(ticker="TINY", close=[5_000.0, 5_100.0, 5_050.0]),
    }
    res = compare_multiple_tickers(data, window_days=50)

    assert "TINY" not in res["tickers_compared"]
    excluded = {entry["ticker"]: entry for entry in res["tickers_excluded"]}
    assert "TINY" in excluded
    assert excluded["TINY"]["reason"] == "insufficient_history"
    assert excluded["TINY"]["rows_available"] == 3

