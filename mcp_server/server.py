"""MCP server exposing three tools over the local store and indicator engine.

    uv run python -m mcp_server.server            # stdio transport

Tools
-----
* ``get_price_data``     raw stored rows for a ticker
* ``compute_indicators`` Tier 0 indicator groups
* ``get_flow_summary``   the cheap flow-only answer

Every call is appended to the audit log (``logs/tool_calls.jsonl``, or
``TA_AGENT_AUDIT_LOG``) with its arguments, its computed values and its duration,
so any number the agent reports can be traced back to a real call.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import ValidationError

from data import store
from indicators.engine import GROUPS, UNSUPPORTED_METRICS, EngineError, compute, flow_summary
from mcp_server.tool_schemas import (
    ComputeIndicatorsInput,
    GetFlowSummaryInput,
    GetPriceDataInput,
    RowSource,
    rows_as_dicts,
)

logger = logging.getLogger("ta_agent.mcp")

REPO_ROOT = Path(__file__).resolve().parent.parent

INSTRUCTIONS = f"""
Technical analysis for Vietnamese stocks, computed from a daily close-only feed.

Available per ticker per trading day: previous close, close, matched trade count,
traded value (VND), traded volume, buy/sell trade counts, buy/sell matched
volumes, foreign buy/sell volume and value, and remaining foreign room.

NOT available, and never to be estimated or approximated:
{chr(10).join(f"  - {k}: {v}" for k, v in UNSUPPORTED_METRICS.items())}

Rules:
  - Never state an indicator value that did not come from one of these tools.
  - A result of {{"insufficient_data": true, ...}} means the history is too short.
    Report that, do not substitute a shorter window.
  - close_to_close_volatility is an explicitly labelled ATR substitute. It is not
    ATR and must not be called ATR.
  - Pass `ticker` to compute_indicators / get_flow_summary to have the server load
    rows itself; that is cheaper than round-tripping rows from get_price_data.
  - Indicator groups: {", ".join(GROUPS)}.
""".strip()

server = MCPServer(name="ta-agent", version="0.1.0", instructions=INSTRUCTIONS)


# --------------------------------------------------------------------------- audit


def audit_log_path() -> Path:
    return Path(os.environ.get("TA_AGENT_AUDIT_LOG", REPO_ROOT / "logs" / "tool_calls.jsonl"))


def audit(tool: str, arguments: dict, *, result: Any = None, error: str | None = None,
          started: float | None = None) -> None:
    """Append one line describing a tool call to the audit log. Never fatal."""
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "tool": tool,
        "arguments": _compact(arguments),
        "duration_ms": None if started is None else round((time.perf_counter() - started) * 1000, 2),
    }
    if error is not None:
        entry["error"] = error
    else:
        entry["result"] = _compact(result)
    try:
        path = audit_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, default=str) + "\n")
    except OSError as exc:  # auditing must not break a working tool call
        logger.warning("could not write audit log: %s", exc)
    logger.info("%s %s", tool, "error: " + error if error else "ok")


def _compact(payload: Any) -> Any:
    """Replace bulk row lists with a count so the audit log stays readable."""
    if isinstance(payload, dict):
        out = {}
        for key, value in payload.items():
            if key == "rows" and isinstance(value, list):
                out["rows"] = f"<{len(value)} rows>"
            else:
                out[key] = _compact(value)
        return out
    if isinstance(payload, list) and len(payload) > 20:
        return f"<{len(payload)} items>"
    if isinstance(payload, list):
        return [_compact(v) for v in payload]
    return payload


def _validation_error(exc: ValidationError) -> ToolError:
    """Flatten a pydantic error into one readable line for the agent.

    ToolError, not ValueError: an anticipated failure comes back to the model
    with the message intact, where an unexpected exception would be reduced to
    "Error executing tool ..." plus a traceback in the log.
    """
    parts = [
        f"{'.'.join(str(p) for p in err['loc']) or 'input'}: {err['msg']}"
        for err in exc.errors()[:5]
    ]
    return ToolError("invalid arguments -> " + "; ".join(parts))


# --------------------------------------------------------------------------- helpers


def _load_rows(source: RowSource) -> tuple[list[dict], str | None, dict | None]:
    """Resolve a RowSource to engine-ready rows.

    Returns (rows, ticker, problem). `problem` is a structured payload to return
    to the caller instead of computing, e.g. an unknown ticker.
    """
    if source.rows:
        return rows_as_dicts(source.rows), None, None
    ticker = (source.ticker or "").upper()
    conn = store.connect()
    try:
        rows = store.get_recent(conn, ticker, source.lookback_days)
        if not rows:
            return [], ticker, {
                "error": "unknown_ticker",
                "ticker": ticker,
                "message": f"no stored rows for {ticker}",
                "available_tickers": store.list_tickers(conn),
            }
        return rows, ticker, None
    finally:
        conn.close()


# --------------------------------------------------------------------------- tools


@server.tool(
    description=(
        "Stored daily trading statistics for one ticker, oldest row first. "
        "lookback_days counts the most recent trading rows, not calendar days. "
        "Pass start/end to pin an explicit ISO date range instead."
    )
)
def get_price_data(
    ticker: str,
    lookback_days: int = 300,
    start: str | None = None,
    end: str | None = None,
) -> dict:
    started = time.perf_counter()
    arguments = {"ticker": ticker, "lookback_days": lookback_days, "start": start, "end": end}
    try:
        params = GetPriceDataInput(
            ticker=ticker, lookback_days=lookback_days, start=start, end=end
        )
    except ValidationError as exc:
        error = _validation_error(exc)
        audit("get_price_data", arguments, error=str(error), started=started)
        raise error from exc

    conn = store.connect()
    try:
        if params.start or params.end:
            rows = store.get_range(conn, params.ticker, params.start, params.end)
        else:
            rows = store.get_recent(conn, params.ticker, params.lookback_days)
        available = store.list_tickers(conn)
    finally:
        conn.close()

    if not rows:
        result = {
            "ticker": params.ticker,
            "rows": [],
            "row_count": 0,
            "error": "no_rows",
            "message": (
                f"no stored rows for {params.ticker}"
                + ("" if params.ticker in available else " (ticker not in the store)")
            ),
            "available_tickers": available,
        }
    else:
        result = {
            "ticker": params.ticker,
            "row_count": len(rows),
            "date_range": {"start": rows[0]["date"], "end": rows[-1]["date"]},
            "rows": [{k: v for k, v in row.items() if k != "ticker"} for row in rows],
        }
    audit("get_price_data", arguments, result=result, started=started)
    return result


@server.tool(
    description=(
        "Compute Tier 0 indicator groups. Supply either `ticker` (server loads the "
        "rows, cheaper) or `rows` from get_price_data. Groups: trend, momentum, "
        "volatility, volume_flow, trade_flow, value_flow, foreign_flow. Any group "
        "whose history is too short returns {'insufficient_data': true, ...} rather "
        "than a number."
    )
)
def compute_indicators(
    rows: list[dict] | None = None,
    groups: list[str] | None = None,
    ticker: str | None = None,
    lookback_days: int = 300,
    params: dict | None = None,
    series_tail: int = 10,
) -> dict:
    started = time.perf_counter()
    arguments = {
        "rows": rows,
        "groups": groups,
        "ticker": ticker,
        "lookback_days": lookback_days,
        "params": params,
        "series_tail": series_tail,
    }
    try:
        request = ComputeIndicatorsInput(
            rows=rows,
            ticker=ticker,
            lookback_days=lookback_days,
            groups=groups or list(GROUPS),
            params=params,
            series_tail=series_tail,
        )
    except ValidationError as exc:
        error = _validation_error(exc)
        audit("compute_indicators", arguments, error=str(error), started=started)
        raise error from exc

    resolved, resolved_ticker, problem = _load_rows(request)
    if problem:
        audit("compute_indicators", arguments, result=problem, started=started)
        return problem

    try:
        result = compute(
            resolved,
            request.groups,
            request.params,
            series_tail=request.series_tail,
        )
    except EngineError as exc:
        audit("compute_indicators", arguments, error=str(exc), started=started)
        raise ToolError(str(exc)) from exc

    result["ticker"] = resolved_ticker or _ticker_hint(request)
    result["groups_requested"] = list(request.groups)
    audit("compute_indicators", arguments, result=result, started=started)
    return result


@server.tool(
    description=(
        "Cheap flow-only answer: buy/sell imbalance by volume and by trade count, "
        "cumulative and window foreign net value in VND, foreign participation "
        "ratio, and foreign room trend. Use this for 'are foreigners buying this "
        "week' style questions instead of a full indicator pass."
    )
)
def get_flow_summary(
    rows: list[dict] | None = None,
    window: int = 5,
    ticker: str | None = None,
    lookback_days: int = 300,
) -> dict:
    started = time.perf_counter()
    arguments = {
        "rows": rows,
        "window": window,
        "ticker": ticker,
        "lookback_days": lookback_days,
    }
    try:
        request = GetFlowSummaryInput(
            rows=rows, ticker=ticker, lookback_days=lookback_days, window=window
        )
    except ValidationError as exc:
        error = _validation_error(exc)
        audit("get_flow_summary", arguments, error=str(error), started=started)
        raise error from exc

    resolved, resolved_ticker, problem = _load_rows(request)
    if problem:
        audit("get_flow_summary", arguments, result=problem, started=started)
        return problem

    try:
        result = flow_summary(resolved, request.window)
    except EngineError as exc:
        audit("get_flow_summary", arguments, error=str(exc), started=started)
        raise ToolError(str(exc)) from exc

    result["ticker"] = resolved_ticker or _ticker_hint(request)
    audit("get_flow_summary", arguments, result=result, started=started)
    return result


def _ticker_hint(request: RowSource) -> str | None:
    """Ticker carried on caller-supplied rows, if they happen to include it."""
    if not request.rows:
        return None
    for row in request.rows:
        if row.ticker:
            return row.ticker.upper()
    return None


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("TA_AGENT_LOG_LEVEL", "INFO"),
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,  # stdout carries the MCP protocol
    )
    logger.info("ta-agent MCP server: db=%s audit=%s", store.default_db_path(), audit_log_path())
    server.run("stdio")


if __name__ == "__main__":
    main()
