"""Market Breadth indicators for Vietnamese stock exchanges.

Computes Advance/Decline statistics, AD ratio, and Breadth Regime
from market index snapshot rows (e.g. from store.get_latest_market_indices
or StockbizClient.get_all_market_info).
"""

from __future__ import annotations

from typing import Any

from indicators import finite, safe_div


def market_breadth_summary(market_info_rows: list[dict]) -> dict:
    """Calculate market breadth summary for each exchange in market_info_rows.

    Parameters
    ----------
    market_info_rows:
        List of dicts from `store.get_latest_market_indices()` or
        `StockbizClient.get_all_market_info()`.

    Returns
    -------
    dict:
        Structured market breadth metrics including advances, declines,
        unchanged, ad_ratio, breadth_regime, total_value_vnd, total_volume.
    """
    if not market_info_rows:
        return {
            "exchanges": {},
            "message": "no market info data available",
        }

    exchanges_dict: dict[str, dict[str, Any]] = {}
    dates: list[str] = []

    for row in market_info_rows:
        raw_ex = row.get("exchange") if "exchange" in row else row.get("Exchange")
        if not raw_ex:
            raw_ex = "UNKNOWN"
        exchange = str(raw_ex).upper().strip()

        dt = row.get("date") if "date" in row else row.get("Date")
        if dt:
            dt_str = str(dt).split("T")[0].split(" ")[0]
            dates.append(dt_str)
        else:
            dt_str = None

        idx_curr = finite(row.get("index_current") if "index_current" in row else row.get("IndexCurrent"))
        idx_chg = finite(row.get("index_change") if "index_change" in row else row.get("IndexChange"))
        idx_pct_chg = finite(row.get("index_percent_change") if "index_percent_change" in row else row.get("IndexPercentChange"))

        def _cast_int(val: Any) -> int:
            f = finite(val)
            return int(f) if f is not None else 0

        advances = _cast_int(row.get("advances") if "advances" in row else row.get("Advances"))
        declines = _cast_int(row.get("declines") if "declines" in row else row.get("Declines"))
        unchanged = _cast_int(row.get("unchanged") if "unchanged" in row else row.get("Unchanged"))

        tot_val = finite(row.get("total_value") if "total_value" in row else row.get("TotalValue")) or 0.0
        tot_vol = _cast_int(row.get("total_volume") if "total_volume" in row else row.get("TotalVolume"))
        tot_trade = _cast_int(row.get("total_trade") if "total_trade" in row else row.get("TotalTrade"))

        ad_ratio = safe_div(advances, declines)
        if ad_ratio is not None:
            ad_ratio = round(ad_ratio, 2)
            if ad_ratio >= 2.0:
                regime = "strongly_bullish"
            elif ad_ratio >= 1.2:
                regime = "bullish"
            elif ad_ratio >= 0.8:
                regime = "neutral"
            elif ad_ratio >= 0.5:
                regime = "bearish"
            else:
                regime = "strongly_bearish"
        else:
            if advances > 0 and declines == 0:
                regime = "strongly_bullish"
            elif advances == 0 and declines > 0:
                regime = "strongly_bearish"
            else:
                regime = "neutral"

        ex_summary = {
            "exchange": exchange,
            "date": dt_str,
            "index_current": idx_curr,
            "index_change": idx_chg,
            "index_percent_change": idx_pct_chg,
            "advances": advances,
            "declines": declines,
            "unchanged": unchanged,
            "ad_ratio": ad_ratio,
            "breadth_regime": regime,
            "total_value_vnd": tot_val,
            "total_volume": tot_vol,
            "total_trade": tot_trade,
        }
        exchanges_dict[exchange] = ex_summary

    res: dict[str, Any] = {
        "exchanges": exchanges_dict,
        "date": dates[0] if dates else None,
    }
    for k, v in exchanges_dict.items():
        res[k] = v

    if len(exchanges_dict) == 1:
        single = next(iter(exchanges_dict.values()))
        for k, v in single.items():
            if k not in res:
                res[k] = v

    return res
