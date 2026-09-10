"""SKILL.md documents JSON paths; this test proves they exist.

The deployed subagent is a small local model that renders tool JSON into a
report. A path in SKILL.md that the payload does not actually contain produces a
blank field or an invented number, and nothing in the pipeline notices. These
tests are the guard: change a payload key and this fails, rather than the
deployed report quietly losing a row.

Paths are listed explicitly rather than scraped out of the markdown, so a typo
in the prose cannot make the test vacuously pass.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from indicators.comparison import compare_multiple_tickers
from indicators.engine import multi_horizon_compute
from tests.conftest import build_rows

SKILL_PATH = Path(__file__).resolve().parent.parent / "skills" / "technical-analysis" / "SKILL.md"


# --------------------------------------------------------------------------- fixtures


@pytest.fixture(scope="module")
def multi_horizon_payload():
    rows = build_rows(
        ticker="AAA",
        close=[30_000.0 + (i % 29) * 260 - (i % 11) * 110 for i in range(430)],
    )
    return multi_horizon_compute(rows, detail="compact")


@pytest.fixture(scope="module")
def comparison_payload():
    data = {
        "AAA": build_rows(ticker="AAA", close=[30_000.0 + (i % 19) * 190 for i in range(300)]),
        "BBB": build_rows(ticker="BBB", close=[18_000.0 - (i % 13) * 95 for i in range(300)]),
        "CCC": build_rows(ticker="CCC", close=[24_000.0 + (i % 7) * 140 for i in range(300)]),
    }
    return compare_multiple_tickers(data, window_days=250, detail="compact")


# --------------------------------------------------------------------------- path walker


def resolve(payload, path: str):
    """Walk a dotted path. `x[]` means 'a non-empty list; descend into item 0'."""
    current = payload
    for part in path.split("."):
        if part.endswith("[]"):
            name = part[:-2]
            if name:
                assert isinstance(current, dict), f"{path}: '{name}' parent is not a dict"
                assert name in current, f"{path}: missing key '{name}'"
                current = current[name]
            assert isinstance(current, list), f"{path}: '{name}' is not a list"
            assert current, f"{path}: '{name}' is an empty list"
            current = current[0]
        else:
            assert isinstance(current, dict), f"{path}: '{part}' parent is not a dict"
            assert part in current, f"{path}: missing key '{part}'"
            current = current[part]
    return current


MULTI_HORIZON_PATHS = [
    # Part 1 — current state
    "daily.latest_close", "daily.price_change_pct", "daily.trend_alignment",
    "daily.data_quality.warnings", "daily.latest_session.volume_mil_shares",
    "daily.latest_session.value_bil_vnd", "daily.latest_session.foreign_net_value_bil",
    "daily.recent_history[].date", "daily.recent_history[].close",
    "daily.recent_history[].change_pct", "daily.recent_history[].volume_mil",
    "daily.recent_history[].foreign_net_bil",
    # Part 2 — the three horizons
    "horizons.short_term.label_vi", "horizons.short_term.description",
    "horizons.short_term.signal_strength", "horizons.short_term.confidence",
    "horizons.short_term.confidence_reason", "horizons.short_term.inputs_used",
    "horizons.short_term.components[].direction",
    "horizons.short_term.components[].label",
    "horizons.short_term.components[].weight",
    "horizons.mid_term.signal_strength", "horizons.mid_term.components[].label",
    "horizons.long_term.signal_strength", "horizons.long_term.components[].label",
    "horizons.alignment.summary", "horizons.alignment.horizons_bullish",
    "horizons.alignment.horizons_bearish", "horizons.alignment.horizons_neutral",
    "horizons.alignment.horizons_scored", "horizons.alignment.confidence",
    "horizons.alignment.confidence_basis", "horizons.alignment.shared_input_caveat",
    "horizons.alignment.per_horizon_confidence",
    # Part 3 — daily detail
    "daily.groups.trend.sma_20.latest", "daily.groups.trend.sma_20.distance_pct",
    "daily.groups.trend.sma_20.direction", "daily.groups.trend.sma_50.latest",
    "daily.groups.trend.sma_100.latest", "daily.groups.trend.ema_20.latest",
    "daily.groups.trend.ema_50.latest",
    "daily.groups.trend.macd.latest.macd", "daily.groups.trend.macd.latest.signal",
    "daily.groups.trend.macd.latest.histogram", "daily.groups.trend.macd.crossover",
    "daily.returns",
    "daily.groups.momentum.rsi_14.latest", "daily.groups.momentum.rsi_14.zone",
    "daily.groups.momentum.return_streak.streak", "daily.groups.momentum.return_streak.flag",
    "daily.groups.momentum.close_percentile_by_window.20d.value",
    "daily.groups.momentum.close_percentile_by_window.20d.range_low",
    "daily.groups.momentum.close_percentile_by_window.20d.range_high",
    "daily.groups.volatility.bollinger.latest.percent_b_pct",
    "daily.groups.volatility.bollinger.latest.bandwidth_pct",
    "daily.groups.volatility.bollinger.position", "daily.groups.volatility.bollinger.squeeze",
    "daily.groups.volatility.close_to_close_vol.latest_annualized_pct",
    "daily.groups.volatility.close_to_close_vol.suggested_stop_distance_pct",
    "daily.groups.volatility.close_to_close_vol.label",
    "daily.groups.volume_flow.volume_ratio.pct_of_average",
    "daily.groups.volume_flow.volume_ratio.flag",
    "daily.groups.volume_flow.volume_spikes.spikes_20d.total",
    "daily.groups.volume_flow.volume_spikes.spikes_60d.total",
    "daily.groups.volume_flow.obv_divergence.divergence_20",
    "daily.groups.volume_flow.obv_divergence.divergence_60",
    "daily.groups.volume_flow.buy_sell_volume_imbalance.latest_rolling_avg",
    "daily.groups.volume_flow.buy_sell_volume_imbalance.bias",
    "daily.groups.trade_flow.buy_sell_count_imbalance_5.latest",
    "daily.groups.trade_flow.avg_trade_size_by_side_20.latest.avg_buy_trade_size_lots",
    "daily.groups.trade_flow.avg_trade_size_by_side_20.latest.avg_sell_trade_size_lots",
    "daily.groups.trade_flow.avg_trade_size_by_side_20.interpretation",
    "daily.groups.foreign_flow.foreign_net_value.latest_bil_vnd",
    "daily.groups.foreign_flow.foreign_net_value.windows.20d.summary",
    "daily.groups.foreign_flow.foreign_net_value.windows.60d.summary",
    "daily.groups.foreign_flow.foreign_room_trend_5.reading",
    # Part 4 — weekly and 52-week
    "weekly_bars_available", "weekly_bars_min_required",
    "stats_52w.window_label_vi", "stats_52w.is_full_52w", "stats_52w.basis_note",
    "stats_52w.high_52w.price", "stats_52w.high_52w.date",
    "stats_52w.high_52w.pct_from_current", "stats_52w.low_52w.price",
    "stats_52w.low_52w.date", "stats_52w.return_pct", "stats_52w.max_drawdown_pct",
    "stats_52w.avg_daily_volume_mil", "stats_52w.avg_daily_value_bil",
    # Part 5 — levels and scenarios
    "levels.basis_note", "levels.supports[].level", "levels.supports[].basis",
    "levels.supports[].confluence", "levels.supports[].distance_pct",
    "levels.resistances[].level", "levels.resistances[].confluence",
    "levels.position.description", "levels.close_extremes.20d.high",
    "levels.close_extremes.20d.high_date", "levels.close_extremes.20d.low",
    "levels.close_extremes.20d.low_date",
    "scenarios.scenarios[].name", "scenarios.scenarios[].bias",
    "scenarios.scenarios[].likelihood", "scenarios.scenarios[].likelihood_basis",
    "scenarios.scenarios[].is_probability_estimate",
    "scenarios.scenarios[].conditions", "scenarios.dominant_scenario",
    "scenarios.likelihood_note",
    "strategies.technical_summary", "strategies.short_term.technical_state",
    "strategies.short_term.support_zone", "strategies.short_term.resistance_zone",
    "strategies.short_term.confirmation_signal", "strategies.short_term.risk_factors",
    "strategies.mid_term.technical_state", "strategies.long_term.technical_state",
    "strategies.disclaimer",
]

COMPARISON_PATHS = [
    "tickers_compared", "tickers_excluded", "levels_note", "disclaimer",
    "table_52w[].ticker", "table_52w[].return_pct", "table_52w[].high_52w",
    "table_52w[].high_date", "table_52w[].pct_from_high", "table_52w[].low_52w",
    "table_52w[].low_date", "table_52w[].pct_from_low", "table_52w[].max_drawdown_pct",
    "table_52w[].avg_volume_mil", "table_52w[].avg_value_bil",
    "table_ma[].ticker", "table_ma[].vs_sma20_pct", "table_ma[].vs_sma50_pct",
    "table_ma[].vs_sma100_pct", "table_ma[].vs_sma200_pct", "table_ma[].vs_ema20_pct",
    "table_ma[].vs_ema50_pct", "table_ma[].vs_ema200_pct", "table_ma[].rsi",
    "table_ma[].rsi_zone", "table_ma[].macd", "table_ma[].macd_signal",
    "table_ma[].macd_hist", "table_ma[].macd_hist_pct_of_close",
    "table_ma[].trend_alignment",
    "table_levels[].ticker", "table_levels[].nearest_support",
    "table_levels[].nearest_support_basis", "table_levels[].nearest_support_distance_pct",
    "table_levels[].nearest_resistance", "table_levels[].nearest_resistance_basis",
    "table_levels[].nearest_resistance_distance_pct", "table_levels[].position_desc",
    "relative_assessment.observations[]", "relative_assessment.tickers_assessed",
]


@pytest.mark.parametrize("path", MULTI_HORIZON_PATHS)
def test_multi_horizon_path_documented_in_skill_exists(multi_horizon_payload, path):
    resolve(multi_horizon_payload, path)


@pytest.mark.parametrize("path", COMPARISON_PATHS)
def test_comparison_path_documented_in_skill_exists(comparison_payload, path):
    resolve(comparison_payload, path)


# --------------------------------------------------------------------------- prose checks


def test_skill_file_does_not_reference_removed_fields():
    """SKILL.md must not tell the model to read fields the engine dropped."""
    text = SKILL_PATH.read_text(encoding="utf-8")
    for removed in (
        "table_pivots",          # replaced by table_levels
        "pivots.classic",        # synthetic-high/low pivots are gone
        "position_description",  # renamed under levels.position.description
        "rsi_14.latest`, `.zone` (vùng quá bán",  # stale phrasing from the old map
    ):
        assert removed not in text, f"SKILL.md still references removed field: {removed}"


def test_skill_file_forbids_rendering_likelihood_as_percent():
    """The old instruction to print `probability` as X% must stay gone."""
    text = SKILL_PATH.read_text(encoding="utf-8")
    assert "Xác suất = X%" not in text
    assert "likelihood" in text
    assert "KHÔNG quy đổi thành %" in text or "KHÔNG đổi thành %" in text


def test_skill_file_documents_every_scope_that_exists():
    """The routing table must cover the real scope enum, no more and no less.

    A scope the skill never mentions is dead capability; a scope the skill names
    but the tool rejects is a guaranteed tool error at runtime.
    """
    from indicators.engine import SCOPES

    text = SKILL_PATH.read_text(encoding="utf-8")
    for scope in SCOPES:
        assert f"`{scope}`" in text or f'"{scope}"' in text or f"'{scope}'" in text, (
            f"SKILL.md never mentions scope '{scope}'"
        )
    # And it must tell the model that omitted sections are not missing data.
    assert "sections_omitted" in text
    assert "KHÔNG phải thiếu dữ liệu" in text


def test_scope_routing_is_a_decision_procedure_not_an_ambiguous_keyword_table():
    """"phân tích" must not be a scope trigger.

    The first routing table listed "phân tích" under scope=full and "ngắn hạn"
    under scope=short_term, so "phân tích vnm trong ngắn hạn" matched both rows
    and a keyword-matching model would take the first — defeating the scope
    feature for the most natural Vietnamese phrasing.
    """
    text = SKILL_PATH.read_text(encoding="utf-8")

    # The routing section must be ordered steps, and must say not to route on "phân tích".
    assert "Bước 1" in text and "Bước 4" in text
    assert 'KHÔNG** dùng nó để chọn scope' in text or "KHÔNG** dùng nó" in text

    # The worked examples must pin the two phrasings that collided.
    assert "phân tích vnm trong ngắn hạn" in text.lower()
    assert "phân tích toàn diện vnm" in text.lower()

    # In the examples table, the short-horizon phrasing resolves to short_term.
    for line in text.splitlines():
        low = line.lower()
        if low.startswith("|") and "phân tích vnm trong ngắn hạn" in low:
            assert "short_term" in line, f"routing example resolves wrongly: {line}"
            break
    else:
        pytest.fail("no routing example row for 'phân tích vnm trong ngắn hạn'")


def test_skill_file_warns_against_reporting_latest_for_a_historical_date():
    """The as_of trap must be spelled out, since `latest` looks like an answer."""
    text = SKILL_PATH.read_text(encoding="utf-8")
    assert "as_of" in text
    assert "as_of_effective" in text
    assert "is_trading_day" in text
    assert "no_rows_before_as_of" in text
    # And it must show the wrong-vs-right pair for a dated question.
    assert "PHIÊN GẦN NHẤT" in text
    assert "2026-01-02" in text


def test_skill_file_documents_the_tools_that_exist():
    text = SKILL_PATH.read_text(encoding="utf-8")
    for tool in (
        "analyze_multi_horizon",
        "compare_tickers",
        "get_price_data",
        "get_flow_summary",
        "compute_indicators",
        "compute_weekly_indicators",
    ):
        assert tool in text, f"SKILL.md does not mention the {tool} tool"
