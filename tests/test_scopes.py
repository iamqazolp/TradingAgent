"""Tests for query-scoped analysis.

A single request does not need every section. These tests pin down that a scope
computes what it promises, skips the rest, says which sections it skipped, and
never lets "not requested" look like "not enough data".
"""

from __future__ import annotations

import json

import pytest

from indicators.engine import GROUPS, SCOPES, multi_horizon_compute
from tests.conftest import build_rows


@pytest.fixture(scope="module")
def rows():
    return build_rows(
        ticker="AAA",
        close=[30_000.0 + (i % 23) * 240 - (i % 7) * 95 for i in range(430)],
    )


def payload_size(result: dict) -> int:
    return len(json.dumps(result, ensure_ascii=False, default=str))


# --------------------------------------------------------------------------- shape


def test_every_scope_produces_a_usable_payload(rows):
    for scope in SCOPES:
        result = multi_horizon_compute(rows, scope=scope)
        assert result["scope"] == scope
        assert result["scope_description"]
        # Always present: these two are what every scope is built on.
        assert "daily" in result
        assert "levels" in result
        assert result["daily"]["latest_close"] is not None


def test_unknown_scope_is_refused(rows):
    from indicators.engine import EngineError

    with pytest.raises(EngineError, match="unknown scope"):
        multi_horizon_compute(rows, scope="wibble")


@pytest.mark.parametrize(
    "scope,horizon",
    [("short_term", "short_term"), ("mid_term", "mid_term"), ("long_term", "long_term")],
)
def test_single_horizon_scope_computes_only_that_horizon(rows, scope, horizon):
    result = multi_horizon_compute(rows, scope=scope)
    horizons = result["horizons"]

    assert horizons["horizons_computed"] == [horizon]
    assert horizon in horizons
    for other in ("short_term", "mid_term", "long_term"):
        if other != horizon:
            assert other not in horizons, f"{scope} computed {other} anyway"

    # The requested horizon is fully formed, not a stub.
    entry = horizons[horizon]
    assert entry["signal_strength"]
    assert entry["confidence"]
    assert entry["confidence_reason"]
    assert entry["components"]
    assert entry["inputs_used"]


def test_full_scope_computes_all_three_horizons(rows):
    horizons = multi_horizon_compute(rows, scope="full")["horizons"]
    assert horizons["horizons_computed"] == ["short_term", "mid_term", "long_term"]
    assert horizons["alignment"]["horizons_scored"] == 3


def test_levels_scope_returns_levels_without_horizons(rows):
    result = multi_horizon_compute(rows, scope="levels")
    assert "horizons" not in result
    assert "scenarios" not in result
    assert "strategies" not in result
    assert result["levels"]["supports"] or result["levels"]["resistances"]
    assert result["levels"]["position"]["description"]


# --------------------------------------------------------------------------- omitted != missing


def test_scopes_declare_what_they_skipped(rows):
    for scope, plan in SCOPES.items():
        result = multi_horizon_compute(rows, scope=scope)
        if scope == "full":
            # Full skips nothing except, under compact detail, value_flow.
            omitted = result.get("sections_omitted", [])
            assert all(o.startswith("daily.groups.") for o in omitted)
            continue

        omitted = result["sections_omitted"]
        assert omitted, f"{scope} skipped work but declared nothing"
        assert "KHÔNG phải là thiếu dữ liệu" in result["sections_omitted_note"]

        # Anything declared omitted must genuinely be absent, and vice versa.
        for name in ("short_term", "mid_term", "long_term"):
            declared = f"horizons.{name}" in omitted
            present = name in (result.get("horizons") or {})
            assert declared != present, (
                f"{scope}: horizons.{name} declared_omitted={declared} present={present}"
            )
        for section in ("weekly", "stats_52w", "strategies"):
            declared = section in omitted
            present = section in result
            assert declared != present, (
                f"{scope}: {section} declared_omitted={declared} present={present}"
            )


def test_omitted_sections_never_appear_as_insufficient_data(rows):
    """A skipped section must be absent, not present-but-empty.

    A present-but-empty section reads as "the data was not there", which is a
    different and wrong claim.
    """
    result = multi_horizon_compute(rows, scope="short_term")
    for section in result["sections_omitted"]:
        head = section.split(".")[0]
        if head == section:
            assert section not in result
        else:
            # Nested paths: walk and confirm the leaf is truly gone.
            parts = section.split(".")
            cursor = result
            missing = False
            for part in parts:
                if not isinstance(cursor, dict) or part not in cursor:
                    missing = True
                    break
                cursor = cursor[part]
            assert missing, f"{section} was declared omitted but is present"


def test_single_horizon_scope_omits_unrequested_horizons(rows):
    """Single horizon scope focuses on one horizon and omits others."""
    result = multi_horizon_compute(rows, scope="short_term")
    assert "scenarios" not in result
    assert "horizons.mid_term" in result["sections_omitted"]
    assert "horizons.long_term" in result["sections_omitted"]

    alignment = result["horizons"]["alignment"]
    assert alignment["label"] == "single_horizon_scope"
    assert alignment["horizons_scored"] == 1
    assert "không đánh giá" in alignment["summary"]
    assert result["horizons"]["horizon_alignment"] == "single_horizon_scope"


def test_strategies_cover_only_the_computed_horizons(rows):
    result = multi_horizon_compute(rows, scope="mid_term")
    strategies = result["strategies"]
    assert strategies["horizons_covered"] == ["mid_term"]
    assert "short_term" not in strategies
    assert "long_term" not in strategies
    assert strategies["mid_term"]["technical_state"]


# --------------------------------------------------------------------------- cost


def test_scoped_calls_are_materially_cheaper(rows):
    full = payload_size(multi_horizon_compute(rows, scope="full"))
    short = payload_size(multi_horizon_compute(rows, scope="short_term"))
    levels = payload_size(multi_horizon_compute(rows, scope="levels"))

    assert short < full * 0.60, f"short_term is {short / full:.0%} of full, expected <60%"
    assert levels < full * 0.35, f"levels is {levels / full:.0%} of full, expected <35%"


def test_scope_skips_weekly_computation_when_unused(rows):
    """short_term and levels must not pay for weekly aggregation at all."""
    for scope in ("short_term", "levels"):
        result = multi_horizon_compute(rows, scope=scope)
        assert "weekly" not in result
        assert "weekly_bars_available" not in result

    # The horizons that rely on weekly bars still get them.
    for scope in ("mid_term", "long_term", "full"):
        result = multi_horizon_compute(rows, scope=scope)
        assert "weekly" in result
        assert result["weekly_bars_available"] > 0


def test_scope_restricts_daily_groups_to_what_it_reads(rows):
    computed = multi_horizon_compute(rows, scope="levels")["daily"]["groups_computed"]
    assert set(computed) == {"trend", "momentum", "volatility"}

    full = multi_horizon_compute(rows, scope="full", detail="full")["daily"]["groups_computed"]
    assert set(full) == set(GROUPS)


# --------------------------------------------------------------------------- MCP layer


@pytest.mark.parametrize(
    "sent,expected",
    [
        ("short", "short_term"),
        ("SHORT", "short_term"),
        ("short-term", "short_term"),
        ("ngan_han", "short_term"),
        ("Dai Han", "long_term"),
        ("medium", "mid_term"),
        ("all", "full"),
        ("", "full"),
        ("'levels'", "levels"),
    ],
)
def test_tool_normalizes_scope_shapes_a_local_model_sends(sent, expected):
    from mcp_server.tool_schemas import AnalyzeMultiHorizonInput

    assert AnalyzeMultiHorizonInput(ticker="AAA", scope=sent).scope == expected


def test_tool_rejects_an_unrecognized_scope():
    from pydantic import ValidationError

    from mcp_server.tool_schemas import AnalyzeMultiHorizonInput

    with pytest.raises(ValidationError):
        AnalyzeMultiHorizonInput(ticker="AAA", scope="wibble")


def test_mcp_tool_passes_scope_through(monkeypatch, rows):
    from data import store
    from mcp_server.server import analyze_multi_horizon

    monkeypatch.setattr(store, "get_recent", lambda conn, ticker, lookback: rows)
    monkeypatch.setattr(store, "list_tickers", lambda conn: ["AAA"])

    result = analyze_multi_horizon(ticker="AAA", scope="ngan_han")
    assert result["scope"] == "short_term"
    assert result["horizons"]["horizons_computed"] == ["short_term"]
