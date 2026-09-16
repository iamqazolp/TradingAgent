"""MCP server exposing the analysis tools over the local store and indicator engine.

    uv run python -m mcp_server.server            # stdio transport

Tools
-----
* ``get_price_data``            raw stored rows for a ticker
* ``compute_indicators``        Tier 0 indicator groups
* ``get_flow_summary``          the cheap flow-only answer
* ``analyze_multi_horizon``     scoped short / mid / long-term analysis
* ``compute_weekly_indicators`` indicator groups on weekly bars
* ``compare_tickers``           2-5 tickers head to head

Tools that load rows server-side accept ``as_of`` to compute values as they
stood on a past date, using only rows up to and including it.

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
from indicators.comparison import compare_multiple_tickers
from indicators.engine import GROUPS, UNSUPPORTED_METRICS, EngineError, compute, flow_summary, multi_horizon_compute
from indicators.weekly import aggregate_weekly, weekly_quality_flags
from mcp_server.cache import get_cache
from mcp_server.tool_schemas import (
    AnalyzeMultiHorizonInput,
    CompareTickersInput,
    ComputeIndicatorsInput,
    ComputeWeeklyInput,
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
    """Replace bulk row lists with a count so the audit log stays readable.

    Only truncates ``rows`` keys (the raw input data) and very large generic
    lists (>200 items).  Indicator result arrays (typically ≤50 items) are
    preserved in full so every number the agent quotes can be traced.
    """
    if isinstance(payload, dict):
        out = {}
        for key, value in payload.items():
            if key == "rows" and isinstance(value, list):
                out["rows"] = f"<{len(value)} rows>"
            elif key == "ticker_data" and isinstance(value, dict):
                out["ticker_data"] = {
                    t: f"<{len(r)} rows>" if isinstance(r, list) else r
                    for t, r in value.items()
                }
            else:
                out[key] = _compact(value)
        return out
    if isinstance(payload, list) and len(payload) > 200:
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

    When `as_of` is set, only rows up to and including that date are loaded, so
    every value the engine reports is the value that stood on that date. Rolling
    indicators must never see a row after the date being asked about.
    """
    if source.rows:
        return rows_as_dicts(source.rows), None, None
    ticker = (source.ticker or "").upper()
    as_of = getattr(source, "as_of", None)
    conn = store.connect()
    try:
        if as_of:
            history = store.get_range(conn, ticker, None, as_of)
            rows = history[-source.lookback_days:]
        else:
            rows = store.get_recent(conn, ticker, source.lookback_days)
        if not rows:
            available = store.list_tickers(conn)
            if as_of and ticker in available:
                earliest, latest = store.date_bounds(conn, ticker)
                return [], ticker, {
                    "error": "no_rows_before_as_of",
                    "ticker": ticker,
                    "as_of": as_of,
                    "stored_range": {"start": earliest, "end": latest},
                    "message": (
                        f"Không có phiên giao dịch nào của {ticker} vào hoặc trước "
                        f"{as_of}. Dữ liệu chỉ có từ {earliest} đến {latest}."
                    ),
                }
            return [], ticker, {
                "error": "unknown_ticker",
                "ticker": ticker,
                "message": f"no stored rows for {ticker}",
                "available_tickers": available,
            }
        return rows, ticker, None
    finally:
        conn.close()


def _as_of_meta(source: RowSource, rows: list[dict]) -> dict | None:
    """Describe how an `as_of` request was resolved, for the caller to report."""
    as_of = getattr(source, "as_of", None)
    if not as_of or not rows:
        return None
    effective = rows[-1]["date"]
    meta = {
        "as_of_requested": as_of,
        "as_of_effective": effective,
        "rows_used_through": effective,
        "is_trading_day": effective == as_of,
    }
    if effective != as_of:
        meta["note"] = (
            f"{as_of} không phải phiên giao dịch; giá trị được tính đến phiên gần "
            f"nhất trước đó là {effective}."
        )
    else:
        meta["note"] = (
            f"Mọi giá trị 'latest' trong kết quả này là giá trị TẠI NGÀY {effective}, "
            "không phải phiên gần nhất hiện tại."
        )
    return meta


# --------------------------------------------------------------------------- tools


@server.tool(
    description=(
        "Stored daily trading statistics for one ticker, oldest row first. "
        "Feed is CLOSE-ONLY (contains close, volume, value, orders, foreign flow; NO intraday high/low). "
        "lookback_days counts the most recent trading rows (e.g. lookback_days=10 for the last 10 sessions). "
        "IMPORTANT: When asked for 'recent', 'latest', or 'last N days/sessions', pass ONLY `ticker` and `lookback_days`. "
        "DO NOT guess or pass `start` or `end` dates unless the user explicitly specified calendar dates in their query."
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

    # Everything that needs the connection happens inside this block. Building
    # the "no rows in that range" message needs date_bounds, which was
    # previously called after the `finally` had already closed the connection —
    # so the one path meant to correct a hallucinated date range raised
    # sqlite3.ProgrammingError instead of returning its guidance.
    conn = store.connect()
    try:
        available = store.list_tickers(conn)
        if params.start or params.end:
            rows = store.get_range(conn, params.ticker, params.start, params.end)
            if not rows and params.ticker in available:
                # Caller asked for recent rows but also supplied a date range the
                # store cannot satisfy. Fall back to the most recent sessions
                # rather than failing the whole query.
                rows = store.get_recent(conn, params.ticker, params.lookback_days)
                fell_back_from_range = bool(rows)
            else:
                fell_back_from_range = False
        else:
            rows = store.get_recent(conn, params.ticker, params.lookback_days)
            fell_back_from_range = False

        if not rows:
            if params.ticker not in available:
                msg = f"no stored rows for {params.ticker} (ticker not in the store)"
            elif params.start or params.end:
                earliest, latest = store.date_bounds(conn, params.ticker)
                msg = (
                    f"no rows for {params.ticker} in range {params.start}..{params.end}. "
                    f"Stored data for {params.ticker} is between {earliest} and {latest}. "
                    f"To get the most recent sessions, call get_price_data with "
                    f"lookback_days={params.lookback_days} and omit start/end."
                )
            else:
                msg = f"no stored rows for {params.ticker}"
        else:
            msg = None
        date_span = store.date_bounds(conn, params.ticker) if fell_back_from_range else None
    finally:
        conn.close()

    if not rows:
        result = {
            "ticker": params.ticker,
            "rows": [],
            "row_count": 0,
            "error": "no_rows",
            "message": msg,
            "available_tickers": available,
        }
    else:
        result = {
            "ticker": params.ticker,
            "row_count": len(rows),
            "date_range": {"start": rows[0]["date"], "end": rows[-1]["date"]},
            "rows": [{k: v for k, v in row.items() if k != "ticker"} for row in rows],
        }
        if date_span is not None:
            # Say so when the requested range was ignored, otherwise the caller
            # believes it received the dates it asked for.
            result["requested_range_empty"] = {
                "start": params.start,
                "end": params.end,
                "stored_range": {"start": date_span[0], "end": date_span[1]},
                "message": (
                    f"Không có dữ liệu trong khoảng {params.start}..{params.end}; "
                    f"đã trả về {len(rows)} phiên gần nhất thay thế. "
                    f"Dữ liệu {params.ticker} chỉ có từ {date_span[0]} đến {date_span[1]}."
                ),
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
    rows: list[dict] | str | None = None,
    groups: list[str] | str | None = None,
    ticker: str | None = None,
    lookback_days: int = 300,
    params: dict | None = None,
    series_tail: int = 20,
    as_of: str | None = None,
) -> dict:
    started = time.perf_counter()
    arguments = {
        "rows": rows,
        "groups": groups,
        "ticker": ticker,
        "lookback_days": lookback_days,
        "params": params,
        "series_tail": series_tail,
        "as_of": as_of,
    }
    try:
        request = ComputeIndicatorsInput(
            rows=rows,
            ticker=ticker,
            lookback_days=lookback_days,
            groups=groups or list(GROUPS),
            params=params,
            series_tail=series_tail,
            as_of=as_of,
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
    as_of_meta = _as_of_meta(request, resolved)
    if as_of_meta:
        result["as_of"] = as_of_meta
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
    rows: list[dict] | str | None = None,
    window: int = 5,
    ticker: str | None = None,
    lookback_days: int = 300,
    as_of: str | None = None,
) -> dict:
    started = time.perf_counter()
    arguments = {
        "rows": rows,
        "window": window,
        "ticker": ticker,
        "lookback_days": lookback_days,
        "as_of": as_of,
    }
    try:
        request = GetFlowSummaryInput(
            rows=rows, ticker=ticker, lookback_days=lookback_days, window=window,
            as_of=as_of,
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
    as_of_meta = _as_of_meta(request, resolved)
    if as_of_meta:
        result["as_of"] = as_of_meta
    audit("get_flow_summary", arguments, result=result, started=started)
    return result


@server.tool(
    description=(
        "Multi-horizon technical analysis of one ticker, scoped to the question. "
        "For each requested horizon (ngắn / trung / dài hạn) it returns a verdict, "
        "a confidence level with its reason, the scored evidence per indicator "
        "group, conflicting signals and an invalidation level, plus daily "
        "indicators and support/resistance levels measured from closes.\n"
        "SET `scope` TO MATCH THE QUESTION — a scoped call is far cheaper:\n"
        "  scope='full'       phân tích toàn diện / đa khung (all 3 horizons, "
        "measured levels, 52-week stats)\n"
        "  scope='short_term' ngắn hạn only (~43% of full)\n"
        "  scope='mid_term'   trung hạn only\n"
        "  scope='long_term'  dài hạn only\n"
        "  scope='levels'     hỗ trợ / kháng cự only (~23% of full)\n"
        "Sections a scope skips are named in `sections_omitted` and are NOT "
        "missing data. Loads rows server-side — just pass the ticker."
    )
)
def analyze_multi_horizon(
    ticker: str,
    lookback_days: int = 500,
    scope: str = "full",
    series_tail: int = 5,
    weekly_series_tail: int = 5,
    detail: str = "compact",
    as_of: str | None = None,
) -> dict:
    started = time.perf_counter()
    arguments = {
        "ticker": ticker,
        "lookback_days": lookback_days,
        "scope": scope,
        "series_tail": series_tail,
        "weekly_series_tail": weekly_series_tail,
        "detail": detail,
        "as_of": as_of,
    }
    try:
        params = AnalyzeMultiHorizonInput(
            ticker=ticker,
            lookback_days=lookback_days,
            scope=scope,
            series_tail=series_tail,
            weekly_series_tail=weekly_series_tail,
            detail=detail,
            as_of=as_of,
        )
    except ValidationError as exc:
        error = _validation_error(exc)
        audit("analyze_multi_horizon", arguments, error=str(error), started=started)
        raise error from exc

    cache = get_cache()
    cache_params = {
        "ticker": params.ticker,
        "lookback_days": params.lookback_days,
        "scope": params.scope,
        "series_tail": params.series_tail,
        "weekly_series_tail": params.weekly_series_tail,
        "detail": params.detail,
        "as_of": params.as_of,
    }
    cached = cache.get("analyze_multi_horizon", **cache_params)
    if cached is not None:
        audit("analyze_multi_horizon", arguments, result=cached, started=started)
        return cached

    conn = store.connect()
    try:
        if params.as_of:
            # Only rows up to the requested date, so no indicator sees the future.
            rows = store.get_range(conn, params.ticker, None, params.as_of)[
                -params.lookback_days:
            ]
        else:
            rows = store.get_recent(conn, params.ticker, params.lookback_days)
        if not rows:
            available = store.list_tickers(conn)
            if params.as_of and params.ticker in available:
                earliest, latest = store.date_bounds(conn, params.ticker)
                result = {
                    "error": "no_rows_before_as_of",
                    "ticker": params.ticker,
                    "as_of": params.as_of,
                    "stored_range": {"start": earliest, "end": latest},
                    "message": (
                        f"Không có phiên giao dịch nào của {params.ticker} vào hoặc "
                        f"trước {params.as_of}. Dữ liệu chỉ có từ {earliest} đến {latest}."
                    ),
                }
            else:
                result = {
                    "error": "unknown_ticker",
                    "ticker": params.ticker,
                    "message": f"no stored rows for {params.ticker}",
                    "available_tickers": available,
                }
            audit("analyze_multi_horizon", arguments, result=result, started=started)
            return result
    finally:
        conn.close()

    try:
        result = multi_horizon_compute(
            rows,
            series_tail=params.series_tail,
            weekly_series_tail=params.weekly_series_tail,
            include_series=False,
            detail=params.detail,
            scope=params.scope,
        )
    except EngineError as exc:
        audit("analyze_multi_horizon", arguments, error=str(exc), started=started)
        raise ToolError(str(exc)) from exc

    result["ticker"] = params.ticker
    if params.as_of:
        effective = rows[-1]["date"]
        result["as_of"] = {
            "as_of_requested": params.as_of,
            "as_of_effective": effective,
            "is_trading_day": effective == params.as_of,
            "note": (
                f"Toàn bộ phân tích được tính TẠI NGÀY {effective}"
                + ("" if effective == params.as_of
                   else f" ({params.as_of} không phải phiên giao dịch)")
                + ", không phải phiên gần nhất hiện tại."
            ),
        }
    cache.put(result, "analyze_multi_horizon", **cache_params)
    audit("analyze_multi_horizon", arguments, result=result, started=started)
    return result


@server.tool(
    description=(
        "Compute indicators on weekly-aggregated bars. Weekly bars are "
        "derived from daily data (close = last close of the week, "
        "volume/value/flow = sum of the week). Use for 'weekly RSI', "
        "'weekly MACD', 'weekly trend' questions. Groups: "
        + ", ".join(GROUPS) + "."
    )
)
def compute_weekly_indicators(
    ticker: str | None = None,
    lookback_days: int = 500,
    groups: list[str] | str | None = None,
    series_tail: int = 26,
    rows: list[dict] | str | None = None,
) -> dict:
    started = time.perf_counter()
    arguments = {
        "ticker": ticker,
        "lookback_days": lookback_days,
        "groups": groups,
        "series_tail": series_tail,
        "rows": rows,
    }
    try:
        request = ComputeWeeklyInput(
            ticker=ticker,
            lookback_days=lookback_days,
            groups=groups or list(GROUPS),
            series_tail=series_tail,
            rows=rows,
        )
    except ValidationError as exc:
        error = _validation_error(exc)
        audit("compute_weekly_indicators", arguments, error=str(error), started=started)
        raise error from exc

    resolved, resolved_ticker, problem = _load_rows(request)
    if problem:
        audit("compute_weekly_indicators", arguments, result=problem, started=started)
        return problem

    try:
        from indicators.engine import rows_to_frame, _compute_core
        frame = rows_to_frame(resolved)
        weekly_frame = aggregate_weekly(frame)

        _MIN_WEEKLY_BARS = 10
        if len(weekly_frame) < _MIN_WEEKLY_BARS:
            result = {
                "error": "insufficient_weekly_data",
                "weekly_bars_available": int(len(weekly_frame)),
                "weekly_bars_min_required": _MIN_WEEKLY_BARS,
                "message": (
                    f"Only {len(weekly_frame)} weekly bars available "
                    f"(need at least {_MIN_WEEKLY_BARS}). "
                    f"Try increasing lookback_days (current: {request.lookback_days})."
                ),
            }
            audit("compute_weekly_indicators", arguments, result=result, started=started)
            return result

        wf_clean = weekly_frame.dropna(subset=["prev_close"])
        result = _compute_core(
            wf_clean, request.groups, series_tail=request.series_tail,
        )
        result["timeframe"] = "weekly"
        result["weekly_bars_used"] = int(len(wf_clean))

        flags = weekly_quality_flags(weekly_frame)
        if flags:
            result["quality_flags"] = flags

    except EngineError as exc:
        audit("compute_weekly_indicators", arguments, error=str(exc), started=started)
        raise ToolError(str(exc)) from exc

    result["ticker"] = resolved_ticker or _ticker_hint(request)
    result["groups_requested"] = list(request.groups)
    audit("compute_weekly_indicators", arguments, result=result, started=started)
    return result


@server.tool(
    description=(
        "Compare 2 to 5 Vietnamese stock tickers head-to-head. "
        "Returns a 52-week performance table (return, close high/low with dates, "
        "max drawdown, average volume/value), a moving-average position table "
        "(vs SMA 20/50/100/200 and EMA 20/50/200, RSI, MACD normalized by price), "
        "a support/resistance table measured from closes, and a relative strength "
        "assessment covering EVERY compared ticker. Tickers with too little "
        "history are listed in `tickers_excluded` rather than dropped. "
        "Pure objective technical analysis, zero buy/sell advice."
    )
)
def compare_tickers(
    tickers: list[str] | str,
    lookback_days: int = 250,
    detail: str = "compact",
) -> dict:
    started = time.perf_counter()
    arguments = {
        "tickers": tickers,
        "lookback_days": lookback_days,
        "detail": detail,
    }
    try:
        params = CompareTickersInput(
            tickers=tickers,
            lookback_days=lookback_days,
            detail=detail,
        )
    except ValidationError as exc:
        error = _validation_error(exc)
        audit("compare_tickers", arguments, error=str(error), started=started)
        raise error from exc

    cache = get_cache()
    cache_params = {
        "tickers": sorted(params.tickers),
        "lookback_days": params.lookback_days,
        "detail": params.detail,
    }
    cached = cache.get("compare_tickers", **cache_params)
    if cached is not None:
        audit("compare_tickers", arguments, result=cached, started=started)
        return cached

    conn = store.connect()
    ticker_data = {}
    missing_tickers = []
    try:
        available = store.list_tickers(conn)
        for t in params.tickers:
            if t not in available:
                missing_tickers.append(t)
            else:
                rows = store.get_recent(conn, t, params.lookback_days)
                ticker_data[t] = rows
    finally:
        conn.close()

    if missing_tickers:
        result = {
            "error": "tickers_not_found",
            "missing_tickers": missing_tickers,
            "available_tickers": available,
            "message": f"Tickers not found in store: {', '.join(missing_tickers)}",
        }
        audit("compare_tickers", arguments, result=result, started=started)
        return result

    try:
        result = compare_multiple_tickers(
            ticker_data,
            window_days=params.lookback_days,
            include_series=False,
            detail=params.detail,
        )
    except EngineError as exc:
        audit("compare_tickers", arguments, error=str(exc), started=started)
        raise ToolError(str(exc)) from exc

    cache.put(result, "compare_tickers", **cache_params)
    audit("compare_tickers", arguments, result=result, started=started)
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
