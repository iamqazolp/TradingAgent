"""Group G: foreign flow.

Net foreign volume, net foreign value, participation ratio, and foreign room
trend. Value is generally the more reliable of the first two because it is
price-weighted.

`CurrentForeignRoom` is a *snapshot*, not a flow. Room falls when foreigners buy
and rises when they sell, so the expected day-over-day change is roughly minus
the day's foreign net volume. It can also move for reasons unrelated to trading:
a change in the foreign ownership limit, or a change in charter capital. Room
changes that cannot be explained by the day's foreign net volume are reported as
suspected structural changes rather than attributed to trading.
"""

from __future__ import annotations

import pandas as pd

from indicators import finite, latest, require, safe_series_div

#: A room change is "unexplained" when the unexplained part exceeds this
#: fraction of the day's total volume (and is not a rounding-scale residual).
ROOM_RESIDUAL_TOLERANCE = 0.05


def foreign_net_volume(df: pd.DataFrame) -> dict:
    """Foreign buy volume minus sell volume, daily and cumulative."""
    marker = require(df.index.to_series(), 1, "foreign_net_volume")
    if marker:
        return marker
    buy = pd.to_numeric(df["foreign_buy_volume"], errors="coerce").astype("float64")
    sell = pd.to_numeric(df["foreign_sell_volume"], errors="coerce").astype("float64")
    daily = (buy - sell).rename("foreign_net_volume")
    cumulative = daily.cumsum().rename("foreign_net_volume_cum")
    lat_vol = latest(daily)
    cum_vol = latest(cumulative)
    return {
        "latest": lat_vol,
        "cumulative": cum_vol,
        "cumulative_mil_shares": round(cum_vol / 1e6, 2) if cum_vol is not None else None,
        "days_with_zero_foreign_activity": int(((buy + sell) == 0).sum()),
        "series": daily,
        "cumulative_series": cumulative,
    }


def foreign_net_value(df: pd.DataFrame) -> dict:
    """Foreign buy value minus sell value in VND, daily and cumulative.

    Preferred over the volume figure: it is price-weighted, so it does not treat
    a lot bought at a high price as equivalent to one bought cheaply.
    """
    marker = require(df.index.to_series(), 1, "foreign_net_value")
    if marker:
        return marker
    buy = pd.to_numeric(df["foreign_buy_value"], errors="coerce").astype("float64")
    sell = pd.to_numeric(df["foreign_sell_value"], errors="coerce").astype("float64")
    daily = (buy - sell).rename("foreign_net_value")
    cumulative = daily.cumsum().rename("foreign_net_value_cum")
    lat_val = latest(daily)
    cum_val = latest(cumulative)

    # Calculate multi-window cumulative stats (20, 60, 120 sessions)
    windows_stats = {}
    for w in (20, 60, 120):
        sub = daily.tail(w)
        if len(sub) > 0:
            sub_sum = float(sub.sum())
            buy_cnt = int((sub > 0).sum())
            sell_cnt = int((sub < 0).sum())
            net_bil = round(sub_sum / 1e9, 2)
            windows_stats[f"{w}d"] = {
                "window": len(sub),
                "net_value_bil": net_bil,
                "net_value_vnd": sub_sum,
                "buying_days": buy_cnt,
                "selling_days": sell_cnt,
                "summary": f"{buy_cnt}/{len(sub)} phiên mua ròng (lũy kế {net_bil:+.2f} tỷ VND)",
            }

    return {
        "latest": lat_val,
        "latest_vnd": lat_val,
        "latest_bil_vnd": round(lat_val / 1e9, 2) if lat_val is not None else None,
        "cumulative": cum_val,
        "cumulative_vnd": cum_val,
        "cumulative_bil_vnd": round(cum_val / 1e9, 2) if cum_val is not None else None,
        "stance": _stance(cum_val),
        "windows": windows_stats,
        "series": daily,
        "cumulative_series": cumulative,
    }


def foreign_participation_ratio(df: pd.DataFrame, window: int = 5) -> dict:
    """(foreign buy + foreign sell) volume / total volume, daily and `window`-day mean.

    Zero is a legitimate and common reading on small caps, not an ingestion bug.
    Days with no total volume become gaps.
    """
    marker = require(df.index.to_series(), window, f"foreign_participation_ratio({window})")
    if marker:
        return marker
    buy = pd.to_numeric(df["foreign_buy_volume"], errors="coerce").astype("float64")
    sell = pd.to_numeric(df["foreign_sell_volume"], errors="coerce").astype("float64")
    daily = safe_series_div(buy + sell, df["total_volume"]).rename(
        "foreign_participation_ratio"
    )
    rolling = daily.rolling(window, min_periods=window).mean().rename(
        f"foreign_participation_ratio_avg{window}"
    )
    return {
        "window": window,
        "latest": latest(daily),
        "latest_rolling_avg": latest(rolling),
        "series": daily,
        "rolling_series": rolling,
    }


def foreign_room_trend(
    df: pd.DataFrame, window: int = 5, tolerance: float = ROOM_RESIDUAL_TOLERANCE
) -> dict:
    """Rolling sum of day-over-day change in `foreign_room` over `window` days.

    Sustained negative: foreign accumulation. Sustained positive: divestment.
    Requires `window + 1` rows because the first row has no prior room value.
    """
    required = window + 1
    marker = require(df.index.to_series(), required, f"foreign_room_trend({window})")
    if marker:
        return marker
    room = pd.to_numeric(df["foreign_room"], errors="coerce").astype("float64")
    change = room.diff().rename("foreign_room_change")
    trend = change.rolling(window, min_periods=window).sum().rename(
        f"foreign_room_trend{window}"
    )
    net_volume = pd.to_numeric(df["foreign_buy_volume"], errors="coerce").astype(
        "float64"
    ) - pd.to_numeric(df["foreign_sell_volume"], errors="coerce").astype("float64")
    value = latest(trend)
    return {
        "window": window,
        "latest": value,
        "latest_room": latest(room),
        "latest_room_change": latest(change),
        "reading": _room_reading(value),
        "suspected_structural_changes": _unexplained_room_changes(
            change, net_volume, df["total_volume"], tolerance
        )[:10],
        "series": change,
        "trend_series": trend,
    }


def _unexplained_room_changes(
    change: pd.Series, net_volume: pd.Series, total_volume: pd.Series, tolerance: float
) -> list[dict]:
    """Room moves that the day's foreign net volume cannot account for."""
    out: list[dict] = []
    for stamp, raw_change in change.items():
        delta = finite(raw_change)
        if delta is None:
            continue
        net = finite(net_volume.loc[stamp]) or 0.0
        volume = finite(total_volume.loc[stamp]) or 0.0
        # Room should fall by roughly the foreign net volume bought.
        residual = delta + net
        threshold = max(tolerance * volume, 1000.0)
        if abs(residual) > threshold:
            out.append(
                {
                    "date": str(stamp),
                    "room_change": delta,
                    "foreign_net_volume": net,
                    "unexplained": residual,
                    "note": (
                        "room change not explained by that day's foreign trading; "
                        "check for a foreign ownership limit or charter capital change"
                    ),
                }
            )
    return out


def _stance(cumulative: float | None) -> str:
    if cumulative is None:
        return "unknown"
    if cumulative > 0:
        return "net_buying"
    if cumulative < 0:
        return "net_selling"
    return "flat"


def _room_reading(trend: float | None) -> str:
    if trend is None:
        return "unknown"
    if trend < 0:
        return "room_shrinking_foreign_accumulation"
    if trend > 0:
        return "room_growing_foreign_divestment"
    return "room_unchanged"


def foreign_flow_group(df: pd.DataFrame, params: dict | None = None) -> dict:
    """Every Group G indicator, keyed by name."""
    params = params or {}
    window = params.get("flow_window", 5)
    return {
        "foreign_net_volume": foreign_net_volume(df),
        "foreign_net_value": foreign_net_value(df),
        f"foreign_participation_ratio_{window}": foreign_participation_ratio(df, window),
        f"foreign_room_trend_{window}": foreign_room_trend(df, window),
    }
