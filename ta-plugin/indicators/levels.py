"""Support and resistance levels that a close-only feed can actually support.

This module replaces the earlier ``pivots.py``, which estimated an intraday
high and low from ``max/min(close, prev_close)`` plus a volatility buffer and
fed them into the classic Pivot Point formulas. Two problems made that
unusable:

* The feed has no high or low. ``engine.UNSUPPORTED_METRICS`` refuses
  ``wick_support_resistance`` by name for exactly that reason, so
  manufacturing a range and publishing seven precise VND levels from it
  contradicted the engine's own contract.
* Because the estimate was built from the same session it was then compared
  against, ``close`` was algebraically always between S1 and R1, and above or
  below PP purely according to the sign of ``close - prev_close``. Over all
  1,530 ticker-sessions in the bundled fixture the reported position was a
  1:1 restatement of "did it close up today"; five of the seven branches were
  unreachable.

What is left after removing the invention is still rich, because every level
here is a real observed or computed number:

* **swing highs / lows of close** — a close that is the highest (lowest) in a
  ±``lookback`` session window. Genuine price structure, just measured on
  closes rather than wicks.
* **N-session close extremes** — the highest and lowest close over 20 / 60 /
  120 / 250 sessions, with the date each occurred.
* **moving averages** — SMA and EMA values passed in from the trend group.
* **Bollinger bands** — upper and lower, passed in from the volatility group.

Every level carries a ``basis`` naming its origin and, where meaningful, the
``date`` it was set. Levels from different sources that sit within
``cluster_pct`` of each other are merged into one level with a ``confluence``
count and the list of contributing bases, which is the honest version of
"strong level": several independent measures agreeing on a price.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from indicators import finite, insufficient, is_insufficient, pick

#: Levels within this percentage of each other are treated as one zone.
DEFAULT_CLUSTER_PCT = 0.75

#: Windows used for close-extreme levels.
DEFAULT_EXTREME_WINDOWS: tuple[int, ...] = (20, 60, 120, 250)


def swing_points(close: pd.Series, lookback: int = 5) -> dict[str, list[dict]]:
    """Local close extrema: a close that leads its ±`lookback` neighbours.

    Returns the most recent swings first. The final `lookback` sessions are
    excluded because a swing there is not yet confirmed — its right-hand
    neighbours do not exist, and calling an unconfirmed extreme a swing high is
    the close-only equivalent of reading a candle that has not closed.
    """
    values = pd.to_numeric(close, errors="coerce").astype("float64")
    highs: list[dict] = []
    lows: list[dict] = []
    n = len(values)
    if n < 2 * lookback + 1:
        return {"highs": [], "lows": [], "confirmed_through": None}

    for i in range(lookback, n - lookback):
        window = values.iloc[i - lookback : i + lookback + 1]
        current = finite(values.iloc[i])
        if current is None:
            continue
        if current == window.max() and (window < current).any():
            highs.append({"level": round(current, 0), "date": str(values.index[i])})
        elif current == window.min() and (window > current).any():
            lows.append({"level": round(current, 0), "date": str(values.index[i])})

    highs.reverse()
    lows.reverse()
    return {
        "highs": highs,
        "lows": lows,
        "lookback": lookback,
        "confirmed_through": str(values.index[n - lookback - 1]),
    }


def close_extremes(close: pd.Series, windows=DEFAULT_EXTREME_WINDOWS) -> dict[str, dict]:
    """Highest and lowest close over each window, with the date it happened.

    A window longer than the available history is omitted entirely rather than
    silently computed over what exists — a "250-session low" measured over 40
    sessions is a different statistic wearing the same name.
    """
    values = pd.to_numeric(close, errors="coerce").astype("float64").dropna()
    out: dict[str, dict] = {}
    for w in windows:
        if len(values) < w:
            continue
        tail = values.iloc[-w:]
        hi_idx = tail.idxmax()
        lo_idx = tail.idxmin()
        out[f"{w}d"] = {
            "window": w,
            "high": round(float(tail.loc[hi_idx]), 0),
            "high_date": str(hi_idx),
            "low": round(float(tail.loc[lo_idx]), 0),
            "low_date": str(lo_idx),
        }
    return out


def _ma_levels(trend: dict | None) -> list[dict]:
    """Moving-average levels from a computed trend group."""
    out: list[dict] = []
    if not isinstance(trend, dict):
        return out
    for key, entry in trend.items():
        if not (key.startswith("sma_") or key.startswith("ema_")):
            continue
        if is_insufficient(entry) or not isinstance(entry, dict):
            continue
        value = finite(entry.get("latest"))
        if value is None or value <= 0:
            continue
        out.append({
            "level": round(value, 0),
            "basis": key.upper().replace("_", ""),
            "kind": "moving_average",
            "direction": entry.get("direction"),
        })
    return out


def _bollinger_levels(volatility: dict | None) -> list[dict]:
    """Upper and lower Bollinger band as levels."""
    band = pick(volatility, "bollinger")
    if band is None or is_insufficient(band) or not isinstance(band, dict):
        return []
    latest_band = band.get("latest") or {}
    out: list[dict] = []
    for field, label in (("upper", "Bollinger trên"), ("lower", "Bollinger dưới")):
        value = finite(latest_band.get(field))
        if value is not None and value > 0:
            out.append({"level": round(value, 0), "basis": label, "kind": "bollinger"})
    return out


def _cluster(levels: list[dict], close: float, cluster_pct: float) -> list[dict]:
    """Merge levels within `cluster_pct` of each other into confluence zones.

    The merged level is the mean of its members, so a zone where SMA50 and the
    60-session close low nearly coincide reports one price with
    ``confluence: 2`` rather than two near-duplicate rows.
    """
    if not levels:
        return []
    ordered = sorted(levels, key=lambda item: item["level"])
    zones: list[list[dict]] = [[ordered[0]]]
    for item in ordered[1:]:
        anchor = zones[-1][0]["level"]
        if anchor > 0 and abs(item["level"] - anchor) / anchor * 100 <= cluster_pct:
            zones[-1].append(item)
        else:
            zones.append([item])

    merged: list[dict] = []
    for zone in zones:
        prices = [item["level"] for item in zone]
        level = round(sum(prices) / len(prices), 0)
        bases = []
        for item in zone:
            label = item["basis"]
            if item.get("date"):
                label = f"{label} ({item['date']})"
            if label not in bases:
                bases.append(label)
        entry: dict[str, Any] = {
            "level": level,
            "basis": " + ".join(bases),
            "bases": bases,
            "confluence": len(bases),
            "distance_pct": round(abs(level - close) / close * 100, 2) if close else None,
        }
        kinds = sorted({item["kind"] for item in zone})
        entry["kinds"] = kinds
        dates = [item["date"] for item in zone if item.get("date")]
        if dates:
            entry["dates"] = dates
        merged.append(entry)
    return merged


def key_levels(
    frame: pd.DataFrame,
    trend: dict | None = None,
    volatility: dict | None = None,
    *,
    swing_lookback: int = 5,
    extreme_windows=DEFAULT_EXTREME_WINDOWS,
    cluster_pct: float = DEFAULT_CLUSTER_PCT,
    max_per_side: int = 6,
) -> dict[str, Any]:
    """Support and resistance levels from close structure, MAs and bands.

    Parameters
    ----------
    frame
        Date-indexed frame containing at least ``close``.
    trend, volatility
        Already-computed group dicts, used for moving-average and Bollinger
        levels. Both optional: without them the result is swing and extreme
        levels only, which is still honest, just sparser.

    Returns
    -------
    dict
        ``supports`` and ``resistances`` (nearest first), each level carrying
        ``basis``, ``confluence`` and ``distance_pct``; plus ``swings``,
        ``close_extremes`` and ``position`` describing where the close sits
        between its nearest support and resistance.
    """
    if "close" not in frame.columns or len(frame) == 0:
        return insufficient("key_levels requires a close series", 1, int(len(frame)))

    close_series = pd.to_numeric(frame["close"], errors="coerce").astype("float64")
    close = finite(close_series.iloc[-1])
    if close is None or close <= 0:
        return insufficient("key_levels requires a positive latest close", 1, int(len(frame)))

    swings = swing_points(close_series, swing_lookback)
    extremes = close_extremes(close_series, extreme_windows)

    candidates: list[dict] = []

    # Swing structure: the three most recent confirmed swings each side is
    # enough for a report; older ones are usually superseded by the extremes.
    # Labelled by structure, not by role: a former swing low sitting above the
    # current close is resistance now, so calling it "đáy" in the resistance
    # list would read as a contradiction. Membership in supports/resistances
    # already carries the role.
    for entry in swings["highs"][:3]:
        candidates.append({
            "level": entry["level"],
            "basis": "mốc swing close",
            "kind": "swing",
            "date": entry["date"],
        })
    for entry in swings["lows"][:3]:
        candidates.append({
            "level": entry["level"],
            "basis": "mốc swing close",
            "kind": "swing",
            "date": entry["date"],
        })

    for key, entry in extremes.items():
        candidates.append({
            "level": entry["high"],
            "basis": f"đỉnh close {key}",
            "kind": "extreme",
            "date": entry["high_date"],
        })
        candidates.append({
            "level": entry["low"],
            "basis": f"đáy close {key}",
            "kind": "extreme",
            "date": entry["low_date"],
        })

    candidates.extend(_ma_levels(trend))
    candidates.extend(_bollinger_levels(volatility))

    supports_raw = [c for c in candidates if c["level"] < close]
    resistances_raw = [c for c in candidates if c["level"] > close]

    supports = _cluster(supports_raw, close, cluster_pct)
    resistances = _cluster(resistances_raw, close, cluster_pct)
    # Nearest first.
    supports.sort(key=lambda item: -item["level"])
    resistances.sort(key=lambda item: item["level"])
    supports = supports[:max_per_side]
    resistances = resistances[:max_per_side]

    nearest_support = supports[0] if supports else None
    nearest_resistance = resistances[0] if resistances else None
    position = _describe_position(close, nearest_support, nearest_resistance)

    return {
        "date": str(frame.index[-1]),
        "latest_close": close,
        "basis_note": (
            "Mọi mức giá đều tính trên GIÁ ĐÓNG CỬA (feed không có high/low "
            "trong phiên); đây không phải đỉnh/đáy nến."
        ),
        "supports": supports,
        "resistances": resistances,
        "nearest_support": nearest_support,
        "nearest_resistance": nearest_resistance,
        "position": position,
        # Only the recent swings are returned. The full history can run to
        # dozens of entries per side, which is payload a consumer pays for and
        # never reads: the level list above already draws on the newest three.
        "swings": {
            "highs": swings["highs"][:5],
            "lows": swings["lows"][:5],
            "lookback": swings.get("lookback"),
            "confirmed_through": swings.get("confirmed_through"),
            "highs_total": len(swings["highs"]),
            "lows_total": len(swings["lows"]),
        },
        "close_extremes": extremes,
        "cluster_pct": cluster_pct,
    }


def _describe_position(
    close: float, support: dict | None, resistance: dict | None
) -> dict[str, Any]:
    """Where the close sits in the band between nearest support and resistance."""
    out: dict[str, Any] = {
        "support_level": support["level"] if support else None,
        "resistance_level": resistance["level"] if resistance else None,
        "support_distance_pct": support["distance_pct"] if support else None,
        "resistance_distance_pct": resistance["distance_pct"] if resistance else None,
    }
    if support is None and resistance is None:
        out["reading"] = "no_levels"
        out["description"] = "Chưa xác định được mức hỗ trợ/kháng cự nào từ dữ liệu hiện có."
        return out
    if resistance is None:
        out["reading"] = "above_all_levels"
        out["description"] = (
            f"Giá đang cao hơn mọi mức tham chiếu tính được; hỗ trợ gần nhất "
            f"{support['level']:,.0f} VND (−{support['distance_pct']:.2f}%)."
        )
        return out
    if support is None:
        out["reading"] = "below_all_levels"
        out["description"] = (
            f"Giá đang thấp hơn mọi mức tham chiếu tính được; kháng cự gần nhất "
            f"{resistance['level']:,.0f} VND (+{resistance['distance_pct']:.2f}%)."
        )
        return out

    span = resistance["level"] - support["level"]
    pct_in_band = round((close - support["level"]) / span * 100, 1) if span > 0 else None
    out["pct_in_band"] = pct_in_band
    if pct_in_band is None:
        out["reading"] = "degenerate_band"
    elif pct_in_band >= 75:
        out["reading"] = "testing_resistance"
    elif pct_in_band <= 25:
        out["reading"] = "testing_support"
    else:
        out["reading"] = "mid_band"

    labels = {
        "testing_resistance": "áp sát vùng kháng cự",
        "testing_support": "áp sát vùng hỗ trợ",
        "mid_band": "ở giữa vùng hỗ trợ và kháng cự",
        "degenerate_band": "trong vùng hỗ trợ/kháng cự chồng lấn",
    }
    out["description"] = (
        f"Giá {close:,.0f} VND {labels[out['reading']]}: hỗ trợ {support['level']:,.0f} VND "
        f"(−{support['distance_pct']:.2f}%, {support['basis']}), kháng cự "
        f"{resistance['level']:,.0f} VND (+{resistance['distance_pct']:.2f}%, {resistance['basis']})."
    )
    return out
