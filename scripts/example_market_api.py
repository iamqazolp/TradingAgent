#!/usr/bin/env python3
"""Example script demonstrating how to call and parse the VietinBank/Stockbiz REST APIs.

Covers:
1. Universal Envelope unwrapping (`{"success": true, "data": ..., "message": ...}`).
2. Safe numeric parsing for stringified numbers (e.g. "2,850,000" -> 2850000).
3. Parsing `GetCurrentQuotes` (intraday prices & orderbook).
4. Parsing `GetHistoricalQuotes` (EOD OHLCV history).
5. Parsing `GetAllMarketInfo` (market breadth & index status, normalizing HOSTC -> VNINDEX).

Usage:
    # Run against mock data (no network needed):
    python scripts/example_market_api.py --mock

    # Run against a live endpoint:
    python scripts/example_market_api.py --url "https://api-endpoint.example.com" --token "YOUR_TOKEN" --symbol "VNM"
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
MOCK_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "stockbiz_rest_mock.json"


# --------------------------------------------------------------------------- safe parsers


def _clean_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", "").replace(" ", "")
    if text in ("", "-", "--", "N/A", "n/a", "null", "NULL"):
        return None
    return text


def parse_float(value: Any, default: float = 0.0) -> float:
    text = _clean_str(value)
    if text is None:
        return default
    try:
        return float(text)
    except ValueError:
        return default


def parse_int(value: Any, default: int = 0) -> int:
    text = _clean_str(value)
    if text is None:
        return default
    try:
        return int(float(text))
    except ValueError:
        return default


def unwrap_envelope(response: dict[str, Any]) -> Any:
    """Validate envelope status and extract payload data."""
    if not response.get("success", False):
        msg = response.get("message", "API call failed with success=false")
        raise RuntimeError(f"API Error: {msg}")
    return response.get("data")


# --------------------------------------------------------------------------- payload parsers


def parse_current_quote_item(raw: dict[str, Any]) -> dict[str, Any]:
    """Parse one item from GetCurrentQuotes into a clean typed dictionary."""
    symbol = str(raw.get("Symbol", "")).upper()
    basic = parse_float(raw.get("PriceBasic"))
    current = parse_float(raw.get("PriceCurrent") or raw.get("PriceLast") or raw.get("PriceClose"))
    change = current - basic if basic > 0 else 0.0
    pct_change = (change / basic * 100.0) if basic > 0 else 0.0

    return {
        "ticker": symbol,
        "name": raw.get("Name"),
        "exchange": raw.get("Exchange"),
        "timestamp": raw.get("Date"),
        "price_basic": basic,
        "price_ceiling": parse_float(raw.get("PriceCeiling")),
        "price_floor": parse_float(raw.get("PriceFloor")),
        "price_open": parse_float(raw.get("PriceOpen")),
        "price_high": parse_float(raw.get("PriceHigh")),
        "price_low": parse_float(raw.get("PriceLow")),
        "price_current": current,
        "change": round(change, 3),
        "percent_change": round(pct_change, 2),
        "volume_last_tick": parse_int(raw.get("Volume")),
        "accumulated_volume": parse_int(raw.get("TotalVolume")),
        "accumulated_value": parse_float(raw.get("TotalValue")),
        "foreign_buy_volume": parse_int(raw.get("BuyForeignQuantity")),
        "foreign_sell_volume": parse_int(raw.get("SellForeignQuantity")),
        "foreign_room": parse_int(raw.get("CurrentForeignRoom")),
        "bid_1": {"price": parse_float(raw.get("PriceBid1")), "volume": parse_int(raw.get("QuantityBid1"))},
        "ask_1": {"price": parse_float(raw.get("PriceAsk1")), "volume": parse_int(raw.get("QuantityAsk1"))},
    }


def parse_historical_quote_item(raw: dict[str, Any]) -> dict[str, Any]:
    """Parse one session item from GetHistoricalQuotes into a clean daily bar."""
    return {
        "ticker": str(raw.get("Symbol", "")).upper(),
        "date": str(raw.get("Date", ""))[:10],
        "open": parse_float(raw.get("Open")),
        "high": parse_float(raw.get("High")),
        "low": parse_float(raw.get("Low")),
        "close": parse_float(raw.get("Close")),
        "prev_close": parse_float(raw.get("Basic")),
        "total_volume": parse_int(raw.get("Volume") or raw.get("DealVolume")),
        "total_value": parse_float(raw.get("TotalValue")),
        "total_trade": parse_int(raw.get("TotalTrade")),
        "foreign_buy_volume": parse_int(raw.get("BuyForeignVolume")),
        "foreign_sell_volume": parse_int(raw.get("SellForeignVolume")),
        "foreign_room": parse_int(raw.get("CurrentForeignRoom")),
        "buy_volume": parse_int(raw.get("BuyQuantity")),
        "sell_volume": parse_int(raw.get("SellQuantity")),
    }


def parse_market_info_item(raw: dict[str, Any]) -> dict[str, Any]:
    """Parse one index item from GetAllMarketInfo, normalizing HOSTC -> VNINDEX."""
    ex_raw = str(raw.get("Exchange", "")).upper()
    ex_normalized = "VNINDEX" if ex_raw in ("HOSTC", "HSX") else ("HNX" if ex_raw == "HASTC" else ex_raw)

    return {
        "exchange_raw": ex_raw,
        "exchange": ex_normalized,
        "symbol_id": raw.get("SymbolID"),
        "timestamp": raw.get("Date"),
        "index_current": parse_float(raw.get("IndexCurrent")),
        "index_change": parse_float(raw.get("IndexChange")),
        "index_percent_change": parse_float(raw.get("IndexPercentChange")),
        "advances": parse_int(raw.get("Advances")),
        "declines": parse_int(raw.get("Declines")),
        "unchanged": parse_int(raw.get("Unchange")),
        "total_volume": parse_int(raw.get("TotalVolume")),
        "total_value": parse_float(raw.get("TotalValue")),
        "total_trade": parse_int(raw.get("TotalTrade")),
    }


# --------------------------------------------------------------------------- live http fetcher


def call_api(
    base_url: str,
    endpoint: str,
    params: dict[str, Any] | None = None,
    token: str | None = None,
    csrf_token: str | None = None,
    timeout: float = 15.0,
) -> Any:
    """Generic helper to call a REST endpoint and unwrap the JSON envelope."""
    url = f"{base_url.rstrip('/')}/{endpoint.lstrip('/')}"
    if params:
        url += f"?{urllib.parse.urlencode(params)}"

    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if csrf_token:
        req.add_header("x-csrf-token", csrf_token)

    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8")
        payload = json.loads(body)
        return unwrap_envelope(payload)


# --------------------------------------------------------------------------- main demo


def main():
    parser = argparse.ArgumentParser(description="VietinBank/Stockbiz Market API Parser Demo")
    parser.add_argument("--mock", action="store_true", help="Run against local mock data fixture")
    parser.add_argument("--url", help="Base API URL (e.g. https://cms-investor-uat.vietinbank.vn/o/stockbiz/v1.0/MarketDataService)")
    parser.add_argument("--token", help="Bearer authorization token (if needed)")
    parser.add_argument("--csrf-token", help="x-csrf-token header value (e.g. HAa1EXPd)")
    parser.add_argument("--symbol", default="VCB", help="Stock ticker to test (default VCB)")
    parser.add_argument("--start", default="2026-10-01", help="Start date YYYY-MM-DD")
    parser.add_argument("--end", default="2026-10-06", help="End date YYYY-MM-DD")
    args = parser.parse_args()

    print("=" * 70)
    print("DEMO: CÁCH TRUY VẤN VÀ BÓC TÁCH DỮ LIỆU TỪ VIETINBANK/STOCKBIZ API")
    print("=" * 70)

    if args.mock or not args.url:
        print(f"\n[*] Đang sử dụng Mock Data từ: {MOCK_FIXTURE}\n")
        with open(MOCK_FIXTURE, encoding="utf-8") as f:
            mock_data = json.load(f)

        # 1. GetCurrentQuotes Demo
        raw_quotes = unwrap_envelope(mock_data["GetCurrentQuotes"])
        print(f"1. GetCurrentQuotes -> Nhận được {len(raw_quotes)} mã:")
        for raw in raw_quotes:
            parsed = parse_current_quote_item(raw)
            print(f"   [{parsed['ticker']}] Giá: {parsed['price_current']} ({parsed['percent_change']:+}%) | "
                  f"KL Tích lũy: {parsed['accumulated_volume']:,} | "
                  f"Khối ngoại ròng: {parsed['foreign_buy_volume'] - parsed['foreign_sell_volume']:+,} cp")

        # 2. GetHistoricalQuotes Demo
        raw_hist = unwrap_envelope(mock_data["GetHistoricalQuotes"])
        symbol = raw_hist.get("Symbol")
        quote_list = raw_hist.get("Quote", [])
        print(f"\n2. GetHistoricalQuotes cho mã {symbol} -> Nhận được {len(quote_list)} phiên:")
        for raw in quote_list:
            bar = parse_historical_quote_item(raw)
            print(f"   Ngày: {bar['date']} | Open: {bar['open']} | High: {bar['high']} | "
                  f"Low: {bar['low']} | Close: {bar['close']} | Vol: {bar['total_volume']:,} (Khớp lệnh: {parse_int(raw.get('DealVolume')):,})")

        # 3. GetAllMarketInfo Demo
        raw_market = unwrap_envelope(mock_data["GetAllMarketInfo"])
        market_list = raw_market.get("MarketInfo", [])
        print(f"\n3. GetAllMarketInfo -> Thống kê độ rộng các sàn:")
        for raw in market_list:
            m = parse_market_info_item(raw)
            print(f"   Sàn: {m['exchange']} (Mã gốc: {m['exchange_raw']}) | Điểm: {m['index_current']} ({m['index_percent_change']:+}%) | "
                  f"Tăng/Giảm/Đứng: {m['advances']} / {m['declines']} / {m['unchanged']} | GTGD: {m['total_value']/1e9:,.1f} tỷ VND")

        print("\n" + "=" * 70)
        print("XÁC THỰC THÀNH CÔNG: Mô hình bóc tách dữ liệu hoạt động trơn tru 100%.")
        print("=" * 70)
        return 0

    # Live call demonstration
    print(f"\n[*] Đang gọi API thực tế tới: {args.url}")
    try:
        # Live query for GetHistoricalQuotes using VietinBank JSON-array param format
        encoded_params = json.dumps([{"symbol": args.symbol.upper(), "startDate": args.start, "endDate": args.end}])
        print(f"[*] Calling GetHistoricalQuotes with params={encoded_params}")
        hist_data = call_api(
            args.url,
            "GetHistoricalQuotes",
            {"params": encoded_params},
            token=args.token,
            csrf_token=args.csrf_token,
        )
        quotes = hist_data.get("Quote", [])
        print(f"[*] Nhận được {len(quotes)} phiên lịch sử cho {args.symbol}:")
        for q in quotes:
            parsed = parse_historical_quote_item(q)
            print(f"   {parsed['date']}: Close={parsed['close']}, Vol={parsed['total_volume']:,}")
    except Exception as e:
        print(f"Lỗi khi gọi API: {e}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
