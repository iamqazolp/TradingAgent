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
    """True if `result` is an insufficient-data marker.

    Also catches the bare string ``"insufficient_data"``, which older group code
    emitted in place of the marker dict. Without this, a consumer testing a
    string marker would read it as a usable value.
    """
    if isinstance(result, str):
        return result == INSUFFICIENT_KEY
    return isinstance(result, dict) and bool(result.get(INSUFFICIENT_KEY))


def pick(group: dict | None, base: str) -> Any:
    """Fetch ``base`` from a group dict, tolerating a window suffix.

    Group modules key several indicators by their configured window —
    ``rsi_14``, ``volume_ratio_20``, ``buy_sell_volume_imbalance_5`` — so the
    key changes when a caller overrides ``params``. Consumers that hardcoded the
    unsuffixed name silently read ``None`` forever: that is how RSI came to be
    absent from every horizon and every comparison row.

    Looks for the exact key first, then for exactly one ``base_<digits>`` match.
    Returns ``None`` when the name is genuinely absent, so callers can tell
    "not computed" from an insufficient-data marker.
    """
    if not isinstance(group, dict):
        return None
    if base in group:
        return group[base]
    prefix = f"{base}_"
    matches = [
        value
        for key, value in group.items()
        if key.startswith(prefix) and key[len(prefix):].isdigit()
    ]
    if len(matches) == 1:
        return matches[0]
    return None


def pick_scalar(group: dict | None, base: str, field: str = "latest") -> Any:
    """``pick`` then read one scalar field, returning None for any marker."""
    entry = pick(group, base)
    if entry is None or is_insufficient(entry) or not isinstance(entry, dict):
        return None
    return entry.get(field)


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


# --------------------------------------------------------------------------- Vietnamese labels


#: Machine enums the payload exposes, mapped to the Vietnamese phrasing a report
#: should use. Kept beside the values rather than inside each group module
#: because the same enum appears in several groups (``signal_strength`` in every
#: horizon, ``zone`` in RSI and close percentile, ``bias`` in two flow groups).
#:
#: The deployed model is a ~31B local model rendering this payload into prose.
#: Left to translate them itself it invents a different Vietnamese phrase on
#: every call — "tiêu cực mạnh", "nghiêng tiêu cực rõ rệt", "mạnh tiêu cực" for
#: the same ``strong_bearish`` — which is exactly the formulaic drift this
#: table removes. One canonical phrase per enum.
VI_LABELS: dict[str, str] = {
    # signal_strength / trend classification
    "strong_bullish": "tích cực rõ rệt",
    "moderate_bullish": "tích cực vừa phải",
    "lean_bullish": "hơi nghiêng tích cực",
    "weak_bullish": "tích cực yếu",
    "bullish": "tích cực",
    "strong_bearish": "tiêu cực rõ rệt",
    "moderate_bearish": "tiêu cực vừa phải",
    "lean_bearish": "hơi nghiêng tiêu cực",
    "weak_bearish": "tiêu cực yếu",
    "bearish": "tiêu cực",
    "neutral": "trung tính",
    "transitional": "chuyển tiếp",
    # trend_alignment
    "aligned_uptrend": "xu hướng tăng đồng thuận (giá trên SMA20 trên SMA50 trên SMA200)",
    "aligned_downtrend": "xu hướng giảm đồng thuận (giá dưới SMA20 dưới SMA50 dưới SMA200)",
    "above_sma200_transitional": "đã vượt lên trên đường trung bình 200 phiên",
    "below_sma200_transitional": "vẫn nằm dưới đường trung bình 200 phiên",
    "short_term_uptrend": "xu hướng tăng ngắn hạn",
    "short_term_downtrend": "xu hướng giảm ngắn hạn",
    # market breadth regime
    "strongly_bullish": "tích cực mạnh",
    "strongly_bearish": "tiêu cực mạnh",
    # direction / bias
    "rising": "đang đi lên",
    "falling": "đang đi xuống",
    "flat": "đi ngang",
    "buy_side": "bên mua chiếm ưu thế",
    "sell_side": "bên bán chiếm ưu thế",
    "balanced": "cân bằng",
    # momentum zone / streak
    "overbought": "vùng quá mua",
    "oversold": "vùng quá bán",
    "near_high": "sát đỉnh biên độ gần nhất",
    "near_low": "sát đáy biên độ gần nhất",
    "middle": "ở giữa biên độ",
    "up": "tăng",
    "down": "giảm",
    # volume ratio flag
    "very_high": "rất cao",
    "elevated": "cao hơn bình thường",
    "very_low": "rất thấp",
    "low": "thấp hơn bình thường",
    "normal": "bình thường",
    # foreign flow stance
    "net_buying": "mua ròng",
    "net_selling": "bán ròng",
    # bollinger position
    "above_upper": "nằm trên dải trên",
    "below_lower": "nằm dưới dải dưới",
    "inside": "nằm trong dải",
    # crossover
    "bullish_cross": "cắt tăng",
    "bearish_cross": "cắt giảm",
    "none": "không có tín hiệu giao cắt",
    # return streak flag / horizontal alignment
    "extended": "kéo dài đáng chú ý",
    "notable": "đáng chú ý",
    # volume imbalance bias, trend alignment aliases used by comparisons
    "strong_uptrend": "tăng mạnh",
    "strong_downtrend": "giảm mạnh",
}


def vi_label(value: Any) -> str | None:
    """Vietnamese phrasing for a machine enum, or None when there is no mapping.

    Returns None rather than the input so a caller can tell "this enum has no
    agreed Vietnamese wording" from "this enum translates to itself".
    """
    if not isinstance(value, str):
        return None
    return VI_LABELS.get(value)


#: Boolean fields whose True/False reads as a technical state rather than a
#: switch. ``squeeze = True`` printed into a report is code leaking into prose,
#: so it gets a phrase of its own. Each entry is an explicit (true, false)
#: pair: Vietnamese negation is not mechanical, so it is written out.
BOOL_LABELS: dict[str, tuple[str, str]] = {
    "squeeze": ("dải Bollinger đang co hẹp", "dải Bollinger nới rộng bình thường"),
}


def _bool_label(key: str, value: bool) -> str | None:
    pair = BOOL_LABELS.get(key)
    if pair is None:
        return None
    return pair[0] if value else pair[1]


def with_vi_labels(obj: Any, *, suffix: str = "_vi") -> Any:
    """Add a ``<key>_vi`` sibling for every machine-enum value in a payload.

    The raw enum is kept alongside it: the skill's paths and every existing
    test still read the original key, and a Vietnamese-only payload would break
    them. Nothing is added when there is no mapping, so numbers and dates are
    untouched.
    """
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for key, value in obj.items():
            if isinstance(key, str) and not key.endswith(suffix):
                label = (
                    _bool_label(key, value)
                    if isinstance(value, bool)
                    else vi_label(value)
                )
                if label is not None:
                    out[key + suffix] = label
            out[key] = with_vi_labels(value, suffix=suffix)
        return out
    if isinstance(obj, list):
        return [with_vi_labels(item, suffix=suffix) for item in obj]
    return obj
