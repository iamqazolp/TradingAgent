"""Every machine enum in a tool payload carries a `_vi` sibling in Vietnamese.

The deployed model is a ~31B local model rendering JSON into prose. When it
had to translate `strong_bearish` itself it produced a different Vietnamese
phrase on every call; these tests pin the raw->Vietnamese contract so the
skill can tell the model to read the `_vi` field instead of inventing one.
"""

from __future__ import annotations

from indicators import VI_LABELS, vi_label, with_vi_labels
from indicators.engine import multi_horizon_compute
from indicators.market_breadth import market_breadth_summary
from tests.conftest import build_rows


def test_every_mapped_enum_translates():
    assert vi_label("strong_bearish") == "tiêu cực rõ rệt"
    assert vi_label("below_sma200_transitional") == "vẫn nằm dưới đường trung bình 200 phiên"
    assert vi_label("net_buying") == "mua ròng"
    assert vi_label("not_a_real_enum") is None
    assert vi_label(68446.0) is None


def test_with_vi_labels_keeps_raw_and_adds_sibling():
    out = with_vi_labels({"signal_strength": "strong_bearish", "close": 68446.0})
    assert out["signal_strength"] == "strong_bearish"
    assert out["signal_strength_vi"] == VI_LABELS["strong_bearish"]
    assert out["close"] == 68446.0
    assert "close_vi" not in out


def test_with_vi_labels_handles_booleans():
    assert with_vi_labels({"squeeze": True})["squeeze_vi"] == "dải Bollinger đang co hẹp"
    assert with_vi_labels({"squeeze": False})["squeeze_vi"] == "dải Bollinger nới rộng bình thường"
    # Switches that are not technical prose get no sibling.
    assert "is_closed_vi" not in with_vi_labels({"is_closed": 0})


def test_multi_horizon_verdicts_carry_vi():
    rows = build_rows(
        ticker="AAA",
        close=[30_000.0 + (i % 29) * 260 - (i % 11) * 110 for i in range(430)],
    )
    payload = multi_horizon_compute(rows, detail="compact")
    for name in ("short_term", "mid_term", "long_term"):
        verdict = payload["horizons"].get(name, {})
        if verdict.get("signal_strength"):
            assert verdict.get("signal_strength_vi"), f"{name} missing signal_strength_vi"
    assert payload["daily"].get("trend_alignment_vi"), "daily missing trend_alignment_vi"
    zone = payload["daily"]["groups"]["momentum"]["rsi_14"]["zone"]
    vi_zone = payload["daily"]["groups"]["momentum"]["rsi_14"]["zone_vi"]
    assert vi_zone == VI_LABELS[zone]


def test_market_breadth_regime_is_aliased_and_translated():
    rows = [{
        "exchange": "HOSE", "date": "2026-01-22",
        "index_current": 1250.0, "index_change": 5.0, "index_percent_change": 0.4,
        "total_trade": 100, "total_value": 1e9, "total_volume": 1000,
        "advances": 200, "declines": 100, "unchanged": 50,
    }]
    out = market_breadth_summary(rows)
    # SKILL.md reads `market_regime`; the payload used to emit only `breadth_regime`.
    assert out["HOSE"]["market_regime"] == out["HOSE"]["breadth_regime"]
    assert out["HOSE"]["breadth_regime_vi"] == VI_LABELS["strongly_bullish"]
