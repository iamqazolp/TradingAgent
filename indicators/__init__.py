"""Indicator engine.

Every function in this package is pure: pandas in, plain dict (possibly holding
pandas Series) out. No network, no database, no file access.

Failure contract
----------------
When the required history is not there, a function returns

    {"insufficient_data": True, "reason": "...", "required_window": n, "available": m}

It never returns a silent NaN, never fabricates a number from a shorter window,
and never raises for the ordinary "not enough history" case. Callers test with
:func:`is_insufficient`.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

INSUFFICIENT_KEY = "insufficient_data"


def insufficient(reason: str, required_window: int, available: int | None = None) -> dict:
    """Build the canonical insufficient-data marker."""
    marker: dict[str, Any] = {
        INSUFFICIENT_KEY: True,
        "reason": reason,
        "required_window": int(required_window),
    }
    if available is not None:
        marker["available"] = int(available)
    return marker


def is_insufficient(result: Any) -> bool:
    """True if `result` is an insufficient-data marker."""
    return isinstance(result, dict) and bool(result.get(INSUFFICIENT_KEY))


def finite(value: Any) -> float | None:
    """Coerce to float, mapping NaN / inf / None / pandas-NA to None."""
    if value is None:
        return None
    try:
        if value is pd.NA or (not isinstance(value, (int, float, np.integer, np.floating)) and pd.isna(value)):
            return None
    except (TypeError, ValueError):
        pass
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def safe_div(numerator: Any, denominator: Any) -> float | None:
    """Divide, returning None when the denominator is zero or either side is missing.

    Zero denominators are real in this feed: a halted day has no matched volume
    and a small cap can have zero trades on one side. Those are gaps, not zeros.
    """
    num = finite(numerator)
    den = finite(denominator)
    if num is None or den is None or den == 0:
        return None
    return num / den


def safe_series_div(numerator: pd.Series, denominator: pd.Series) -> pd.Series:
    """Elementwise divide with zero denominators mapped to NaN (no warnings)."""
    den = pd.to_numeric(denominator, errors="coerce").astype("float64")
    num = pd.to_numeric(numerator, errors="coerce").astype("float64")
    return num / den.where(den != 0, other=np.nan)


def latest(series: pd.Series) -> float | None:
    """Last finite-or-None value of a series."""
    if series is None or len(series) == 0:
        return None
    return finite(series.iloc[-1])


def prior_average(series: pd.Series, window: int) -> pd.Series:
    """Mean of the `window` values *before* each row (today excluded).

    Every "today vs its own recent normal" comparison in this engine uses this
    exclusive convention: including today in its own baseline would damp exactly
    the spike we are trying to detect. It costs one extra row of history.
    """
    numeric = pd.to_numeric(series, errors="coerce").astype("float64")
    return numeric.shift(1).rolling(window, min_periods=window).mean()


def ratio_to_prior_average(series: pd.Series, window: int) -> pd.Series:
    """Each row divided by the average of the `window` rows before it."""
    numeric = pd.to_numeric(series, errors="coerce").astype("float64")
    return safe_series_div(numeric, prior_average(numeric, window))


def require(series: pd.Series, window: int, what: str) -> dict | None:
    """Return an insufficient-data marker if `series` is shorter than `window`."""
    available = 0 if series is None else int(len(series))
    if available < window:
        return insufficient(
            f"{what} requires {window} rows of history, {available} available",
            window,
            available,
        )
    return None
