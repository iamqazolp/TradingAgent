"""Unit tests for indicators/screener.py: strategies, ranking, and foreign flow scanning."""

from __future__ import annotations

import pytest
from indicators.screener import (
    VN30_TICKERS,
    scan_foreign_flow,
    screen_and_rank,
)
from tests.conftest import build_rows


def test_vn30_tickers_list():
    assert len(VN30_TICKERS) == 30
    assert len(set(VN30_TICKERS)) == 30
    for sym in ("HPG", "VNM", "VCB", "FPT", "VIC", "SSI"):
        assert sym in VN30_TICKERS


def test_momentum_breakout_strategy():
    # Construct 40 rows with price above SMA20, volume spike on last day, RSI in 50-70 zone
    closes = [10_000.0 + 50 * i for i in range(40)]
    closes[-1] = closes[-2] * 1.03  # +3% breakout
    volumes = [1_000] * 39 + [2_500]  # 2.5x volume spike

    rows = build_rows(
        ticker="HPG",
        close=closes,
        total_volume=volumes,
    )
    result = screen_and_rank({"HPG": rows}, strategy="momentum_breakout", top_n=3)
    assert result["universe_size"] == 1
    assert len(result["ranked_candidates"]) == 1
    candidate = result["ranked_candidates"][0]
    assert candidate["symbol"] == "HPG"
    assert candidate["score"] >= 70.0
    assert candidate["price"] == closes[-1]
    assert any("SMA20" in s for s in candidate["key_signals"])
    assert any("Khối lượng" in s for s in candidate["key_signals"])


def test_oversold_reversal_strategy():
    # 25 rows steadily declining so RSI < 30, then bounce on last day near support
    closes = [20_000.0 - 200 * i for i in range(25)]
    closes[-1] = closes[-2] * 1.02  # 2% bounce
    rows = build_rows(
        ticker="VNM",
        close=closes,
        open=[c - 100 for c in closes],
        high=[c + 200 for c in closes],
        low=[c - 300 for c in closes],
    )
    result = screen_and_rank({"VNM": rows}, strategy="oversold_reversal", top_n=3)
    assert len(result["ranked_candidates"]) == 1
    cand = result["ranked_candidates"][0]
    assert cand["symbol"] == "VNM"
    assert cand["score"] >= 50.0
    assert any("RSI" in s for s in cand["key_signals"])


def test_foreign_accumulation_strategy():
    # 20 rows with huge positive foreign buying in last 5 sessions
    rows = build_rows(
        count=20,
        ticker="SSI",
        close=[30_000.0] * 20,
        total_value=[100_000_000_000.0] * 20,
        foreign_buy_value=[30_000_000_000.0] * 20,
        foreign_sell_value=[5_000_000_000.0] * 20,
        foreign_room=[10_000_000] * 20,
    )
    result = screen_and_rank({"SSI": rows}, strategy="foreign_accumulation", top_n=3)
    cand = result["ranked_candidates"][0]
    assert cand["symbol"] == "SSI"
    assert cand["score"] >= 80.0
    assert any("Khối ngoại" in s for s in cand["key_signals"])
    assert any("Tỷ trọng" in s for s in cand["key_signals"])


def test_intraday_breakout_strategy():
    # Construct 1h bars for today: 2 morning bars with high 20,000, afternoon bar breaks to 21,000
    hourly_rows = [
        {"ticker": "FPT", "date": "2026-03-30", "session_index": 1, "open": 19_500.0, "high": 19_800.0, "low": 19_400.0, "close": 19_700.0, "volume": 100_000},
        {"ticker": "FPT", "date": "2026-03-30", "session_index": 2, "open": 19_700.0, "high": 20_000.0, "low": 19_600.0, "close": 19_900.0, "volume": 120_000},
        {"ticker": "FPT", "date": "2026-03-30", "session_index": 3, "open": 20_000.0, "high": 21_200.0, "low": 19_950.0, "close": 21_000.0, "volume": 350_000},
    ]
    result = screen_and_rank({"FPT": hourly_rows}, strategy="intraday_breakout", timeframe="1h", top_n=3)
    cand = result["ranked_candidates"][0]
    assert cand["symbol"] == "FPT"
    assert cand["score"] >= 80.0
    assert any("bứt phá" in s for s in cand["key_signals"])
    assert any("bùng nổ" in s for s in cand["key_signals"])


def test_screen_and_rank_sorting_and_top_n():
    universe = {}
    for i, sym in enumerate(["AAA", "BBB", "CCC", "DDD", "EEE"]):
        closes = [10_000.0 + (i * 100) * j for j in range(30)]
        universe[sym] = build_rows(ticker=sym, close=closes)

    result = screen_and_rank(universe, strategy="momentum_breakout", top_n=3)
    ranked = result["ranked_candidates"]
    assert len(ranked) == 3
    assert ranked[0]["rank"] == 1
    assert ranked[1]["rank"] == 2
    assert ranked[2]["rank"] == 3
    # Check descending order
    assert ranked[0]["score"] >= ranked[1]["score"] >= ranked[2]["score"]


def test_insufficient_data_resilience():
    # Ticker with only 2 rows should not raise exception
    short_rows = build_rows(count=2, ticker="SHT")
    result = screen_and_rank({"SHT": short_rows}, strategy="momentum_breakout", top_n=3)
    assert len(result["ranked_candidates"]) == 1
    cand = result["ranked_candidates"][0]
    assert cand["score"] == 0.0
    assert cand["highlights"].get("insufficient_data") is True


def test_scan_foreign_flow_logic():
    buyer_rows = build_rows(
        count=10,
        ticker="BUY",
        total_value=[10_000_000_000.0] * 10,
        foreign_buy_value=[5_000_000_000.0] * 10,
        foreign_sell_value=[1_000_000_000.0] * 10,
        foreign_room=[2_000_000] * 10,
    )
    seller_rows = build_rows(
        count=10,
        ticker="SEL",
        total_value=[10_000_000_000.0] * 10,
        foreign_buy_value=[500_000_000.0] * 10,
        foreign_sell_value=[4_000_000_000.0] * 10,
        foreign_room=[5_000_000] * 10,
    )
    exhausted_rows = build_rows(
        count=10,
        ticker="EXH",
        foreign_room=[50_000] * 10,
    )
    universe = {"BUY": buyer_rows, "SEL": seller_rows, "EXH": exhausted_rows}
    result = scan_foreign_flow(universe, window_days=5, top_n=2)

    assert result["universe_size"] == 3
    assert len(result["top_net_buyers"]) >= 1
    assert result["top_net_buyers"][0]["symbol"] == "BUY"
    assert result["top_net_buyers"][0]["net_value_billion"] > 0

    assert len(result["top_net_sellers"]) >= 1
    assert result["top_net_sellers"][0]["symbol"] == "SEL"
    assert result["top_net_sellers"][0]["net_value_billion"] < 0

    assert len(result["room_warnings"]) >= 1
    assert any(w["symbol"] == "EXH" for w in result["room_warnings"])
