"""Group E: order flow by trade count.

Volume imbalance cannot tell a few large orders from many small ones. Trade
counts and average trade size by side can, which is the whole point of this
group.
"""

from __future__ import annotations

import pandas as pd

from indicators import latest, prior_average, ratio_to_prior_average, require, safe_series_div


def buy_sell_count_imbalance(df: pd.DataFrame, window: int = 5) -> dict:
    """(buy_count - sell_count) / (buy_count + sell_count), daily and `window`-day mean."""
    marker = require(df.index.to_series(), window, f"buy_sell_count_imbalance({window})")
    if marker:
        return marker
    buy = pd.to_numeric(df["buy_count"], errors="coerce").astype("float64")
    sell = pd.to_numeric(df["sell_count"], errors="coerce").astype("float64")
    daily = safe_series_div(buy - sell, buy + sell).rename("buy_sell_count_imbalance")
    rolling = daily.rolling(window, min_periods=window).mean().rename(
        f"buy_sell_count_imbalance_avg{window}"
    )
    return {
        "window": window,
        "latest": latest(daily),
        "latest_rolling_avg": latest(rolling),
        "series": daily,
        "rolling_series": rolling,
    }


def avg_trade_size_by_side(df: pd.DataFrame, window: int = 20) -> dict:
    """Average matched volume per trade on each side, vs each side's own baseline.

    `buy_volume / buy_count` against the average of the prior `window` days of
    the same quantity, and likewise for the sell side. A buy-side ratio well
    above 1 with a flat sell side means larger buy tickets than usual, which
    reads as accumulation by bigger participants rather than broad retail
    interest.
    """
    required = window + 1  # the baseline excludes today
    marker = require(df.index.to_series(), required, f"avg_trade_size_by_side({window})")
    if marker:
        return marker

    buy_size = safe_series_div(df["buy_volume"], df["buy_count"]).rename("avg_buy_trade_size")
    sell_size = safe_series_div(df["sell_volume"], df["sell_count"]).rename("avg_sell_trade_size")
    buy_ratio = ratio_to_prior_average(buy_size, window).rename("avg_buy_trade_size_ratio")
    sell_ratio = ratio_to_prior_average(sell_size, window).rename("avg_sell_trade_size_ratio")

    lat_buy = latest(buy_size)
    lat_sell = latest(sell_size)
    return {
        "window": window,
        "latest": {
            "avg_buy_trade_size": lat_buy,
            "avg_sell_trade_size": lat_sell,
            "avg_buy_trade_size_lots": round(lat_buy / 100, 1) if lat_buy is not None else None,
            "avg_sell_trade_size_lots": round(lat_sell / 100, 1) if lat_sell is not None else None,
            "avg_buy_trade_size_baseline": latest(prior_average(buy_size, window)),
            "avg_sell_trade_size_baseline": latest(prior_average(sell_size, window)),
            "buy_ratio_to_baseline": latest(buy_ratio),
            "sell_ratio_to_baseline": latest(sell_ratio),
            "buy_vs_sell_size": latest(safe_series_div(buy_size, sell_size)),
        },
        "interpretation": _size_reading(latest(buy_ratio), latest(sell_ratio)),
        "buy_size_series": buy_size,
        "sell_size_series": sell_size,
        "buy_ratio_series": buy_ratio,
        "sell_ratio_series": sell_ratio,
    }


def _size_reading(buy_ratio: float | None, sell_ratio: float | None) -> str:
    """Plain-language reading of the two ratios. 'unknown' when either is missing."""
    if buy_ratio is None or sell_ratio is None:
        return "unknown"
    big_buy = buy_ratio >= 1.3
    big_sell = sell_ratio >= 1.3
    if big_buy and not big_sell:
        return "larger_buy_tickets"
    if big_sell and not big_buy:
        return "larger_sell_tickets"
    if big_buy and big_sell:
        return "larger_tickets_both_sides"
    return "ordinary_ticket_sizes"


def trade_flow_group(df: pd.DataFrame, params: dict | None = None) -> dict:
    """Every Group E indicator, keyed by name."""
    params = params or {}
    flow_window = params.get("flow_window", 5)
    size_window = params.get("trade_size_window", 20)
    return {
        f"buy_sell_count_imbalance_{flow_window}": buy_sell_count_imbalance(df, flow_window),
        f"avg_trade_size_by_side_{size_window}": avg_trade_size_by_side(df, size_window),
    }
