"""Engine: turn stored rows into a frame, dispatch indicator groups, serialize.

This is the only module in the package that knows about row dicts and JSON. The
group modules stay pandas-in / dict-out.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Callable

import numpy as np
import pandas as pd

from indicators import finite, insufficient, is_insufficient, safe_div, safe_series_div
from indicators.foreign_flow import (
    foreign_flow_group,
    foreign_net_value,
    foreign_participation_ratio,
    foreign_room_trend,
)
from indicators.momentum import momentum_group
from indicators.trade_flow import buy_sell_count_imbalance, trade_flow_group
from indicators.trend import trend_group
from indicators.value_flow import value_flow_group
from indicators.volatility import volatility_group
from indicators.volume_flow import (
    buy_sell_volume_imbalance,
    prev_close_mismatches,
    volume_flow_group,
)

#: Columns the engine expects on every row.
REQUIRED_COLUMNS = (
    "date",
    "prev_close",
    "close",
    "total_trade",  
    "total_value",
    "total_volume",
    "buy_count",
    "sell_count",
    "buy_volume",
    "sell_volume",
    "foreign_buy_volume",
    "foreign_sell_volume",
    "foreign_buy_value",
    "foreign_sell_value",
    "foreign_room",
)

#: Groups dispatched with the full DataFrame.
_FRAME_GROUPS: dict[str, Callable[[pd.DataFrame, dict | None], dict]] = {
    "trend": trend_group,
    "momentum": momentum_group,
    "volatility": volatility_group,
    "volume_flow": volume_flow_group,
    "trade_flow": trade_flow_group,
    "value_flow": value_flow_group,
    "foreign_flow": foreign_flow_group,
}

GROUPS: tuple[str, ...] = tuple(_FRAME_GROUPS)

#: What this feed cannot support, and the honest substitute where one exists.
#: Surfaced to the agent so it declines rather than fabricates.
UNSUPPORTED_METRICS: dict[str, str] = {
    "open_price": "not in this feed; only close and previous close are available",
    "overnight_gap": "needs the open price, which this feed does not provide",
    "candle_body_ratio": "needs the open price, which this feed does not provide",
    "atr": (
        "needs high/low; use close_to_close_volatility as an explicitly labelled "
        "substitute for stop sizing, or atr when high/low are present"
    ),
    "adx": "needs high/low; use adx in trend group when high/low are present",
    "stochastic": "needs high/low; use stochastic in momentum group when high/low are present",
    "ichimoku": "needs high/low; use SMA alignment plus MACD for trend structure",
    "vwap": (
        "true intraday VWAP needs tick data; total_value / total_volume gives a "
        "daily average traded price, which is not the same thing"
    ),
    "wick_support_resistance": "needs high/low; no substitute available from close alone",
    "market_breadth": "needs multi-ticker index data; out of scope for v1",
    "fundamentals": "not in this feed",
    "news_sentiment": "not in this feed",
}


class EngineError(ValueError):
    """Input rows are unusable (missing columns, empty, unsortable)."""


# --------------------------------------------------------------------------- frames


def rows_to_frame(rows: list[dict]) -> pd.DataFrame:
    """Build a date-indexed, ascending, numeric frame from stored row dicts."""
    if not rows:
        raise EngineError("no rows supplied")
    first = rows[0]
    missing = [c for c in REQUIRED_COLUMNS if c not in first and c not in ("open", "high", "low")]
    if missing:
        raise EngineError(f"rows are missing required columns: {', '.join(missing)}")
    
    # Ensure open, high, low exist with valid geometry
    norm_rows = []
    for r in rows:
        item = dict(r)
        close = float(r["close"])
        prev_close = float(r.get("prev_close", close))
        if "open" not in item or item["open"] is None:
            item["open"] = prev_close
        if "high" not in item or item["high"] is None:
            item["high"] = max(close, float(item["open"]), prev_close)
        if "low" not in item or item["low"] is None:
            item["low"] = min(close, float(item["open"]), prev_close)
        norm_rows.append(item)

    frame = pd.DataFrame(norm_rows)
    frame["date"] = frame["date"].map(_iso_date)
    frame = frame.drop_duplicates(subset="date", keep="last")
    frame = frame.sort_values("date").set_index("date")
    for column in REQUIRED_COLUMNS:
        if column != "date" and column in frame.columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce").astype("float64")
    return frame


def _iso_date(value: Any) -> str:
    if isinstance(value, datetime):
        if value.hour == 0 and value.minute == 0 and value.second == 0:
            return value.strftime("%Y-%m-%d")
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    s = str(value).strip()
    return s


def derived_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Add the read-time derived columns from the plan's section 5."""
    out = frame.copy()
    out["net_buy_volume"] = out["buy_volume"] - out["sell_volume"]
    out["net_buy_count"] = out["buy_count"] - out["sell_count"]
    out["avg_buy_trade_size"] = safe_series_div(out["buy_volume"], out["buy_count"])
    out["avg_sell_trade_size"] = safe_series_div(out["sell_volume"], out["sell_count"])
    out["avg_trade_value"] = safe_series_div(out["total_value"], out["total_trade"])
    out["foreign_net_volume"] = out["foreign_buy_volume"] - out["foreign_sell_volume"]
    out["foreign_net_value"] = out["foreign_buy_value"] - out["foreign_sell_value"]
    out["foreign_total_volume"] = out["foreign_buy_volume"] + out["foreign_sell_volume"]
    out["foreign_room_change"] = out["foreign_room"].diff()
    return out


# --------------------------------------------------------------------------- compute


def compute(
    rows: list[dict],
    groups: list[str] | tuple[str, ...] | None = None,
    params: dict | None = None,
    *,
    series_tail: int = 10,
) -> dict:
    """Compute the requested indicator groups from `rows`.

    `groups` is a subset of :data:`GROUPS`; None means all of them. Series are
    trimmed to their last `series_tail` points on the way out.
    """
    requested = tuple(groups) if groups else GROUPS
    unknown = [g for g in requested if g not in GROUPS]
    if unknown:
        raise EngineError(
            f"unknown group(s): {', '.join(unknown)}; valid groups are {', '.join(GROUPS)}"
        )

    frame = rows_to_frame(rows)
    close = frame["close"]
    results: dict[str, Any] = {}
    for group in requested:
        results[group] = _FRAME_GROUPS[group](frame, params)

    return {
        "rows_used": int(len(frame)),
        "date_range": {"start": str(frame.index[0]), "end": str(frame.index[-1])},
        "latest_close": finite(close.iloc[-1]),
        "groups": serialize(results, series_tail=series_tail),
        "data_quality": data_quality(frame),
    }


def flow_summary(rows: list[dict], window: int = 5) -> dict:
    """The cheap flow-only answer: is money, and specifically foreign money, coming in?

    Deliberately narrow so an agent can answer "are foreigners net buying this
    week?" without paying for a full indicator pass.
    """
    frame = rows_to_frame(rows)
    volume_imbalance = buy_sell_volume_imbalance(frame, window)
    count_imbalance = buy_sell_count_imbalance(frame, window)
    net_value = foreign_net_value(frame)
    participation = foreign_participation_ratio(frame, window)
    room = foreign_room_trend(frame, window)

    def scalar(result: dict, key: str) -> Any:
        return result if is_insufficient(result) else result.get(key)

    recent_net_value = None
    if not is_insufficient(net_value) and len(frame) >= 1:
        recent = frame["foreign_buy_value"] - frame["foreign_sell_value"]
        recent_net_value = finite(recent.tail(window).sum())

    return {
        "window": window,
        "rows_used": int(len(frame)),
        "date_range": {"start": str(frame.index[0]), "end": str(frame.index[-1])},
        "buy_sell_volume_imbalance_avg": scalar(volume_imbalance, "latest_rolling_avg"),
        "buy_sell_count_imbalance_avg": scalar(count_imbalance, "latest_rolling_avg"),
        "foreign_net_value_cum": scalar(net_value, "cumulative"),
        "foreign_net_value_window": recent_net_value,
        "foreign_participation_ratio_avg": scalar(participation, "latest_rolling_avg"),
        "foreign_room_trend": scalar(room, "latest"),
        "notes": _flow_notes(volume_imbalance, count_imbalance, net_value, room),
    }


def _flow_notes(volume_imbalance, count_imbalance, net_value, room) -> list[str]:
    """Short, factual caveats attached to a flow answer."""
    notes: list[str] = []
    if not is_insufficient(room) and room.get("suspected_structural_changes"):
        notes.append(
            "foreign room moved by more than that day's foreign trading can explain on "
            f"{len(room['suspected_structural_changes'])} day(s); check for an ownership "
            "limit or charter capital change before reading it as flow"
        )
    if not is_insufficient(net_value) and not is_insufficient(volume_imbalance):
        stance = net_value.get("stance")
        bias = volume_imbalance.get("bias")
        if stance == "net_buying" and bias == "sell_side":
            notes.append("foreign money is net buying while overall matched flow is sell-side")
        elif stance == "net_selling" and bias == "buy_side":
            notes.append("foreign money is net selling while overall matched flow is buy-side")
    if not is_insufficient(volume_imbalance) and not is_insufficient(count_imbalance):
        vol = volume_imbalance.get("latest_rolling_avg")
        cnt = count_imbalance.get("latest_rolling_avg")
        if vol is not None and cnt is not None and vol * cnt < 0:
            notes.append(
                "volume imbalance and trade-count imbalance point in opposite "
                "directions: one side is trading in larger tickets"
            )
    return notes


# --------------------------------------------------------------------------- quality


def data_quality(frame: pd.DataFrame, gap_threshold_days: int = 5) -> dict:
    """Data-quality flags that must travel with any numeric answer.

    Reports calendar gaps, zero-volume (likely halted) days, and closes that
    disagree with the next row's `prev_close`, which is the fingerprint of an
    unadjusted split or stock dividend.
    """
    dates = [str(d) for d in frame.index]
    gaps: list[dict] = []
    parsed: list[date | None] = []
    for value in dates:
        try:
            parsed.append(datetime.strptime(value, "%Y-%m-%d").date())
        except ValueError:
            parsed.append(None)
    for i in range(1, len(parsed)):
        left, right = parsed[i - 1], parsed[i]
        if left is None or right is None:
            continue
        delta = (right - left).days
        if delta > gap_threshold_days:
            gaps.append({"from": dates[i - 1], "to": dates[i], "calendar_days": delta})

    zero_volume = [str(d) for d, v in frame["total_volume"].items() if (finite(v) or 0) <= 0]
    mismatches = prev_close_mismatches(frame["close"], frame["prev_close"], 0.005)
    corporate_action_suspects = [
        m | {"ratio": safe_div(m["prior_close"], m["prev_close"])} for m in mismatches
    ]

    return {
        "rows": int(len(frame)),
        "date_range": {"start": dates[0], "end": dates[-1]} if dates else None,
        "calendar_gaps": gaps[:10],
        "calendar_gap_count": len(gaps),
        "zero_volume_days": zero_volume[:10],
        "zero_volume_day_count": len(zero_volume),
        "suspected_corporate_actions": corporate_action_suspects[:10],
        "suspected_corporate_action_count": len(corporate_action_suspects),
        "warnings": _quality_warnings(gaps, zero_volume, corporate_action_suspects),
    }


def _quality_warnings(gaps: list, zero_volume: list, corporate_actions: list) -> list[str]:
    warnings: list[str] = []
    if corporate_actions:
        warnings.append(
            f"{len(corporate_actions)} date(s) where prev_close does not match the prior "
            "close: the feed is probably not adjusted for a split or stock dividend, and "
            "close-based indicators spanning that date are distorted"
        )
    if gaps:
        warnings.append(
            f"{len(gaps)} calendar gap(s) longer than a long weekend: holidays or a "
            "trading halt. Gaps are left as gaps, never filled with zeros"
        )
    if zero_volume:
        warnings.append(
            f"{len(zero_volume)} day(s) with zero total volume: likely halted, treated as "
            "gaps in flow metrics"
        )
    return warnings


# --------------------------------------------------------------------------- serialize


def serialize(obj: Any, series_tail: int = 10) -> Any:
    """Recursively convert pandas/numpy values into JSON-safe primitives.

    Series become ``{"dates": [...], "values": [...]}`` trimmed to the last
    `series_tail` points; NaN and inf become ``null``.
    """
    if isinstance(obj, pd.Series):
        tail = obj if series_tail is None or series_tail <= 0 else obj.tail(series_tail)
        return {
            "dates": [str(i) for i in tail.index],
            "values": [finite(v) for v in tail.to_numpy()],
        }
    if isinstance(obj, pd.DataFrame):  # pragma: no cover - not produced today
        return {c: serialize(obj[c], series_tail) for c in obj.columns}
    if isinstance(obj, dict):
        return {k: serialize(v, series_tail) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [serialize(v, series_tail) for v in obj]
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return finite(obj)
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, float):
        return finite(obj)
    return obj


def unsupported(metric: str) -> dict:
    """Structured refusal for a metric this feed cannot support."""
    key = metric.strip().lower().replace(" ", "_")
    reason = UNSUPPORTED_METRICS.get(key)
    if reason is None:
        return insufficient(f"unknown metric {metric!r}", 0)
    return {"unsupported": True, "metric": key, "reason": reason}
