"""Generate synthetic fixtures in the exact shape of the GetTradingStatistics feed.

These are SYNTHETIC. No live endpoint was available when this project was built,
so the fixtures reproduce the documented response *shape* (every numeric field a
string, some with thousands separators, some blank) and realistic Vietnamese
market magnitudes, not real prices. Point `TA_AGENT_API_URL` at the real feed and
re-run `python -m data.ingest --ticker ... --start ... --end ...` to replace them.

Deterministic: seeded per ticker, so the committed fixtures and the ground-truth
file stay in lockstep.

    uv run python scripts/generate_fixtures.py
"""

from __future__ import annotations

import json
import math
import random
from datetime import date, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures"

START = date(2024, 1, 2)
TRADING_DAYS = 520  # a little over two years

#: Non-weekend market holidays, so the fixtures contain real calendar gaps.
HOLIDAYS = {
    date(2024, 2, 8), date(2024, 2, 9), date(2024, 2, 12), date(2024, 2, 13),
    date(2024, 2, 14), date(2024, 4, 18), date(2024, 4, 30), date(2024, 5, 1),
    date(2024, 9, 2), date(2025, 1, 1), date(2025, 1, 28), date(2025, 1, 29),
    date(2025, 1, 30), date(2025, 1, 31), date(2025, 4, 30), date(2025, 5, 1),
    date(2025, 9, 2), date(2026, 1, 1), date(2026, 2, 16), date(2026, 2, 17),
    date(2026, 2, 18), date(2026, 4, 30), date(2026, 5, 1),
}

#: name -> (seed, start price VND, daily vol, base volume, foreign appetite, shares out)
TICKERS = {
    "VNM": (11, 68_500.0, 0.014, 3_200_000, 0.22, 2_089_000_000),
    "HPG": (22, 27_300.0, 0.019, 18_000_000, 0.14, 6_396_000_000),
    "TNG": (33, 21_800.0, 0.026, 1_100_000, 0.01, 114_000_000),
}

#: One day where VNM's foreign room moves for a structural reason (a foreign
#: ownership limit change), not because of that day's trading.
ROOM_STRUCTURAL_EVENT = (date(2025, 6, 10), 40_000_000)


def trading_days(start: date, count: int) -> list[date]:
    """`count` weekdays from `start`, skipping the holiday set."""
    out: list[date] = []
    cursor = start
    while len(out) < count:
        if cursor.weekday() < 5 and cursor not in HOLIDAYS:
            out.append(cursor)
        cursor += timedelta(days=1)
    return out


def _vnd(value: float, *, separators: bool = False) -> str:
    """Format a number the way the feed does: as a string, sometimes with commas."""
    rounded = int(round(value))
    return f"{rounded:,}" if separators else str(rounded)


def generate_ticker(symbol: str, days: list[date]) -> list[dict]:
    """One ticker's worth of raw records, oldest first."""
    seed, price, daily_vol, base_volume, foreign_appetite, shares_out = TICKERS[symbol]
    rng = random.Random(seed)
    # Vietnamese market foreign ownership limit: 49% for most non-banking names.
    room = int(shares_out * 0.49 * 0.35)
    records: list[dict] = []

    for i, day in enumerate(days):
        prev_close = price
        # Mild trend regimes plus noise, so trend indicators have something to see.
        drift = 0.0006 * math.sin(i / 45.0) + 0.0002
        shock = rng.gauss(drift, daily_vol)
        price = max(1_000.0, round(prev_close * (1.0 + shock), -1))

        activity = math.exp(rng.gauss(0.0, 0.45))
        # Heavier turnover on bigger moves, which is what real tape looks like.
        activity *= 1.0 + 4.0 * abs(shock)
        total_volume = max(1_000, int(base_volume * activity / 10) * 10)

        # Buy/sell split leans with the day's direction.
        lean = 0.5 + max(-0.28, min(0.28, shock * 9.0)) + rng.gauss(0.0, 0.05)
        lean = max(0.2, min(0.8, lean))
        buy_volume = int(total_volume * lean)
        sell_volume = total_volume - buy_volume

        # Ticket size is set in money, not shares: a retail order in Vietnam is a
        # few tens of millions of VND regardless of the share price.
        ticket_value = max(3_000_000.0, rng.gauss(30_000_000.0, 9_000_000.0))
        avg_ticket = max(100, int(ticket_value / price))
        total_trade = max(10, total_volume // avg_ticket)
        # Buy side trades in slightly larger tickets on up days.
        count_lean = max(0.25, min(0.75, lean - 0.06 * (1 if shock > 0 else -1)))
        buy_count = max(1, int(total_trade * count_lean))
        sell_count = max(1, total_trade - buy_count)

        # ~2% of days carry a small classification residual against the totals,
        # which is exactly the condition ingestion warns about instead of failing.
        if rng.random() < 0.02:
            buy_volume -= max(10, total_volume // 5_000)
            sell_count -= 1 if sell_count > 1 else 0

        average_price = price * (1.0 + rng.gauss(0.0, 0.003))
        total_value = total_volume * average_price

        if rng.random() < foreign_appetite * 4:
            foreign_buy_volume = int(total_volume * abs(rng.gauss(0.0, foreign_appetite / 2)))
            foreign_sell_volume = int(total_volume * abs(rng.gauss(0.0, foreign_appetite / 2)))
            foreign_buy_volume = min(foreign_buy_volume, buy_volume)
            foreign_sell_volume = min(foreign_sell_volume, sell_volume)
        else:
            # Small caps legitimately show no foreign activity on most days.
            foreign_buy_volume = foreign_sell_volume = 0

        foreign_buy_value = foreign_buy_volume * average_price
        foreign_sell_value = foreign_sell_volume * average_price

        room -= foreign_buy_volume - foreign_sell_volume
        if symbol == "VNM" and day == ROOM_STRUCTURAL_EVENT[0]:
            room += ROOM_STRUCTURAL_EVENT[1]  # ownership-limit change, not trading
        room = max(0, room)

        # Open price fluctuates around prev_close
        open_shock = rng.gauss(0.0, daily_vol * 0.4)
        open_price = max(1_000.0, round(prev_close * (1.0 + open_shock), -1))

        # High and Low intraday excursions
        high_excursion = abs(rng.gauss(0.0, daily_vol * 0.8))
        low_excursion = abs(rng.gauss(0.0, daily_vol * 0.8))
        high_price = max(price, open_price, round(max(price, open_price) * (1.0 + high_excursion), -1))
        low_price = min(price, open_price, round(min(price, open_price) * (1.0 - low_excursion), -1))
        low_price = max(1_000.0, low_price)

        records.append(
            {
                "Symbol": symbol,
                "Date": day.strftime("%d/%m/%Y"),
                "PricePreviousClose": f"{prev_close:.0f}",
                "PriceOpen": f"{open_price:.0f}",
                "PriceHigh": f"{high_price:.0f}",
                "PriceLow": f"{low_price:.0f}",
                "PriceClose": f"{price:.0f}",
                "TotalTrade": str(total_trade),
                "TotalValue": _vnd(total_value, separators=True),
                "TotalVolume": str(total_volume),
                "BuyCount": str(buy_count),
                "SellCount": str(sell_count),
                "BuyQuantity": str(buy_volume),
                "SellQuantity": str(sell_volume),
                "ForeignerBuyQuantity": str(foreign_buy_volume) if foreign_buy_volume else "",
                "ForeignerSellQuantity": str(foreign_sell_volume) if foreign_sell_volume else "",
                "ForeignerBuyValue": _vnd(foreign_buy_value, separators=True)
                if foreign_buy_volume
                else "",
                "ForeignerSellValue": _vnd(foreign_sell_value, separators=True)
                if foreign_sell_volume
                else "",
                "CurrentForeignRoom": _vnd(room),
            }
        )
    return records


def edge_case_records() -> list[dict]:
    """Awkward records the ingest and engine layers must handle explicitly."""
    def record(**overrides) -> dict:
        base = {
            "Symbol": "EDGE",
            "Date": "01/03/2026",
            "PricePreviousClose": "10,000",
            "PriceClose": "10,100",
            "TotalTrade": "500",
            "TotalValue": "5,050,000,000",
            "TotalVolume": "500000",
            "BuyCount": "260",
            "SellCount": "240",
            "BuyQuantity": "255000",
            "SellQuantity": "245000",
            "ForeignerBuyQuantity": "1000",
            "ForeignerSellQuantity": "0",
            "ForeignerBuyValue": "10,100,000",
            "ForeignerSellValue": "0",
            "CurrentForeignRoom": "9,000,000",
        }
        base.update(overrides)
        return base

    return [
        # 1. Halted day: no matched volume, no trades. Must become a gap, not a zero.
        record(
            Date="02/03/2026",
            PricePreviousClose="10,100",
            PriceClose="10,100",
            TotalTrade="0",
            TotalValue="0",
            TotalVolume="0",
            BuyCount="0",
            SellCount="0",
            BuyQuantity="0",
            SellQuantity="0",
            ForeignerBuyQuantity="",
            ForeignerSellQuantity="",
            ForeignerBuyValue="",
            ForeignerSellValue="",
        ),
        # 2. Missing close: must be rejected outright.
        record(Date="03/03/2026", PriceClose=""),
        # 3. Missing previous close: rejected too, 0 is not a valid price.
        record(Date="04/03/2026", PricePreviousClose="-"),
        # 4. Unadjusted 2:1 split: prev_close disagrees with the prior close.
        record(Date="05/03/2026", PricePreviousClose="5,050", PriceClose="5,100"),
        # 5. Duplicate of record 1's date, later value wins.
        record(Date="02/03/2026", PriceClose="10,150", TotalVolume="500010"),
        # 6. Blank foreign block on a normally traded day.
        record(
            Date="06/03/2026",
            PricePreviousClose="5,100",
            PriceClose="5,200",
            ForeignerBuyQuantity="",
            ForeignerSellQuantity="",
            ForeignerBuyValue="",
            ForeignerSellValue="",
        ),
        # 7. Newly listed ticker with almost no history.
        {
            "Symbol": "NEWCO",
            "Date": "05/03/2026",
            "PricePreviousClose": "15,000",
            "PriceClose": "15,400",
            "TotalTrade": "80",
            "TotalValue": "1,232,000,000",
            "TotalVolume": "80000",
            "BuyCount": "50",
            "SellCount": "30",
            "BuyQuantity": "48000",
            "SellQuantity": "32000",
            "ForeignerBuyQuantity": "",
            "ForeignerSellQuantity": "",
            "ForeignerBuyValue": "",
            "ForeignerSellValue": "",
            "CurrentForeignRoom": "3,000,000",
        },
        {
            "Symbol": "NEWCO",
            "Date": "06/03/2026",
            "PricePreviousClose": "15,400",
            "PriceClose": "15,250",
            "TotalTrade": "60",
            "TotalValue": "915,000,000",
            "TotalVolume": "60000",
            "BuyCount": "25",
            "SellCount": "35",
            "BuyQuantity": "27000",
            "SellQuantity": "33000",
            "ForeignerBuyQuantity": "",
            "ForeignerSellQuantity": "",
            "ForeignerBuyValue": "",
            "ForeignerSellValue": "",
            "CurrentForeignRoom": "3,000,000",
        },
    ]


def main() -> int:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    days = trading_days(START, TRADING_DAYS)
    records: list[dict] = []
    for symbol in TICKERS:
        records.extend(generate_ticker(symbol, days))

    # The envelope mirrors the ASP.NET-style wrapper the feed uses.
    payload = {"Success": "true", "Message": "", "Data": records}
    sample = FIXTURE_DIR / "sample_daily_data.json"
    sample.write_text(json.dumps(payload, indent=1), encoding="utf-8")

    edges = FIXTURE_DIR / "edge_cases.json"
    edges.write_text(
        json.dumps({"Success": "true", "Data": edge_case_records()}, indent=1),
        encoding="utf-8",
    )

    print(f"{sample}: {len(records)} records, {len(TICKERS)} tickers, {len(days)} days each")
    print(f"  {days[0]} .. {days[-1]}")
    print(f"{edges}: {len(edge_case_records())} records")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
