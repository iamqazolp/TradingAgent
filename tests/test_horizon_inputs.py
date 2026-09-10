"""Regression tests for the horizon layer's reads of engine output.

Every test here corresponds to a defect where the interpretation layer looked up
a key the engine does not emit and silently degraded instead of failing. A unit
test on the pure classifiers cannot catch that class of bug: the classifiers
were correct and were simply never given a value.
"""

from __future__ import annotations

import pytest

from indicators import pick, pick_scalar
from indicators.engine import compute, multi_horizon_compute
from tests.conftest import build_rows


@pytest.fixture
def long_rows():
    """Enough history for SMA200, weekly bars and the 120d windows."""
    return build_rows(
        ticker="AAA",
        close=[20_000.0 + (i % 23) * 210 - (i % 7) * 90 for i in range(420)],
    )


# --------------------------------------------------------------------------- resolver


def test_pick_resolves_window_suffixed_keys():
    group = {"rsi_14": {"latest": 55.0}, "z_score_20": {"latest": 1.0}}
    assert pick(group, "rsi") == {"latest": 55.0}
    assert pick_scalar(group, "rsi") == 55.0
    # Exact match wins over a suffixed one.
    assert pick({"rsi": {"latest": 1}, "rsi_14": {"latest": 2}}, "rsi") == {"latest": 1}
    # Absent name is None, not an accidental match.
    assert pick(group, "macd") is None
    # Ambiguity is refused rather than guessed.
    assert pick({"rsi_9": {}, "rsi_14": {}}, "rsi") is None


def test_pick_scalar_treats_marker_as_missing():
    group = {"rsi_14": {"insufficient_data": True, "reason": "too short"}}
    assert pick_scalar(group, "rsi") is None


# --------------------------------------------------------------------------- rsi reaches horizons


def test_every_horizon_receives_rsi(long_rows):
    """RSI must reach all three horizon verdicts.

    Reading a hardcoded "rsi" against a group that emits "rsi_14" produced
    `momentum: insufficient_data` on every horizon, so momentum contributed
    nothing to any verdict.
    """
    result = multi_horizon_compute(long_rows)
    horizons = result["horizons"]

    for name in ("short_term", "mid_term"):
        assert horizons[name]["rsi"] is not None, f"{name} has no daily RSI"
        assert horizons[name]["momentum"] != "insufficient_data"

    # The long-term view uses the weekly RSI.
    assert horizons["long_term"]["weekly_rsi"] is not None
    assert horizons["long_term"]["momentum"] != "insufficient_data"


def test_horizons_score_momentum_components(long_rows):
    result = multi_horizon_compute(long_rows)
    for name in ("short_term", "mid_term", "long_term"):
        components = result["horizons"][name]["components"]
        momentum = [c for c in components if c["group"] == "momentum"]
        assert momentum, f"{name} has no momentum component"
        assert any(c["direction"] is not None for c in momentum), (
            f"{name}: every momentum component is missing data"
        )


# --------------------------------------------------------------------------- horizon separation


def test_horizons_use_different_windows(long_rows):
    """The three horizons must not be three copies of one reading."""
    result = multi_horizon_compute(long_rows, detail="full")
    horizons = result["horizons"]

    labels = {
        name: {c["label"] for c in horizons[name]["components"]}
        for name in ("short_term", "mid_term", "long_term")
    }
    # Each horizon has at least one input the others do not use.
    assert labels["short_term"] - labels["mid_term"] - labels["long_term"]
    assert labels["mid_term"] - labels["short_term"] - labels["long_term"]
    assert labels["long_term"] - labels["short_term"] - labels["mid_term"]

    # And each cites a foreign-flow window of its own.
    assert horizons["short_term"]["foreign_20d"] is not None
    assert horizons["mid_term"]["foreign_60d"] is not None
    assert horizons["long_term"]["foreign_120d"] is not None

    # The foreign windows genuinely differ, rather than one figure reused.
    windows = {
        horizons["short_term"]["foreign_20d"]["window"],
        horizons["mid_term"]["foreign_60d"]["window"],
        horizons["long_term"]["foreign_120d"]["window"],
    }
    assert windows == {20, 60, 120}


def test_compact_mode_keeps_every_field_the_skill_renders(long_rows):
    """Compact must not drop anything SKILL.md tells the model to render."""
    compact = multi_horizon_compute(long_rows, detail="compact")
    required_per_horizon = (
        "label_vi", "description", "signal_strength", "confidence",
        "confidence_reason", "inputs_used", "components", "conflicts",
        "conflict_summary", "invalidation", "sma_values",
    )
    for name in ("short_term", "mid_term", "long_term"):
        horizon = compact["horizons"][name]
        for field in required_per_horizon:
            assert field in horizon, f"compact dropped {name}.{field}"

    for section in ("daily", "horizons", "scenarios", "strategies", "stats_52w", "levels"):
        assert section in compact, f"compact dropped {section}"
    assert compact["levels"]["supports"] is not None
    assert compact["daily"]["groups"]["momentum"]


def test_compact_mode_is_materially_smaller(long_rows):
    """Compact must actually cut payload, or it is not worth the flag."""
    import json

    compact = json.dumps(multi_horizon_compute(long_rows, detail="compact"), default=str)
    full = json.dumps(multi_horizon_compute(long_rows, detail="full"), default=str)
    assert len(compact) < len(full) * 0.9


def test_components_carry_numeric_evidence(long_rows):
    """Each scored component must state the number behind it."""
    result = multi_horizon_compute(long_rows)
    for name in ("short_term", "mid_term", "long_term"):
        for comp in result["horizons"][name]["components"]:
            if comp["direction"] is None:
                assert comp["missing_reason"], f"{name}/{comp['label']}: no reason given"
                continue
            assert comp["evidence"], f"{name}/{comp['label']}: no evidence"
            assert any(ch.isdigit() for ch in comp["evidence"]), (
                f"{name}/{comp['label']}: evidence cites no number: {comp['evidence']}"
            )


def test_missing_data_lowers_confidence_without_faking_neutral():
    """Short history must reduce coverage, not manufacture neutral readings."""
    short_history = build_rows(
        ticker="AAA", close=[20_000.0 + i * 30 for i in range(40)]
    )
    result = multi_horizon_compute(short_history)
    long_term = result["horizons"]["long_term"]

    assert long_term["coverage_pct"] < 100.0
    assert long_term["groups_missing"]
    assert long_term["confidence"] in ("thấp", "trung bình")
    # Components with no data report None, never 0.
    missing = [c for c in long_term["components"] if c["direction"] is None]
    assert missing
    for comp in missing:
        assert comp["missing_reason"]


def test_alignment_reports_per_horizon_directions(long_rows):
    result = multi_horizon_compute(long_rows)
    alignment = result["horizons"]["alignment"]

    assert set(alignment["directions"]) == {"short_term", "mid_term", "long_term"}
    assert alignment["label"] in (
        "all_bullish", "all_bearish", "mostly_bullish", "mostly_bearish",
        "conflicting", "all_neutral", "insufficient_data",
    )
    assert alignment["summary"]
    assert alignment["shared_input_caveat"]
    assert set(alignment["per_horizon_confidence"]) == {
        "short_term", "mid_term", "long_term"
    }


# --------------------------------------------------------------------------- invalidation & levels


def _measured_levels(result: dict) -> set[float]:
    return {
        entry["level"]
        for entry in result["levels"]["supports"] + result["levels"]["resistances"]
    }


def _check_invalidation(result: dict, name: str) -> str | None:
    """Assert one horizon's invalidation and return the branch it exercised."""
    horizon = result["horizons"][name]
    invalidation = horizon["invalidation"]
    if invalidation is None:
        return None
    measured = _measured_levels(result)
    assert invalidation["basis"]
    assert invalidation["condition"]

    strength = horizon["signal_strength"]
    if "bullish" in strength or "bearish" in strength:
        assert invalidation["level"] in measured, (
            f"{name}: invalidation level {invalidation['level']} is not a measured level"
        )
        # Direction and wording must agree.
        if "bullish" in strength:
            assert "tích cực bị vô hiệu" in invalidation["condition"]
            assert "dưới" in invalidation["condition"]
        else:
            assert "tiêu cực bị vô hiệu" in invalidation["condition"]
            assert "trên" in invalidation["condition"]
        return "directional"

    # A neutral verdict names both boundaries instead of borrowing one side's wording.
    assert "trung tính bị vô hiệu" in invalidation["condition"]
    for key in ("upper_level", "lower_level"):
        if invalidation.get(key) is not None:
            assert invalidation[key] in measured
    return "neutral"


def test_each_horizon_has_a_measured_invalidation_level(long_rows):
    result = multi_horizon_compute(long_rows)
    for name in ("short_term", "mid_term", "long_term"):
        _check_invalidation(result, name)


def test_neutral_verdict_does_not_borrow_bullish_invalidation_wording():
    """A neutral horizon must not report "nhận định tích cực bị vô hiệu".

    Direction 0 fell into the `>= 0` branch, so a neutral long-term verdict
    described itself as a positive one being invalidated.
    """
    branches: set[str] = set()
    # Several shapes, to reach a neutral verdict on at least one horizon.
    for spec in (
        [10_000.0 + i * 40 for i in range(35)],
        [20_000.0 + (i % 3) * 25 for i in range(60)],
        [15_000.0 + (i % 5) * 30 - (i % 2) * 30 for i in range(90)],
    ):
        result = multi_horizon_compute(build_rows(ticker="AAA", close=spec))
        for name in ("short_term", "mid_term", "long_term"):
            branch = _check_invalidation(result, name)
            if branch:
                branches.add(branch)

    assert "neutral" in branches, (
        "no neutral verdict was produced, so the neutral wording path is untested"
    )


# --------------------------------------------------------------------------- scenarios


def test_scenarios_quote_only_measured_levels(long_rows):
    """No scenario may quote a level that is not in the measured level set.

    Targets and triggers used to fall back to close * 1.05 / 0.90 while still
    labelling themselves "nearest resistance".
    """
    result = multi_horizon_compute(long_rows)
    measured = {
        entry["level"]
        for entry in result["levels"]["supports"] + result["levels"]["resistances"]
    }

    for scenario in result["scenarios"]["scenarios"]:
        for field in ("trigger", "invalidation"):
            block = scenario.get(field) or {}
            if block.get("level") is not None:
                assert block["level"] in measured, (
                    f"{scenario['name']}.{field} quotes unmeasured level {block['level']}"
                )
        zone = scenario.get("target_zone") or {}
        for key in ("from", "to"):
            if zone.get(key) is not None:
                assert zone[key] in measured, (
                    f"{scenario['name']}.target_zone.{key} quotes unmeasured level {zone[key]}"
                )
        band = scenario.get("range") or {}
        for key in ("low", "high"):
            if band.get(key) is not None:
                assert band[key] in measured


def test_scenario_likelihood_is_not_a_percentage(long_rows):
    """The likelihood label must not be renderable as a percent."""
    result = multi_horizon_compute(long_rows)
    for scenario in result["scenarios"]["scenarios"]:
        assert "probability" not in scenario
        assert scenario["likelihood"] in ("cao", "trung bình", "thấp")
        assert scenario["is_probability_estimate"] is False
        assert scenario["likelihood_basis"]
        assert isinstance(scenario["conditions"], list)
    assert "KHÔNG phải xác suất" in result["scenarios"]["likelihood_note"]
