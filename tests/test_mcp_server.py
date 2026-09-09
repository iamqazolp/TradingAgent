"""MCP server tests: tool surface, validation, refusals, and the audit trail.

The tools are called in process through `server.call_tool`, which is the same
entry point the stdio transport uses, so schema validation and error wrapping are
exercised exactly as a real client would hit them.
`scripts/mcp_smoke.py` covers the transport itself.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from data import store
from indicators import is_insufficient
from indicators.engine import GROUPS, UNSUPPORTED_METRICS
from mcp_server import server as server_module
from mcp_server.server import server
from tests.conftest import build_rows

TICKER = "TST"


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """Point the server at a throwaway database and audit log."""
    db_path = tmp_path / "ta.sqlite"
    monkeypatch.setenv("TA_AGENT_DB", str(db_path))
    monkeypatch.setenv("TA_AGENT_AUDIT_LOG", str(tmp_path / "tool_calls.jsonl"))
    conn = store.connect(db_path)
    try:
        store.upsert_rows(conn, seeded_rows())
    finally:
        conn.close()
    return tmp_path


def seeded_rows() -> list[dict]:
    """40 rows of a steadily rising, internally consistent history."""
    closes = [10_000.0 + 100 * i for i in range(40)]
    return build_rows(
        ticker=TICKER,
        close=closes,
        total_volume=[1_000] * 40,
        buy_volume=[600] * 40,
        sell_volume=[400] * 40,
        foreign_room=[1_000_000 - 100 * i for i in range(40)],
        foreign_buy_volume=[100] * 40,
        foreign_sell_volume=[0] * 40,
        foreign_buy_value=[1_000_000.0] * 40,
        foreign_sell_value=[0.0] * 40,
    )


def call(tool: str, arguments: dict) -> dict:
    """Call a tool and decode its JSON payload."""
    result = asyncio.run(server.call_tool(tool, arguments))
    assert not result.is_error, result
    return json.loads(result.content[0].text)


def audit_entries(tmp_path: Path) -> list[dict]:
    path = tmp_path / "tool_calls.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# --------------------------------------------------------------------------- surface


def test_the_server_exposes_expected_tools():
    tools = asyncio.run(server.list_tools())
    assert sorted(t.name for t in tools) == [
        "analyze_multi_horizon",
        "compare_tickers",
        "compute_indicators",
        "compute_weekly_indicators",
        "get_flow_summary",
        "get_price_data",
    ]
    for tool in tools:
        assert tool.description
        assert tool.input_schema["type"] == "object"


def test_instructions_state_the_limits_and_the_no_fabrication_rule():
    text = server.instructions
    assert "Never state an indicator value that did not come from one of these tools" in text
    assert "must not be called ATR" in text
    for metric in UNSUPPORTED_METRICS:
        assert metric in text
    for group in GROUPS:
        assert group in text


# --------------------------------------------------------------------------- get_price_data


def test_get_price_data_returns_recent_rows_oldest_first():
    payload = call("get_price_data", {"ticker": TICKER, "lookback_days": 5})
    assert payload["row_count"] == 5
    dates = [row["date"] for row in payload["rows"]]
    assert dates == sorted(dates)
    assert payload["date_range"] == {"start": dates[0], "end": dates[-1]}
    assert payload["rows"][-1]["close"] == pytest.approx(13_900.0)
    assert "ticker" not in payload["rows"][0]  # not repeated on every row


def test_get_price_data_accepts_an_explicit_date_range():
    everything = call("get_price_data", {"ticker": TICKER, "lookback_days": 40})
    start = everything["rows"][2]["date"]
    end = everything["rows"][5]["date"]
    payload = call("get_price_data", {"ticker": TICKER, "start": start, "end": end})
    assert payload["row_count"] == 4
    assert payload["date_range"] == {"start": start, "end": end}


def test_unknown_ticker_is_a_structured_answer_not_a_crash():
    payload = call("get_price_data", {"ticker": "ZZZZ"})
    assert payload["error"] == "no_rows"
    assert payload["row_count"] == 0
    assert payload["available_tickers"] == [TICKER]
    assert "not in the store" in payload["message"]


def test_lowercase_ticker_is_accepted():
    assert call("get_price_data", {"ticker": TICKER.lower()})["ticker"] == TICKER


@pytest.mark.parametrize(
    "arguments,expected",
    [
        ({"ticker": "not a ticker"}, "ticker"),
        ({"ticker": TICKER, "lookback_days": 0}, "lookback_days"),
        ({"ticker": TICKER, "start": "2026-13-45"}, "start"),
        ({"ticker": TICKER, "start": "2026-02-01", "end": "2026-01-01"}, "after end"),
    ],
)
def test_bad_arguments_come_back_as_readable_tool_errors(arguments, expected):
    # ToolError, not an unexpected exception: the message has to reach the model,
    # otherwise it retries blind.
    with pytest.raises(ToolError) as exc:
        asyncio.run(server.call_tool("get_price_data", arguments))
    assert expected in str(exc.value)


def test_unknown_arguments_are_dropped_by_the_mcp_layer():
    # The MCP runtime validates against the generated schema and does not pass
    # undeclared keys through, so the call succeeds without them. Recorded here
    # because it means an agent typo silently loses that argument rather than
    # producing an error.
    payload = call("get_price_data", {"ticker": TICKER, "lookbackDays": 3})
    assert payload["row_count"] == 40  # the default, not 3


# --------------------------------------------------------------------------- compute_indicators


def test_compute_indicators_by_ticker():
    payload = call("compute_indicators", {"ticker": TICKER, "groups": ["trend", "momentum"]})
    assert payload["ticker"] == TICKER
    assert payload["groups_requested"] == ["trend", "momentum"]
    assert payload["rows_used"] == 40
    assert payload["groups"]["trend"]["sma_20"]["latest"] == pytest.approx(12_950.0)
    assert payload["groups"]["momentum"]["rsi_14"]["latest"] == pytest.approx(100.0)
    assert payload["data_quality"]["warnings"] == []


def test_compute_indicators_with_inline_rows_matches_the_stored_result():
    stored = call("compute_indicators", {"ticker": TICKER, "groups": ["trend"]})
    rows = call("get_price_data", {"ticker": TICKER, "lookback_days": 40})["rows"]
    inline = call("compute_indicators", {"rows": rows, "groups": ["trend"]})
    assert inline["groups"]["trend"]["sma_20"]["latest"] == pytest.approx(
        stored["groups"]["trend"]["sma_20"]["latest"]
    )


def test_series_tail_controls_the_payload_size():
    default_tail = call(
        "compute_indicators", {"ticker": TICKER, "groups": ["trend"]}
    )
    assert len(default_tail["groups"]["trend"]["sma_20"]["series"]["values"]) == 20
    trimmed = call(
        "compute_indicators", {"ticker": TICKER, "groups": ["trend"], "series_tail": 2}
    )
    assert len(trimmed["groups"]["trend"]["sma_20"]["series"]["values"]) == 2
    none = call(
        "compute_indicators", {"ticker": TICKER, "groups": ["trend"], "series_tail": 0}
    )
    assert len(none["groups"]["trend"]["sma_20"]["series"]["values"]) == 40


def test_short_history_returns_markers_through_the_tool_layer():
    rows = build_rows(ticker=TICKER, close=[10_000.0, 10_100.0, 10_050.0])
    payload = call("compute_indicators", {"rows": rows, "groups": ["trend", "momentum"]})
    trend = payload["groups"]["trend"]
    assert is_insufficient(trend["sma_20"])
    assert "20 rows of history, 3 available" in trend["sma_20"]["reason"]
    assert is_insufficient(payload["groups"]["momentum"]["rsi_14"])


def test_unsupported_group_is_rejected_with_the_valid_list():
    with pytest.raises(ToolError) as exc:
        asyncio.run(server.call_tool("compute_indicators", {"ticker": TICKER, "groups": ["atr"]}))
    message = str(exc.value)
    assert "trend" in message and "volatility" in message


def test_rows_and_ticker_are_mutually_exclusive():
    rows = build_rows(ticker=TICKER, close=[10_000.0, 10_100.0])
    with pytest.raises(ToolError, match="not both"):
        asyncio.run(server.call_tool("compute_indicators", {"rows": rows, "ticker": TICKER}))
    with pytest.raises(ToolError, match="either"):
        asyncio.run(server.call_tool("compute_indicators", {"groups": ["trend"]}))


def test_a_single_row_is_rejected_before_it_reaches_the_engine():
    with pytest.raises(ToolError, match="at least 2 rows"):
        asyncio.run(
            server.call_tool("compute_indicators", {"rows": build_rows(ticker=TICKER, close=[10_000.0])})
        )


def test_a_non_positive_close_in_supplied_rows_is_rejected():
    rows = build_rows(ticker=TICKER, close=[10_000.0, 0.0])
    with pytest.raises(ToolError, match="close"):
        asyncio.run(server.call_tool("compute_indicators", {"rows": rows}))


def test_unknown_ticker_on_compute_returns_a_structured_problem():
    payload = call("compute_indicators", {"ticker": "ZZZZ", "groups": ["trend"]})
    assert payload["error"] == "unknown_ticker"
    assert payload["available_tickers"] == [TICKER]


def test_param_overrides_reach_the_engine():
    payload = call(
        "compute_indicators",
        {"ticker": TICKER, "groups": ["trend"], "params": {"sma_windows": [5]}},
    )
    trend = payload["groups"]["trend"]
    assert "sma_5" in trend and "sma_20" not in trend
    assert trend["sma_5"]["latest"] == pytest.approx(13_700.0)


# --------------------------------------------------------------------------- get_flow_summary


def test_get_flow_summary_answers_the_flow_question_only():
    payload = call("get_flow_summary", {"ticker": TICKER, "window": 5})
    assert payload["ticker"] == TICKER
    assert payload["buy_sell_volume_imbalance_avg"] == pytest.approx(0.2)
    assert payload["foreign_net_value_cum"] == pytest.approx(40_000_000.0)
    assert payload["foreign_net_value_window"] == pytest.approx(5_000_000.0)
    assert payload["foreign_participation_ratio_avg"] == pytest.approx(0.1)
    assert payload["foreign_room_trend"] == pytest.approx(-500.0)
    # Deliberately narrow: no indicator groups in a flow answer.
    assert "groups" not in payload


def test_flow_summary_window_is_validated():
    with pytest.raises(ToolError, match="window"):
        asyncio.run(server.call_tool("get_flow_summary", {"ticker": TICKER, "window": 1}))


# --------------------------------------------------------------------------- audit trail


def test_every_call_is_logged_with_its_arguments_and_result(isolated_env):
    call("get_price_data", {"ticker": TICKER, "lookback_days": 3})
    call("compute_indicators", {"ticker": TICKER, "groups": ["trend"]})
    call("get_flow_summary", {"ticker": TICKER, "window": 5})

    entries = audit_entries(isolated_env)
    assert [e["tool"] for e in entries] == [
        "get_price_data",
        "compute_indicators",
        "get_flow_summary",
    ]
    for entry in entries:
        assert entry["ts"].startswith("20")
        assert entry["duration_ms"] >= 0
        assert "error" not in entry
        assert entry["result"]
    # Bulk rows are collapsed so the log stays readable, but the computed values
    # that the agent could quote are kept in full.
    price_entry = entries[0]
    assert price_entry["result"]["rows"] == "<3 rows>"
    trend = entries[1]["result"]["groups"]["trend"]
    assert trend["sma_20"]["latest"] == pytest.approx(12_950.0)


def test_failed_calls_are_logged_too(isolated_env):
    with pytest.raises(ToolError):
        asyncio.run(server.call_tool("get_price_data", {"ticker": "not a ticker"}))
    entries = audit_entries(isolated_env)
    assert len(entries) == 1
    assert entries[0]["tool"] == "get_price_data"
    assert "invalid arguments" in entries[0]["error"]
    assert "result" not in entries[0]


def test_audit_log_path_follows_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("TA_AGENT_AUDIT_LOG", str(tmp_path / "elsewhere.jsonl"))
    assert server_module.audit_log_path() == tmp_path / "elsewhere.jsonl"


def test_an_unwritable_audit_log_does_not_break_a_working_call(monkeypatch):
    # Auditing is important, but a read-only log directory must not cost the user
    # their answer.
    monkeypatch.setenv("TA_AGENT_AUDIT_LOG", "/proc/nope/tool_calls.jsonl")
    assert call("get_price_data", {"ticker": TICKER, "lookback_days": 2})["row_count"] == 2
