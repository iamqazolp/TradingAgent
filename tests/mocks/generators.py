"""Synthetic data generation for standalone mock market and foreign flow APIs."""

from __future__ import annotations

import hashlib
import math
import random
from datetime import date, datetime, timedelta

# Default profiles for common Vietnamese tickers
TICKER_PROFILES: dict[str, tuple[float, float, int, float, int]] = {
    "VNM": (68_500.0, 0.014, 3_200_000, 0.22, 2_089_000_000),
    "HPG": (27_300.0, 0.019, 18_000_000, 0.14, 6_396_000_000),
    "TNG": (21_800.0, 0.026, 1_100_000, 0.01, 114_000_000),
    "FPT": (95_000.0, 0.016, 4_000_000, 0.25, 1_200_000_000),
    "MWG": (48_000.0, 0.020, 6_500_000, 0.18, 1_400_000_000),
    "SSI": (32_000.0, 0.024, 12_000_000, 0.12, 1_500_000_000),
    "VCB": (88_000.0, 0.012, 2_000_000, 0.15, 5_589_000_000),
}


def _get_profile(symbol: str) -> tuple[float, float, int, float, int]:
    sym = symbol.upper()
    if sym in TICKER_PROFILES:
        return TICKER_PROFILES[sym]
    # Deterministic profile for arbitrary ticker symbols based on MD5 hash
    seed = int(hashlib.md5(sym.encode("utf-8")).hexdigest()[:8], 16)
    rng = random.Random(seed)
    price = round(rng.uniform(15_000, 80_000), -2)
    vol = rng.uniform(0.015, 0.030)
    base_vol = int(rng.uniform(500_000, 8_000_000))
    foreign_app = rng.uniform(0.05, 0.30)
    shares = int(rng.uniform(100_000_000, 2_000_000_000))
    return (price, vol, base_vol, foreign_app, shares)


def _trading_days(start_dt: date, end_dt: date) -> list[date]:
    """Generate list of weekdays between start_dt and end_dt."""
    days: list[date] = []
    curr = start_dt
    while curr <= end_dt:
        if curr.weekday() < 5:
            days.append(curr)
        curr += timedelta(days=1)
    return days


def _vnd(val: float, *, commas: bool = True) -> str:
    rounded = int(round(val))
    return f"{rounded:,}" if commas else str(rounded)


def _simulate_daily_series(symbol: str, days: list[date], start: str) -> tuple[list[dict], int]:
    """Generate unified daily price, volume, and foreign trajectory."""
    start_price, vol, base_vol, foreign_app, shares = _get_profile(symbol)
    seed = int(hashlib.md5(f"{symbol.upper()}_{start}".encode("utf-8")).hexdigest()[:8], 16)
    rng = random.Random(seed)

    price = start_price
    series = []
    for day in days:
        prev_close = price
        drift = 0.0004 + rng.gauss(0.0, vol)
        close_price = max(1_000.0, round(prev_close * (1.0 + drift), -1))

        open_shock = rng.gauss(0.0, vol * 0.4)
        open_price = max(1_000.0, round(prev_close * (1.0 + open_shock), -1))

        high_excursion = abs(rng.gauss(0.0, vol * 0.8))
        low_excursion = abs(rng.gauss(0.0, vol * 0.8))
        high_price = max(close_price, open_price, round(max(close_price, open_price) * (1.0 + high_excursion), -1))
        low_price = max(1_000.0, min(close_price, open_price, round(min(close_price, open_price) * (1.0 - low_excursion), -1)))

        turnover = max(1_000, int(base_vol * math.exp(rng.gauss(0.0, 0.4))))
        lean = max(0.2, min(0.8, 0.5 + ((close_price - prev_close) / prev_close) * 6.0 + rng.gauss(0.0, 0.05)))

        if rng.random() < foreign_app * 4:
            f_buy_vol = int(turnover * abs(rng.gauss(0.0, foreign_app / 2)))
            f_sell_vol = int(turnover * abs(rng.gauss(0.0, foreign_app / 2)))
        else:
            f_buy_vol = f_sell_vol = 0

        series.append({
            "day": day,
            "prev_close": prev_close,
            "open": open_price,
            "high": high_price,
            "low": low_price,
            "close": close_price,
            "volume": turnover,
            "lean": lean,
            "f_buy_vol": f_buy_vol,
            "f_sell_vol": f_sell_vol,
        })
        price = close_price
    return series, shares


def generate_foreign_flow(symbol: str, start: str, end: str) -> list[dict]:
    """Source 1 API: Daily foreign trading and foreign room statistics."""
    try:
        start_date = date.fromisoformat(start)
        end_date = date.fromisoformat(end)
    except ValueError:
        start_date = date.today() - timedelta(days=30)
        end_date = date.today()

    days = _trading_days(start_date, end_date)
    series, shares = _simulate_daily_series(symbol, days, start)
    room = int(shares * 0.49 * 0.30)
    records: list[dict] = []

    for item in series:
        day = item["day"]
        price = item["close"]
        prev_close = item["prev_close"]
        f_buy_vol = item["f_buy_vol"]
        f_sell_vol = item["f_sell_vol"]

        f_buy_val = f_buy_vol * price
        f_sell_val = f_sell_vol * price
        room = max(0, room - (f_buy_vol - f_sell_vol))

        records.append({
            "Symbol": symbol.upper(),
            "Date": day.strftime("%d/%m/%Y"),
            "PricePreviousClose": f"{prev_close:.0f}",
            "PriceClose": f"{price:.0f}",
            "ForeignerBuyQuantity": str(f_buy_vol) if f_buy_vol else "",
            "ForeignerSellQuantity": str(f_sell_vol) if f_sell_vol else "",
            "ForeignerBuyValue": _vnd(f_buy_val) if f_buy_vol else "",
            "ForeignerSellValue": _vnd(f_sell_val) if f_sell_vol else "",
            "CurrentForeignRoom": _vnd(room),
        })

    return records


def generate_market_bars(
    symbol: str, start: str, end: str, *, timeframe: str = "1D"
) -> list[dict]:
    """Source 2 API: Hourly (1H/4H) and Daily (1D) OHLC candlesticks & order flow."""
    try:
        start_date = date.fromisoformat(start)
        end_date = date.fromisoformat(end)
    except ValueError:
        start_date = date.today() - timedelta(days=30)
        end_date = date.today()

    days = _trading_days(start_date, end_date)
    series, _ = _simulate_daily_series(symbol, days, start)
    is_hourly = timeframe.upper() in ("1H", "4H", "HOUR", "HOURLY")
    records: list[dict] = []

    for item in series:
        day = item["day"]
        daily_close = item["close"]
        daily_open = item["open"]
        daily_high = item["high"]
        daily_low = item["low"]
        daily_prev_close = item["prev_close"]
        daily_volume = item["volume"]
        lean = item["lean"]

        if not is_hourly:
            buy_volume = int(daily_volume * lean)
            sell_volume = daily_volume - buy_volume
            ticket_val = max(5_000_000.0, 30_000_000.0)
            avg_ticket = max(50, int(ticket_val / daily_close))
            total_trade = max(5, daily_volume // avg_ticket)
            buy_count = max(1, int(total_trade * lean))
            sell_count = max(1, total_trade - buy_count)
            total_value = daily_volume * daily_close

            records.append({
                "Symbol": symbol.upper(),
                "Date": day.strftime("%Y-%m-%d"),
                "PricePreviousClose": f"{daily_prev_close:.0f}",
                "PriceOpen": f"{daily_open:.0f}",
                "PriceHigh": f"{daily_high:.0f}",
                "PriceLow": f"{daily_low:.0f}",
                "PriceClose": f"{daily_close:.0f}",
                "TotalVolume": str(daily_volume),
                "TotalTrade": str(total_trade),
                "TotalValue": _vnd(total_value, commas=False),
                "BuyQuantity": str(buy_volume),
                "SellQuantity": str(sell_volume),
                "BuyCount": str(buy_count),
                "SellCount": str(sell_count),
            })
        else:
            # 5 hourly trading periods: 09:00, 10:00, 11:00, 13:00, 14:00
            session_hours = [9, 10, 11, 13, 14]
            session_vol_factor = 1.0 / len(session_hours)
            bar_price = daily_prev_close
            for idx, hour in enumerate(session_hours):
                is_last = (idx == len(session_hours) - 1)
                is_first = (idx == 0)
                bar_open = daily_open if is_first else bar_price
                bar_close = daily_close if is_last else round((daily_open + daily_close) / 2, -1)
                bar_high = daily_high if (bar_close == daily_high or is_last) else max(bar_open, bar_close) + 50.0
                bar_low = daily_low if (bar_close == daily_low or is_last) else min(bar_open, bar_close) - 50.0
                bar_volume = max(100, int(daily_volume * session_vol_factor))
                buy_volume = int(bar_volume * lean)
                sell_volume = bar_volume - buy_volume
                total_trade = max(5, bar_volume // 200)
                buy_count = max(1, int(total_trade * lean))
                sell_count = max(1, total_trade - buy_count)
                total_value = bar_volume * bar_close

                dt_str = datetime(day.year, day.month, day.day, hour, 0, 0).strftime("%Y-%m-%d %H:%M:%S")
                records.append({
                    "Symbol": symbol.upper(),
                    "Date": dt_str,
                    "PricePreviousClose": f"{daily_prev_close:.0f}",
                    "PriceOpen": f"{bar_open:.0f}",
                    "PriceHigh": f"{bar_high:.0f}",
                    "PriceLow": f"{bar_low:.0f}",
                    "PriceClose": f"{bar_close:.0f}",
                    "TotalVolume": str(bar_volume),
                    "TotalTrade": str(total_trade),
                    "TotalValue": _vnd(total_value, commas=False),
                    "BuyQuantity": str(buy_volume),
                    "SellQuantity": str(sell_volume),
                    "BuyCount": str(buy_count),
                    "SellCount": str(sell_count),
                })
                bar_price = bar_close

    return records
