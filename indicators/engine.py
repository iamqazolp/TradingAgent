"""Engine: turn stored rows into a frame, dispatch indicator groups, serialize.

This is the only module in the package that knows about row dicts and JSON. The
group modules stay pandas-in / dict-out.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Callable

import numpy as np
import pandas as pd

from indicators import (
    finite,
    insufficient,
    is_insufficient,
    safe_div,
    safe_series_div,
    with_vi_labels,
)
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
from indicators.weekly import aggregate_weekly, weekly_quality_flags
from indicators.horizon import horizon_analysis
from indicators.levels import key_levels
from indicators.strategies import suggest_strategies
from indicators.stats_52w import stats_52w

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

#: Groups that only need the close series.
_CLOSE_GROUPS: dict[str, Callable[[pd.Series, dict | None], dict]] = {
    "trend": trend_group,
    "momentum": momentum_group,
    "volatility": volatility_group,
}

#: Groups that need the full frame.
_FRAME_GROUPS: dict[str, Callable[[pd.DataFrame, dict | None], dict]] = {
    "volume_flow": volume_flow_group,
    "trade_flow": trade_flow_group,
    "value_flow": value_flow_group,
    "foreign_flow": foreign_flow_group,
}

GROUPS: tuple[str, ...] = tuple(_CLOSE_GROUPS) + tuple(_FRAME_GROUPS)

#: What this feed cannot support, and the honest substitute where one exists.
#: Surfaced to the agent so it declines rather than fabricates.
UNSUPPORTED_METRICS: dict[str, str] = {
    "open_price": "not in this feed; only close and previous close are available",
    "overnight_gap": "needs the open price, which this feed does not provide",
    "candle_body_ratio": "needs the open price, which this feed does not provide",
    "atr": (
        "needs high/low; use close_to_close_volatility as an explicitly labelled "
        "substitute for stop sizing"
    ),
    "adx": "needs high/low; no substitute available from close alone",
    "stochastic": "needs high/low; RSI is the available momentum oscillator",
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
    missing = [c for c in REQUIRED_COLUMNS if c not in rows[0]]
    if missing:
        raise EngineError(f"rows are missing required columns: {', '.join(missing)}")

    frame = pd.DataFrame(list(rows))
    frame["date"] = frame["date"].map(_iso_date)
    frame = frame.drop_duplicates(subset="date", keep="last")
    frame = frame.sort_values("date").set_index("date")
    for column in REQUIRED_COLUMNS:
        if column != "date":
            frame[column] = pd.to_numeric(frame[column], errors="coerce").astype("float64")
    for column in ("open", "high", "low"):
        if column in frame.columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce").astype("float64")
    return frame


def _iso_date(value: Any) -> str:
    if isinstance(value, datetime):
        return (
            value.strftime("%Y-%m-%d %H:%M:%S")
            if (value.hour or value.minute or value.second)
            else value.strftime("%Y-%m-%d")
        )
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    s = str(value).strip()
    if " " in s or "T" in s:
        return s.replace("T", " ")[:19]
    return s[:10]


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


def _compute_frame(
    frame: pd.DataFrame,
    groups: list[str] | tuple[str, ...],
    params: dict | None = None,
    series_tail: int = 20,
    **kwargs,
) -> dict:
    """Internal: compute indicator groups from a pre-built frame.

    This is the shared logic for both :func:`compute` (daily rows) and
    :func:`multi_horizon_compute` (weekly aggregated frame).  Callers
    that already have a frame can skip the ``rows_to_frame`` validation
    overhead.
    """
    close = frame["close"]
    group_params = dict(params or {})
    if "dates" not in group_params:
        group_params["dates"] = pd.Series(frame.index, index=frame.index)

    for col in ("high", "low", "open", "prev_close"):
        if col in frame.columns and col not in group_params:
            group_params[col] = frame[col]

    results: dict[str, Any] = {}
    for group in groups:
        if group in _CLOSE_GROUPS:
            results[group] = _CLOSE_GROUPS[group](close, group_params)
        else:
            results[group] = _FRAME_GROUPS[group](frame, group_params)

    latest_close = finite(close.iloc[-1])
    latest_prev_close = finite(frame["prev_close"].iloc[-1])
    price_change_pct = None
    price_limit_flag = None
    if latest_close is not None and latest_prev_close is not None and latest_prev_close > 0:
        price_change_pct = round((latest_close - latest_prev_close) / latest_prev_close * 100, 2)
        abs_pct = abs(price_change_pct)
        if abs_pct >= 6.5:
            price_limit_flag = "near_ceiling" if price_change_pct > 0 else "near_floor"
    # Pre-compute trend alignment so the LLM doesn't have to reason about it
    trend_alignment = None
    if "trend" in results and latest_close is not None:
        trend = results["trend"]
        sma_values = {}
        for key in ("sma_20", "sma_50", "sma_200"):
            val = trend.get(key, {})
            if not is_insufficient(val):
                sma_values[key] = val.get("latest")
        if len(sma_values) >= 2:
            s20 = sma_values.get("sma_20")
            s50 = sma_values.get("sma_50")
            s200 = sma_values.get("sma_200")
            if s20 and s50 and s200:
                if latest_close > s20 > s50 > s200:
                    trend_alignment = "aligned_uptrend"
                elif latest_close < s20 < s50 < s200:
                    trend_alignment = "aligned_downtrend"
                elif latest_close > s200:
                    trend_alignment = "above_sma200_transitional"
                else:
                    trend_alignment = "below_sma200_transitional"
            elif s20 and s50:
                if latest_close > s20 > s50:
                    trend_alignment = "short_term_uptrend"
                elif latest_close < s20 < s50:
                    trend_alignment = "short_term_downtrend"
                else:
                    trend_alignment = "transitional"

    returns = None
    if "trend" in results and isinstance(results["trend"], dict):
        returns = results["trend"].get("returns_by_window")

    latest_session = None
    if len(frame) > 0:
        latest_row = frame.iloc[-1]
        bc = latest_row.get("buy_count")
        sc = latest_row.get("sell_count")
        tot_vol = finite(latest_row.get("total_volume"))
        tot_val = finite(latest_row.get("total_value"))
        buy_vol = finite(latest_row.get("buy_volume"))
        sell_vol = finite(latest_row.get("sell_volume"))
        fb_val = finite(latest_row.get("foreign_buy_value"))
        fs_val = finite(latest_row.get("foreign_sell_value"))
        latest_session = {
            "date": str(frame.index[-1]),
            "close": latest_close,
            "volume_shares": tot_vol,
            "volume_mil_shares": round(tot_vol / 1e6, 2) if tot_vol is not None else None,
            "value_vnd": tot_val,
            "value_bil_vnd": round(tot_val / 1e9, 2) if tot_val is not None else None,
            "buy_count": int(bc) if bc is not None and pd.notna(bc) else None,
            "sell_count": int(sc) if sc is not None and pd.notna(sc) else None,
            "buy_volume_shares": buy_vol,
            "buy_volume_mil": round(buy_vol / 1e6, 2) if buy_vol is not None else None,
            "sell_volume_shares": sell_vol,
            "sell_volume_mil": round(sell_vol / 1e6, 2) if sell_vol is not None else None,
            "foreign_buy_value_bil": round(fb_val / 1e9, 2) if fb_val is not None else None,
            "foreign_sell_value_bil": round(fs_val / 1e9, 2) if fs_val is not None else None,
            "foreign_net_value_bil": round((fb_val - fs_val) / 1e9, 2) if fb_val is not None and fs_val is not None else None,
        }

    recent_history = []
    if len(frame) > 0:
        sub_tail = frame.tail(min(5, len(frame)))
        for dt_idx, r in sub_tail.iterrows():
            c_val = finite(r.get("close"))
            pc_val = finite(r.get("prev_close"))
            # None, not 0.0, when the change cannot be computed: "unchanged" and
            # "unknown" are different facts and a reader cannot tell them apart
            # once both print as 0.00%.
            chg = (
                round((c_val - pc_val) / pc_val * 100, 2)
                if c_val is not None and pc_val is not None and pc_val > 0
                else None
            )
            tv_val = finite(r.get("total_volume"))
            fb_v = finite(r.get("foreign_buy_value"))
            fs_v = finite(r.get("foreign_sell_value"))
            fnet_b = round((fb_v - fs_v) / 1e9, 2) if fb_v is not None and fs_v is not None else None
            recent_history.append({
                "date": str(dt_idx),
                "close": c_val,
                "change_pct": chg,
                "volume_mil": round(tv_val / 1e6, 2) if tv_val is not None else None,
                "foreign_net_bil": fnet_b,
            })

    return with_vi_labels({
        "rows_used": int(len(frame)),
        "date_range": {"start": str(frame.index[0]), "end": str(frame.index[-1])},
        "latest_close": latest_close,
        "latest_prev_close": latest_prev_close,
        "price_change_pct": price_change_pct,
        "price_limit_flag": price_limit_flag,
        "trend_alignment": trend_alignment,
        "returns": returns,
        "latest_session": latest_session,
        "recent_history": recent_history,
        # Group enums (RSI zone, volume flag, MACD crossover, flow bias,
        # Bollinger position, streak direction) get their Vietnamese phrase
        # here, in one recursive pass, instead of at ~40 call sites.
        "groups": serialize(results, series_tail=series_tail),
        "data_quality": data_quality(frame),
    })


_compute_core = _compute_frame


def get_unsupported_metrics(has_high_low: bool = False, has_open: bool = False) -> dict[str, str]:
    """Return a copy of UNSUPPORTED_METRICS filtered for available data."""
    metrics = dict(UNSUPPORTED_METRICS)
    if has_high_low:
        metrics.pop("atr", None)
        metrics.pop("stochastic", None)
    if has_open:
        metrics.pop("open_price", None)
        metrics.pop("overnight_gap", None)
        metrics.pop("candle_body_ratio", None)
    return metrics


def compute(
    rows: list[dict],
    groups: list[str] | tuple[str, ...] | None = None,
    params: dict | None = None,
    *,
    series_tail: int = 20,
) -> dict:
    """Compute the requested indicator groups from `rows`.

    `groups` is a subset of :data:`GROUPS`; None means all of them. Series are
    trimmed to their last `series_tail` points on the way out, since the full
    history is already available through `get_price_data`.
    """
    requested = tuple(groups) if groups else GROUPS
    unknown = [g for g in requested if g not in GROUPS]
    if unknown:
        raise EngineError(
            f"unknown group(s): {', '.join(unknown)}; valid groups are {', '.join(GROUPS)}"
        )

    frame = rows_to_frame(rows)
    has_hl = (
        "high" in frame.columns
        and "low" in frame.columns
        and bool(frame["high"].notna().any())
        and bool(frame["low"].notna().any())
    )
    has_open = "open" in frame.columns and bool(frame["open"].notna().any())

    result = _compute_frame(frame, requested, params, series_tail=series_tail)
    if has_hl or has_open:
        result["unsupported_metrics"] = get_unsupported_metrics(has_high_low=has_hl, has_open=has_open)
    return result


#: Keys duplicated across the multi-horizon payload. Each is available in full
#: elsewhere in the same response, so repeating it inside every horizon triples
#: the cost of the largest fields for no extra information.
_HORIZON_REDUNDANT_KEYS: tuple[str, ...] = (
    "macd",            # daily.groups.trend.macd
    "bollinger",       # daily.groups.volatility.bollinger
    "weekly_macd",     # weekly.groups.trend.macd
    "weekly_bollinger",
    "key_levels",      # levels.supports / levels.resistances
    "weekly_confirmation",
    "foreign_overall",
    "foreign_20d",     # groups.foreign_flow.foreign_net_value.windows
    "foreign_60d",
    "foreign_120d",
)

#: Weekly groups worth computing for the long-term view. The flow groups are
#: summed over a week, which makes them hard to interpret and nothing consumes
#: them; computing all seven roughly doubled the weekly payload.
_COMPACT_WEEKLY_GROUPS: tuple[str, ...] = ("trend", "momentum", "volatility")

#: Daily groups every scope needs: the trend/momentum/volatility trio feeds the
#: MA tables, the oscillators and the Bollinger levels.
_CORE_DAILY_GROUPS: tuple[str, ...] = ("trend", "momentum", "volatility")

#: Query scopes for :func:`multi_horizon_compute`.
#:
#: A single analysis request does not need every section. Answering "phân tích
#: ngắn hạn VNM" with the full payload spends about 73% of its tokens on weekly
#: bars, 52-week statistics and two unused horizons — and pays to compute them.
#: Each scope names exactly what it needs so the work and the payload both shrink.
#:
SCOPES: dict[str, dict[str, Any]] = {
    "full": {
        "description_vi": "Phân tích toàn diện: cả ba khung, mốc kỹ thuật, thống kê 52 tuần",
        "horizons": ("short_term", "mid_term", "long_term"),
        "daily_groups": GROUPS,
        "weekly": True,
        "stats_52w": True,
        "strategies": True,
        "levels": True,
    },
    "short_term": {
        "description_vi": "Chỉ khung ngắn hạn (1–4 tuần)",
        "horizons": ("short_term",),
        "daily_groups": _CORE_DAILY_GROUPS + ("volume_flow", "trade_flow", "foreign_flow"),
        "weekly": False,
        "stats_52w": False,
        "strategies": True,
        "levels": True,
    },
    "mid_term": {
        "description_vi": "Chỉ khung trung hạn (1–3 tháng), có xác nhận khung tuần",
        "horizons": ("mid_term",),
        "daily_groups": _CORE_DAILY_GROUPS + ("volume_flow", "trade_flow", "foreign_flow"),
        "weekly": True,  # the mid-term view uses weekly SMA20 as confirmation
        "stats_52w": False,
        "strategies": True,
        "levels": True,
    },
    "long_term": {
        "description_vi": "Chỉ khung dài hạn (> 3 tháng), chủ yếu khung tuần",
        "horizons": ("long_term",),
        "daily_groups": _CORE_DAILY_GROUPS + ("foreign_flow",),
        "weekly": True,
        "stats_52w": True,
        "strategies": True,
        "levels": True,
    },
    "levels": {
        "description_vi": "Chỉ các mốc hỗ trợ / kháng cự và vị thế giá hiện tại",
        "horizons": (),
        "daily_groups": _CORE_DAILY_GROUPS,
        "weekly": False,
        "stats_52w": False,
        "strategies": False,
        "levels": True,
    },
}

#: What each scope deliberately does not compute, phrased for the report so a
#: consumer never mistakes "not requested" for "not enough data".
_SCOPE_OMISSION_NOTE = (
    "Các mục dưới đây KHÔNG được tính trong scope '{scope}' vì không cần cho câu hỏi "
    "này — đây KHÔNG phải là thiếu dữ liệu: {omitted}. Nếu cần, gọi lại với scope='full'."
)


def compact_multi_horizon(result: dict) -> dict:
    """Drop fields that repeat data present elsewhere in the same payload.

    The full payload runs to roughly 19k tokens for 500 sessions, most of it
    duplication. A locally hosted model has to hold the skill prompt, this
    payload and its own report inside one context window, and the duplicated
    copies are what push that over. Every key removed here is still available
    at its canonical path.
    """
    horizons = result.get("horizons")
    if isinstance(horizons, dict):
        for name in ("short_term", "mid_term", "long_term"):
            horizon = horizons.get(name)
            if isinstance(horizon, dict):
                for key in _HORIZON_REDUNDANT_KEYS:
                    horizon.pop(key, None)

    strategies = result.get("strategies")
    if isinstance(strategies, dict):
        for name in ("short_term", "mid_term", "long_term"):
            entry = strategies.get(name)
            if isinstance(entry, dict):
                # Duplicates `levels`, and the per-horizon nearest levels are
                # already in support_zone / resistance_zone.
                entry.pop("levels_to_watch", None)
                entry.pop("conflicts", None)

    levels = result.get("levels")
    if isinstance(levels, dict):
        levels.pop("swings", None)
        levels.pop("nearest_support", None)
        levels.pop("nearest_resistance", None)

    return result


def multi_horizon_compute(
    rows: list[dict],
    *,
    series_tail: int = 5,
    weekly_series_tail: int = 5,
    include_series: bool = False,
    detail: str = "compact",
    scope: str = "full",
) -> dict:
    """Multi-horizon analysis, scoped to what the question needs.

    This is the entry point the ``analyze_multi_horizon`` MCP tool calls. It
    orchestrates, for the sections the chosen ``scope`` asks for:

    1. Daily indicator computation
    2. Weekly bar aggregation and weekly indicators
    3. Support / resistance levels
    4. Horizon analysis (short / mid / long term)
    5. Technical perspectives per horizon
    6. 52-week statistics

    Parameters
    ----------
    rows
        Daily row dicts, same format as :func:`compute`.
    series_tail, weekly_series_tail
        How many data points of each series to include.
    include_series
        Whether to keep raw float series arrays. False (default) prunes them.
    detail
        ``"compact"`` (default) removes fields that duplicate data present
        elsewhere in the response. ``"full"`` keeps every field.
    scope
        One of :data:`SCOPES`. ``"full"`` (default) computes everything;
        ``"short_term"``, ``"mid_term"`` and ``"long_term"`` compute one horizon
        and only the inputs it uses; ``"levels"`` returns support/resistance
        alone. Sections a scope skips are named in ``sections_omitted`` so a
        consumer can tell "not requested" from "not enough data".

    Returns
    -------
    dict
        ``daily`` and ``levels`` always; ``weekly``, ``horizons``,
        ``strategies`` and ``stats_52w`` when the scope includes
        them, plus ``scope``, ``scope_description`` and ``sections_omitted``.
    """
    plan = SCOPES.get(scope)
    if plan is None:
        raise EngineError(
            f"unknown scope {scope!r}; valid scopes are {', '.join(SCOPES)}"
        )

    # 1. Daily frame and daily indicators, restricted to the groups this scope reads.
    frame = rows_to_frame(rows)
    daily_groups = plan["daily_groups"] if detail != "full" else GROUPS
    daily_result = _compute_core(frame, daily_groups, series_tail=series_tail)
    daily_result["timeframe"] = "daily"
    daily_result["groups_computed"] = list(daily_groups)

    # 2. Weekly bars, only when a requested horizon actually uses them.
    _MIN_WEEKLY_BARS = 14
    weekly_frame = None
    weekly_result = None
    if plan["weekly"]:
        weekly_frame = aggregate_weekly(frame)
        weekly_groups = GROUPS if detail == "full" else _COMPACT_WEEKLY_GROUPS
        if len(weekly_frame) >= _MIN_WEEKLY_BARS:
            # Drop rows with NaN prev_close (first week) for clean computation
            wf_clean = weekly_frame.dropna(subset=["prev_close"])
            if len(wf_clean) >= _MIN_WEEKLY_BARS:
                weekly_result = _compute_core(
                    wf_clean, weekly_groups, series_tail=weekly_series_tail,
                )
                weekly_result["timeframe"] = "weekly"
                flags = weekly_quality_flags(weekly_frame)
                if flags:
                    weekly_result["quality_flags"] = flags

    # 3. Support / resistance levels. Computed before the horizons because each
    #    horizon attaches a real invalidation level taken from here rather than
    #    inventing one.
    levels = key_levels(
        frame,
        daily_result.get("groups", {}).get("trend"),
        daily_result.get("groups", {}).get("volatility"),
    )

    latest_close = daily_result.get("latest_close")

    result: dict[str, Any] = {
        "analysis_type": "multi_horizon",
        "scope": scope,
        "scope_description": plan["description_vi"],
        "detail": detail,
        "daily": daily_result,
        "levels": levels,
    }

    # 4. Horizons
    horizons = None
    if plan["horizons"]:
        horizons = horizon_analysis(
            daily_result, weekly_result, latest_close, levels,
            which=plan["horizons"],
        )
        result["horizons"] = horizons

    if plan["weekly"]:
        result["weekly"] = weekly_result
        result["weekly_bars_available"] = int(len(weekly_frame))
        result["weekly_bars_min_required"] = _MIN_WEEKLY_BARS

    # 5. Technical perspectives for the horizons that were computed
    if plan.get("strategies") and horizons:
        result["strategies"] = suggest_strategies(
            horizons, latest_close, daily_result, levels,
            which=plan["horizons"],
        )

    # 6. 52-week statistics
    if plan.get("stats_52w"):
        result["stats_52w"] = stats_52w(frame, window=min(250, len(frame)))

    omitted = _omitted_sections(plan, daily_groups)
    if omitted:
        result["sections_omitted"] = omitted
        result["sections_omitted_note"] = _SCOPE_OMISSION_NOTE.format(
            scope=scope, omitted=", ".join(omitted)
        )

    if not include_series:
        result = prune_series(result)
    if detail != "full":
        result = compact_multi_horizon(result)
    return result


def _omitted_sections(plan: dict, daily_groups) -> list[str]:
    """Name what this scope did not compute, for the report to pass along."""
    omitted: list[str] = []
    for name in ("short_term", "mid_term", "long_term"):
        if name not in plan["horizons"]:
            omitted.append(f"horizons.{name}")
    if not plan.get("weekly"):
        omitted.append("weekly")
    if not plan.get("stats_52w"):
        omitted.append("stats_52w")
    if not plan.get("strategies"):
        omitted.append("strategies")
    for group in GROUPS:
        if group not in daily_groups:
            omitted.append(f"daily.groups.{group}")
    return omitted


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
            parsed.append(datetime.strptime(value[:10], "%Y-%m-%d").date())
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


def serialize(obj: Any, series_tail: int = 20) -> Any:
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


def unsupported(metric: str, has_high_low: bool = False) -> dict:
    """Structured refusal for a metric this feed cannot support."""
    key = metric.strip().lower().replace(" ", "_")
    metrics = get_unsupported_metrics(has_high_low=has_high_low)
    reason = metrics.get(key)
    if reason is None:
        if has_high_low and key in ("atr", "stochastic"):
            return {"supported": True, "metric": key, "message": f"{key} is supported when OHLC data is available"}
        return insufficient(f"unknown metric {metric!r}", 0)
    return {"unsupported": True, "metric": key, "reason": reason}


def prune_series(obj: Any) -> Any:
    """Prune verbose historical series arrays from analysis dicts to keep LLM context light.

    Removes dict keys ending in '_series', 'series', and 'normalized_series_100'.
    Scalar summaries, levels, horizons and strategies are preserved.
    """
    if isinstance(obj, dict):
        return {
            k: prune_series(v)
            for k, v in obj.items()
            if not k.endswith("_series")
            and k != "series"
            and k != "normalized_series_100"
        }
    if isinstance(obj, list):
        return [prune_series(x) for x in obj]
    return obj
