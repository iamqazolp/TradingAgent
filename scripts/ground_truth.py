"""Independent ground-truth indicator implementation.

Deliberately shares NO code with the engine: it parses the raw fixture JSON
itself, uses only the standard library (`math`, `statistics`), and reimplements
every Tier 0 indicator from its definition. If this file and
`indicators/` agree, two independent implementations agree; if they were written
against the same helpers, agreement would prove nothing.

    uv run python scripts/ground_truth.py            # writes tests/fixtures/ground_truth.json

Conventions fixed here (and matched in the engine, documented in README.md):
  * EMA is seeded with the SMA of the first n values (TA-Lib convention).
  * MACD signal is an EMA of the live MACD section only.
  * RSI uses Wilder smoothing; a window with no losses and no gains reads 50.
  * Bollinger uses population stdev (ddof=0); realized volatility uses sample
    stdev (ddof=1).
  * "vs baseline" ratios divide today by the mean of the previous n days,
    excluding today.
"""

from __future__ import annotations

import json
import math
import statistics
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "sample_daily_data.json"
OUTPUT = REPO_ROOT / "tests" / "fixtures" / "ground_truth.json"

#: Rows per ticker, matching the get_price_data default lookback.
LOOKBACK = 300


# --------------------------------------------------------------------------- parsing


def _num(text: object) -> float:
    """Cast one raw string field to a float. Blank means zero."""
    if text is None:
        return 0.0
    cleaned = str(text).strip().replace(",", "")
    if cleaned in ("", "-", "--"):
        return 0.0
    return float(cleaned)


def load_fixture(path: Path = FIXTURE) -> dict[str, list[dict]]:
    """Group raw fixture records by ticker, ascending by date, own parser."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    records = payload["Data"] if isinstance(payload, dict) else payload
    by_ticker: dict[str, list[dict]] = {}
    for raw in records:
        row = {
            "date": datetime.strptime(raw["Date"], "%d/%m/%Y").strftime("%Y-%m-%d"),
            "prev_close": _num(raw["PricePreviousClose"]),
            "close": _num(raw["PriceClose"]),
            "total_trade": _num(raw["TotalTrade"]),
            "total_value": _num(raw["TotalValue"]),
            "total_volume": _num(raw["TotalVolume"]),
            "buy_count": _num(raw["BuyCount"]),
            "sell_count": _num(raw["SellCount"]),
            "buy_volume": _num(raw["BuyQuantity"]),
            "sell_volume": _num(raw["SellQuantity"]),
            "foreign_buy_volume": _num(raw["ForeignerBuyQuantity"]),
            "foreign_sell_volume": _num(raw["ForeignerSellQuantity"]),
            "foreign_buy_value": _num(raw["ForeignerBuyValue"]),
            "foreign_sell_value": _num(raw["ForeignerSellValue"]),
            "foreign_room": _num(raw["CurrentForeignRoom"]),
        }
        by_ticker.setdefault(str(raw["Symbol"]).upper(), []).append(row)
    for rows in by_ticker.values():
        rows.sort(key=lambda r: r["date"])
    return by_ticker


# --------------------------------------------------------------------------- indicators


def sma(values: list[float], n: int) -> float | None:
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def ema_list(values: list[float], n: int) -> list[float | None]:
    """SMA-seeded EMA over the whole list; leading entries are None."""
    if len(values) < n:
        return [None] * len(values)
    out: list[float | None] = [None] * len(values)
    alpha = 2.0 / (n + 1.0)
    current = sum(values[:n]) / n
    out[n - 1] = current
    for i in range(n, len(values)):
        current = values[i] * alpha + current * (1.0 - alpha)
        out[i] = current
    return out


def macd(values: list[float], fast: int = 12, slow: int = 26, signal: int = 9) -> dict:
    if len(values) < slow + signal - 1:
        return {"macd": None, "signal": None, "histogram": None}
    fast_ema = ema_list(values, fast)
    slow_ema = ema_list(values, slow)
    line = [
        None if f is None or s is None else f - s for f, s in zip(fast_ema, slow_ema)
    ]
    live = [v for v in line if v is not None]
    signal_live = ema_list(live, signal)
    macd_now = live[-1]
    signal_now = signal_live[-1]
    if signal_now is None:
        return {"macd": macd_now, "signal": None, "histogram": None}
    return {
        "macd": macd_now,
        "signal": signal_now,
        "histogram": macd_now - signal_now,
    }


def rsi(values: list[float], n: int = 14) -> float | None:
    """Wilder RSI, computed by hand from the deltas."""
    if len(values) < n + 1:
        return None
    deltas = [values[i] - values[i - 1] for i in range(1, len(values))]
    gains = [d if d > 0 else 0.0 for d in deltas]
    losses = [-d if d < 0 else 0.0 for d in deltas]
    avg_gain = sum(gains[:n]) / n
    avg_loss = sum(losses[:n]) / n
    for i in range(n, len(deltas)):
        avg_gain = (avg_gain * (n - 1) + gains[i]) / n
        avg_loss = (avg_loss * (n - 1) + losses[i]) / n
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def bollinger(values: list[float], n: int = 20, k: float = 2.0) -> dict:
    if len(values) < n:
        return {"middle": None, "upper": None, "lower": None, "percent_b": None}
    window = values[-n:]
    middle = sum(window) / n
    stdev = statistics.pstdev(window)  # population stdev, classic Bollinger
    upper = middle + k * stdev
    lower = middle - k * stdev
    span = upper - lower
    return {
        "middle": middle,
        "upper": upper,
        "lower": lower,
        "percent_b": None if span == 0 else (values[-1] - lower) / span,
        "width": None if middle == 0 else span / middle,
    }


def close_to_close_volatility(values: list[float], n: int = 20) -> float | None:
    """Sample stdev of the last n daily log returns, in percent."""
    if len(values) < n + 1:
        return None
    returns = [
        math.log(values[i] / values[i - 1]) for i in range(len(values) - n, len(values))
    ]
    return statistics.stdev(returns) * 100.0


def imbalance(buy: list[float], sell: list[float]) -> list[float | None]:
    out: list[float | None] = []
    for b, s in zip(buy, sell):
        total = b + s
        out.append(None if total == 0 else (b - s) / total)
    return out


def mean_of_last(values: list[float | None], n: int) -> float | None:
    """Mean of the last n entries; None if any is missing (gaps stay gaps)."""
    if len(values) < n:
        return None
    window = values[-n:]
    if any(v is None for v in window):
        return None
    return sum(window) / n  # type: ignore[arg-type]


def obv(rows: list[dict]) -> float | None:
    """Cumulative signed volume, direction from the provider's prev_close."""
    total = 0.0
    for row in rows:
        if row["prev_close"] <= 0:
            return None
        if row["close"] > row["prev_close"]:
            total += row["total_volume"]
        elif row["close"] < row["prev_close"]:
            total -= row["total_volume"]
    return total


def ratio_to_prior_mean(values: list[float | None], n: int) -> float | None:
    """Today divided by the mean of the n days before today."""
    if len(values) < n + 1 or values[-1] is None:
        return None
    baseline_window = values[-(n + 1) : -1]
    if any(v is None for v in baseline_window):
        return None
    baseline = sum(baseline_window) / n  # type: ignore[arg-type]
    return None if baseline == 0 else values[-1] / baseline


def per_row_div(numerators: list[float], denominators: list[float]) -> list[float | None]:
    return [None if d == 0 else n / d for n, d in zip(numerators, denominators)]


# --------------------------------------------------------------------------- assembly


def ticker_truth(rows: list[dict], window: int = 5) -> dict:
    """Every Tier 0 value for one ticker's row list."""
    rows = rows[-LOOKBACK:]
    close = [r["close"] for r in rows]

    buy_volume = [r["buy_volume"] for r in rows]
    sell_volume = [r["sell_volume"] for r in rows]
    buy_count = [r["buy_count"] for r in rows]
    sell_count = [r["sell_count"] for r in rows]
    total_value = [r["total_value"] for r in rows]
    total_trade = [r["total_trade"] for r in rows]
    total_volume = [r["total_volume"] for r in rows]

    volume_imbalance = imbalance(buy_volume, sell_volume)
    count_imbalance = imbalance(buy_count, sell_count)
    buy_size = per_row_div(buy_volume, buy_count)
    sell_size = per_row_div(sell_volume, sell_count)
    avg_trade_value = per_row_div(total_value, total_trade)

    foreign_net_volume = [
        r["foreign_buy_volume"] - r["foreign_sell_volume"] for r in rows
    ]
    foreign_net_value = [r["foreign_buy_value"] - r["foreign_sell_value"] for r in rows]
    participation = per_row_div(
        [r["foreign_buy_volume"] + r["foreign_sell_volume"] for r in rows], total_volume
    )
    room_change = [
        rows[i]["foreign_room"] - rows[i - 1]["foreign_room"] for i in range(1, len(rows))
    ]

    return {
        "rows": len(rows),
        "date_range": {"start": rows[0]["date"], "end": rows[-1]["date"]},
        "latest_close": close[-1],
        "sma_20": sma(close, 20),
        "sma_50": sma(close, 50),
        "sma_200": sma(close, 200),
        "ema_12": ema_list(close, 12)[-1],
        "ema_26": ema_list(close, 26)[-1],
        "macd": macd(close),
        "rsi_14": rsi(close, 14),
        "bollinger_20_2": bollinger(close, 20, 2.0),
        "close_to_close_volatility_20": close_to_close_volatility(close, 20),
        "buy_sell_volume_imbalance": volume_imbalance[-1],
        "buy_sell_volume_imbalance_avg_5": mean_of_last(volume_imbalance, window),
        "buy_sell_count_imbalance": count_imbalance[-1],
        "buy_sell_count_imbalance_avg_5": mean_of_last(count_imbalance, window),
        "obv": obv(rows),
        "avg_buy_trade_size": buy_size[-1],
        "avg_sell_trade_size": sell_size[-1],
        "avg_buy_trade_size_ratio_20": ratio_to_prior_mean(buy_size, 20),
        "avg_sell_trade_size_ratio_20": ratio_to_prior_mean(sell_size, 20),
        "avg_trade_value": avg_trade_value[-1],
        "avg_trade_value_ratio_20": ratio_to_prior_mean(avg_trade_value, 20),
        "value_spike_ratio_20": ratio_to_prior_mean(
            [float(v) for v in total_value], 20
        ),
        "foreign_net_volume": foreign_net_volume[-1],
        "foreign_net_volume_cum": sum(foreign_net_volume),
        "foreign_net_value": foreign_net_value[-1],
        "foreign_net_value_cum": sum(foreign_net_value),
        "foreign_participation_ratio": participation[-1],
        "foreign_participation_ratio_avg_5": mean_of_last(participation, window),
        "foreign_room_trend_5": sum(room_change[-window:]) if len(room_change) >= window else None,
    }


def build(path: Path = FIXTURE) -> dict:
    by_ticker = load_fixture(path)
    return {
        "source": path.name,
        "lookback_rows": LOOKBACK,
        "flow_window": 5,
        "note": (
            "Independent pure-stdlib reimplementation used to validate the pandas "
            "engine. Regenerate with scripts/ground_truth.py after changing fixtures."
        ),
        "tickers": {t: ticker_truth(rows) for t, rows in sorted(by_ticker.items())},
    }


def main() -> int:
    truth = build()
    OUTPUT.write_text(json.dumps(truth, indent=1), encoding="utf-8")
    print(f"{OUTPUT}: {len(truth['tickers'])} tickers")
    for ticker, values in truth["tickers"].items():
        print(
            f"  {ticker}: rows={values['rows']} close={values['latest_close']:.0f} "
            f"rsi14={values['rsi_14']:.2f} sma20={values['sma_20']:.1f} "
            f"macd={values['macd']['macd']:.2f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
