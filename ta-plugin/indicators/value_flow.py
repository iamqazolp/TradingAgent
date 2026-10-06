"""Group F: value flow. Average trade value and value spikes, in VND.

Value-weighted flow is more informative than raw volume on days with an unusual
price move, since the same number of shares can represent very different money.
"""

from __future__ import annotations

import pandas as pd

from indicators import latest, prior_average, ratio_to_prior_average, require, safe_series_div


def avg_trade_value(df: pd.DataFrame, window: int = 20) -> dict:
    """`total_value / total_trade` vs the average of the prior `window` days.

    Ticket size in VND. An unusually large average ticket points at institutional
    participation; an unusually small one at retail churn.
    """
    required = window + 1
    marker = require(df.index.to_series(), required, f"avg_trade_value({window})")
    if marker:
        return marker
    per_trade = safe_series_div(df["total_value"], df["total_trade"]).rename("avg_trade_value")
    ratio = ratio_to_prior_average(per_trade, window).rename("avg_trade_value_ratio")
    value = latest(ratio)
    return {
        "window": window,
        "latest": latest(per_trade),
        "latest_vnd": latest(per_trade),
        "baseline_vnd": latest(prior_average(per_trade, window)),
        "ratio_to_baseline": value,
        "flag": _ticket_flag(value),
        "series": per_trade,
        "ratio_series": ratio,
    }


def value_spike(df: pd.DataFrame, window: int = 20) -> dict:
    """Today's `total_value` vs the average of the prior `window` days."""
    required = window + 1
    marker = require(df.index.to_series(), required, f"value_spike({window})")
    if marker:
        return marker
    total_value = pd.to_numeric(df["total_value"], errors="coerce").astype("float64")
    ratio = ratio_to_prior_average(total_value, window).rename("value_spike_ratio")
    value = latest(ratio)
    return {
        "window": window,
        "latest": value,
        "latest_value_vnd": latest(total_value),
        "baseline_vnd": latest(prior_average(total_value, window)),
        "ratio_to_baseline": value,
        "flag": _spike_flag(value),
        "series": ratio,
    }


def _ticket_flag(ratio: float | None) -> str:
    if ratio is None:
        return "unknown"
    if ratio >= 1.5:
        return "unusually_large_tickets"
    if ratio <= 0.66:
        return "unusually_small_tickets"
    return "normal"


def _spike_flag(ratio: float | None) -> str:
    if ratio is None:
        return "unknown"
    if ratio >= 2.0:
        return "value_spike"
    if ratio >= 1.5:
        return "elevated_value"
    if ratio <= 0.5:
        return "value_drought"
    return "normal"


def value_flow_group(df: pd.DataFrame, params: dict | None = None) -> dict:
    """Every Group F indicator, keyed by name."""
    params = params or {}
    window = params.get("value_window", 20)
    return {
        f"avg_trade_value_{window}": avg_trade_value(df, window),
        f"value_spike_{window}": value_spike(df, window),
    }
