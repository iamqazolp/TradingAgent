"""Ingestion for the VietinBank `GetTradingStatistics` feed.

Every numeric field in the raw response arrives as a *string*, sometimes with
thousands separators, sometimes blank. Nothing here relies on JSON number
parsing: each field is cast explicitly, and anything that cannot be cast is
either a rejection (for prices) or a logged warning (for reconciliation).

Rejection policy
----------------
* Missing / unparseable ``PriceClose`` -> row rejected. There is no honest
  substitute for a close price.
* Missing / unparseable ``PricePreviousClose`` -> row rejected. ``0`` is not a
  valid price and would silently poison OBV and every return-based metric.
* Missing / blank volume, count, value or foreign-room field -> cast to ``0``.
  In this feed a blank there means "nothing traded on that side", which is
  ordinary behaviour for small caps, not a data error.
* ``BuyQuantity + SellQuantity != TotalVolume`` (same for counts) -> warning
  only. The provider's totals are the source of truth; matched-trade
  classification can legitimately leave a residual.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import urllib.parse
import urllib.request
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from data import store

load_dotenv()

logger = logging.getLogger(__name__)

#: raw API field -> (column, kind). Kind drives the cast.
FIELD_MAP: dict[str, tuple[str, str]] = {
    "Symbol": ("ticker", "ticker"),
    "Date": ("date", "date"),
    "PricePreviousClose": ("prev_close", "price"),
    "PriceOpen": ("open", "price_optional"),
    "Open": ("open", "price_optional"),
    "PriceHigh": ("high", "price_optional"),
    "High": ("high", "price_optional"),
    "PriceLow": ("low", "price_optional"),
    "Low": ("low", "price_optional"),
    "PriceClose": ("close", "price"),
    "Close": ("close", "price"),
    "TotalTrade": ("total_trade", "int"),
    "TotalValue": ("total_value", "float"),
    "TotalVolume": ("total_volume", "int"),
    "BuyCount": ("buy_count", "int"),
    "SellCount": ("sell_count", "int"),
    "BuyQuantity": ("buy_volume", "int"),
    "SellQuantity": ("sell_volume", "int"),
    "ForeignerBuyQuantity": ("foreign_buy_volume", "int"),
    "ForeignerSellQuantity": ("foreign_sell_volume", "int"),
    "ForeignerBuyValue": ("foreign_buy_value", "float"),
    "ForeignerSellValue": ("foreign_sell_value", "float"),
    "CurrentForeignRoom": ("foreign_room", "int"),
}

_DATETIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y %H:%M",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%d/%m/%Y",
    "%d-%m-%Y",
    "%Y%m%d",
    "%d/%m/%y",
)


class IngestError(ValueError):
    """A single record could not be parsed into a storable row."""


@dataclass
class IngestReport:
    """Outcome of one ingestion run."""

    rows: list[dict] = field(default_factory=list)
    rejected: list[tuple[dict, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    written: int = 0

    @property
    def tickers(self) -> list[str]:
        return sorted({row["ticker"] for row in self.rows})

    def summary(self) -> str:
        return (
            f"{self.written} rows written, {len(self.rows)} parsed, "
            f"{len(self.rejected)} rejected, {len(self.warnings)} warnings, "
            f"tickers={','.join(self.tickers) or '-'}"
        )


# --------------------------------------------------------------------------- casts


def _clean(value: Any) -> str | None:
    """Normalise a raw field to a bare string, or None when it carries no value."""
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip().replace(",", "").replace(" ", "").replace(" ", "")
        if text in ("", "-", "--", "N/A", "n/a", "null", "NULL"):
            return None
        return text
    return str(value)


def cast_int(value: Any, field_name: str, *, default: int | None = None) -> int:
    text = _clean(value)
    if text is None:
        if default is None:
            raise IngestError(f"missing {field_name}")
        return default
    try:
        return int(float(text))
    except ValueError as exc:  # pragma: no cover - defensive
        raise IngestError(f"{field_name}={value!r} is not an integer") from exc


def cast_float(value: Any, field_name: str, *, default: float | None = None) -> float:
    text = _clean(value)
    if text is None:
        if default is None:
            raise IngestError(f"missing {field_name}")
        return default
    try:
        return float(text)
    except ValueError as exc:
        raise IngestError(f"{field_name}={value!r} is not a number") from exc


def cast_price(value: Any, field_name: str) -> float:
    price = cast_float(value, field_name)
    if price <= 0:
        raise IngestError(f"{field_name}={value!r} is not a positive price")
    return price


def parse_date(value: Any) -> str:
    """Normalise the feed's date/datetime to an ISO date or datetime string."""
    if isinstance(value, datetime):
        if value.hour == 0 and value.minute == 0 and value.second == 0:
            return value.strftime("%Y-%m-%d")
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    text = None if value is None else str(value).strip()
    if not text:
        raise IngestError("missing Date")
    for fmt in _DATETIME_FORMATS:
        try:
            dt = datetime.strptime(text, fmt)
            if dt.hour == 0 and dt.minute == 0 and dt.second == 0 and ("%H" not in fmt and "%M" not in fmt):
                return dt.strftime("%Y-%m-%d")
            if dt.hour != 0 or dt.minute != 0 or dt.second != 0:
                return dt.strftime("%Y-%m-%d %H:%M:%S")
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue
    raise IngestError(f"Date={value!r} is not a recognised date")


# --------------------------------------------------------------------------- parsing


def parse_record(raw: dict) -> dict:
    """Cast one raw API record into a storable row. Raises IngestError on reject."""
    row: dict[str, Any] = {}
    for raw_key, (column, kind) in FIELD_MAP.items():
        value = raw.get(raw_key)
        if value is None and column in row:
            continue
        if kind == "ticker":
            text = _clean(value)
            if not text:
                raise IngestError("missing Symbol")
            row[column] = text.upper()
        elif kind == "date":
            if value is not None:
                row[column] = parse_date(value)
        elif kind == "price":
            if value is not None:
                row[column] = cast_price(value, raw_key)
        elif kind == "price_optional":
            if value is not None and _clean(value) is not None:
                row[column] = cast_price(value, raw_key)
        elif kind == "int":
            if value is not None or column not in row:
                row[column] = cast_int(value, raw_key, default=0)
        else:
            if value is not None or column not in row:
                row[column] = cast_float(value, raw_key, default=0.0)

    if "date" not in row:
        raise IngestError("missing Date")
    if "close" not in row:
        raise IngestError("missing PriceClose")
    if "prev_close" not in row:
        raise IngestError("missing PricePreviousClose")

    # Handle open/high/low for single-source vs dual-source feeds
    had_explicit_ohlc = any(
        k in raw and _clean(raw[k]) is not None
        for k in ("PriceOpen", "Open", "PriceHigh", "High", "PriceLow", "Low")
    )
    if had_explicit_ohlc:
        if "open" not in row or row["open"] is None or row["open"] == 0:
            row["open"] = row["prev_close"]
        if "high" not in row or row["high"] is None or row["high"] == 0:
            row["high"] = max(row["close"], row["open"], row["prev_close"])
        if "low" not in row or row["low"] is None or row["low"] == 0:
            row["low"] = min(row["close"], row["open"], row["prev_close"])

        # Validate OHLC geometry
        if row["high"] < row["low"]:
            raise IngestError(f"High ({row['high']}) cannot be lower than Low ({row['low']})")
        if row["high"] < row["close"] - 1e-6 or row["high"] < row["open"] - 1e-6:
            raise IngestError(f"High ({row['high']}) cannot be lower than Open ({row['open']}) or Close ({row['close']})")
        if row["low"] > row["close"] + 1e-6 or row["low"] > row["open"] + 1e-6:
            raise IngestError(f"Low ({row['low']}) cannot be higher than Open ({row['open']}) or Close ({row['close']})")
    else:
        # Absence of explicit OHLC in flow feed (Source 1)
        row["open"] = 0.0
        row["high"] = 0.0
        row["low"] = 0.0

    return row


def reconciliation_warnings(row: dict) -> list[str]:
    """Provider totals vs the sum of the two sides. Advisory only, never fatal."""
    out: list[str] = []
    tag = f"{row['ticker']} {row['date']}"
    side_volume = row["buy_volume"] + row["sell_volume"]
    if side_volume != row["total_volume"]:
        out.append(
            f"{tag}: buy_volume+sell_volume={side_volume} != total_volume="
            f"{row['total_volume']} (residual {side_volume - row['total_volume']})"
        )
    side_count = row["buy_count"] + row["sell_count"]
    if side_count != row["total_trade"]:
        out.append(
            f"{tag}: buy_count+sell_count={side_count} != total_trade="
            f"{row['total_trade']} (residual {side_count - row['total_trade']})"
        )
    foreign_volume = row["foreign_buy_volume"] + row["foreign_sell_volume"]
    if row["total_volume"] and foreign_volume > row["total_volume"]:
        out.append(
            f"{tag}: foreign volume {foreign_volume} exceeds total_volume "
            f"{row['total_volume']}"
        )
    return out


def parse_records(raws: Iterable[dict]) -> IngestReport:
    """Parse many records, collecting rejects and warnings instead of raising."""
    report = IngestReport()
    seen: dict[tuple[str, str], int] = {}
    for raw in raws:
        try:
            row = parse_record(raw)
        except IngestError as exc:
            report.rejected.append((raw, str(exc)))
            logger.warning("rejected record: %s", exc)
            continue
        for message in reconciliation_warnings(row):
            report.warnings.append(message)
            logger.warning(message)
        key = (row["ticker"], row["date"])
        if key in seen:
            idx = seen[key]
            existing = report.rows[idx]
            merged = dict(existing)
            for k, v in row.items():
                # Prefer non-zero/non-null values when merging partial records
                if v is not None and v != 0 and v != 0.0:
                    merged[k] = v
                elif k not in merged or merged[k] is None or merged[k] == 0:
                    merged[k] = v
            report.rows[idx] = merged
            report.warnings.append(f"{key[0]} {key[1]}: merged duplicate record in payload")
        else:
            seen[key] = len(report.rows)
            report.rows.append(row)
    return report


def extract_records(payload: Any) -> list[dict]:
    """Pull the record list out of whatever envelope the response arrives in.

    Accepts a bare list, or a dict wrapping the list under any key (``data``,
    ``Data``, ``d``, ``items``, ...), at any nesting depth. A record is
    recognised by carrying a ``Symbol`` key.
    """
    if isinstance(payload, list):
        if all(isinstance(item, dict) for item in payload) and (
            not payload or any("Symbol" in item for item in payload)
        ):
            return list(payload)
        for item in payload:
            found = extract_records(item)
            if found:
                return found
        return []
    if isinstance(payload, dict):
        if "Symbol" in payload:
            return [payload]
        if isinstance(payload.get("d"), str):
            # Some ASP.NET endpoints double-encode the body as a JSON string.
            return extract_records(json.loads(payload["d"]))
        for value in payload.values():
            found = extract_records(value)
            if found:
                return found
    return []


def load_json(path: str | Path) -> list[dict]:
    """Read raw records from a saved API response on disk."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    records = extract_records(payload)
    if not records:
        raise IngestError(f"no records with a Symbol field found in {path}")
    return records


# --------------------------------------------------------------------------- live feed


def fetch_market_bars(
    ticker: str,
    start: str,
    end: str,
    *,
    timeframe: str = "1D",
    url: str | None = None,
    timeout: float = 15.0,
) -> list[dict]:
    """Fetch OHLC & order flow records from the configured market feed (Source 2)."""
    endpoint = url or os.environ.get("TA_AGENT_MARKET_API_URL") or os.environ.get("TA_AGENT_API_URL")
    if not endpoint:
        raise IngestError("no market feed endpoint configured; set TA_AGENT_MARKET_API_URL in .env")
    token = os.environ.get("TA_AGENT_MARKET_API_TOKEN") or os.environ.get("TA_AGENT_API_TOKEN")
    override = _load_param_override("TA_AGENT_MARKET_API_PARAMS") or _load_param_override("TA_AGENT_API_PARAMS")
    return _fetch_single_endpoint(
        endpoint,
        ticker=ticker,
        start=start,
        end=end,
        token=token,
        override_params=override,
        extra_query={"timeframe": timeframe} if timeframe != "1D" else None,
        timeout=timeout,
    )


def fetch_foreign_flow(
    ticker: str,
    start: str,
    end: str,
    *,
    url: str | None = None,
    timeout: float = 15.0,
) -> list[dict]:
    """Fetch foreign buy/sell flow and foreign room records from the foreign feed (Source 1)."""
    endpoint = url or os.environ.get("TA_AGENT_FOREIGN_API_URL") or os.environ.get("TA_AGENT_API_URL")
    if not endpoint:
        raise IngestError("no foreign feed endpoint configured; set TA_AGENT_FOREIGN_API_URL in .env")
    token = os.environ.get("TA_AGENT_FOREIGN_API_TOKEN") or os.environ.get("TA_AGENT_API_TOKEN")
    override = _load_param_override("TA_AGENT_FOREIGN_API_PARAMS") or _load_param_override("TA_AGENT_API_PARAMS")
    return _fetch_single_endpoint(
        endpoint,
        ticker=ticker,
        start=start,
        end=end,
        token=token,
        override_params=override,
        timeout=timeout,
    )


def fetch_trading_statistics(
    ticker: str,
    start: str,
    end: str,
    *,
    url: str | None = None,
    timeout: float = 15.0,
    source: str = "auto",
) -> list[dict]:
    """Fetch raw records from the live feed(s).

    Supports single-endpoint mode (``TA_AGENT_API_URL``) or dual-source mode:
    - Market OHLC + Order Flow (Source 2): ``TA_AGENT_MARKET_API_URL``
    - Foreign Flow & Room (Source 1): ``TA_AGENT_FOREIGN_API_URL``

    `source` can be 'auto', 'market', 'foreign', or 'all'.
    """
    market_url = os.environ.get("TA_AGENT_MARKET_API_URL")
    foreign_url = os.environ.get("TA_AGENT_FOREIGN_API_URL")
    single_url = url or os.environ.get("TA_AGENT_API_URL")

    # Determine which URLs to fetch
    targets: list[tuple[str, str | None, dict | None]] = []

    if url:
        targets.append((url, os.environ.get("TA_AGENT_API_TOKEN"), _load_param_override("TA_AGENT_API_PARAMS")))
    elif source == "market" and market_url:
        targets.append((market_url, os.environ.get("TA_AGENT_MARKET_API_TOKEN"), _load_param_override("TA_AGENT_MARKET_API_PARAMS")))
    elif source == "foreign" and foreign_url:
        targets.append((foreign_url, os.environ.get("TA_AGENT_FOREIGN_API_TOKEN"), _load_param_override("TA_AGENT_FOREIGN_API_PARAMS")))
    elif source == "all" and market_url and foreign_url:
        targets.append((market_url, os.environ.get("TA_AGENT_MARKET_API_TOKEN"), _load_param_override("TA_AGENT_MARKET_API_PARAMS")))
        targets.append((foreign_url, os.environ.get("TA_AGENT_FOREIGN_API_TOKEN"), _load_param_override("TA_AGENT_FOREIGN_API_PARAMS")))
    elif single_url:
        targets.append((single_url, os.environ.get("TA_AGENT_API_TOKEN"), _load_param_override("TA_AGENT_API_PARAMS")))
    elif market_url and foreign_url:
        targets.append((market_url, os.environ.get("TA_AGENT_MARKET_API_TOKEN"), _load_param_override("TA_AGENT_MARKET_API_PARAMS")))
        targets.append((foreign_url, os.environ.get("TA_AGENT_FOREIGN_API_TOKEN"), _load_param_override("TA_AGENT_FOREIGN_API_PARAMS")))
    else:
        raise IngestError(
            "no live endpoint configured; set TA_AGENT_API_URL (or TA_AGENT_MARKET_API_URL "
            "and TA_AGENT_FOREIGN_API_URL) or ingest a saved response with --file"
        )

    all_records: list[dict] = []
    for endpoint_url, token, override_params in targets:
        records = _fetch_single_endpoint(
            endpoint_url,
            ticker=ticker,
            start=start,
            end=end,
            token=token,
            override_params=override_params,
            timeout=timeout,
        )
        all_records.extend(records)

    return all_records


def _load_param_override(env_var: str) -> dict | None:
    val = os.environ.get(env_var)
    if val:
        try:
            return json.loads(val)
        except Exception:
            return None
    return None


def _fetch_single_endpoint(
    url: str,
    *,
    ticker: str,
    start: str,
    end: str,
    token: str | None = None,
    override_params: dict | None = None,
    extra_query: dict | None = None,
    timeout: float = 15.0,
) -> list[dict]:
    names = {"symbol": "symbol", "fromDate": "fromDate", "toDate": "toDate"}
    if override_params:
        names.update(override_params)
    params = {names["symbol"]: ticker.upper(), names["fromDate"]: start, names["toDate"]: end}
    if extra_query:
        params.update(extra_query)
    query = urllib.parse.urlencode(params)
    full_url = f"{url}{'&' if '?' in url else '?'}{query}"
    request = urllib.request.Request(full_url, headers={"Accept": "application/json"})
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    logger.info("fetching %s %s..%s from %s", ticker, start, end, url)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()[:500]
        hint = ""
        if exc.code in (401, 403):
            hint = "; check TA_AGENT_API_TOKEN"
        elif exc.code == 400:
            hint = "; check the query parameter names (TA_AGENT_API_PARAMS)"
        raise IngestError(
            f"feed returned HTTP {exc.code} {exc.reason} for {url}{hint}"
            + (f"\n  response: {detail}" if detail else "")
        ) from exc
    except urllib.error.URLError as exc:
        raise IngestError(f"cannot reach {url}: {exc.reason}") from exc
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise IngestError(
            f"feed response from {url} is not JSON ({exc}); first 200 chars: {body[:200]!r}"
        ) from exc
    return extract_records(payload)


# --------------------------------------------------------------------------- ingest


def ingest_records(
    raws: Sequence[dict], conn: sqlite3.Connection | None = None, *, db_path: str | Path | None = None
) -> IngestReport:
    """Parse and upsert raw records. Re-running with the same input is a no-op."""
    owns_conn = conn is None
    conn = conn or store.connect(db_path)
    try:
        report = parse_records(raws)
        report.written = store.upsert_rows(conn, report.rows)
    finally:
        if owns_conn:
            conn.close()
    logger.info("ingest: %s", report.summary())
    return report


def ingest_file(
    path: str | Path,
    conn: sqlite3.Connection | None = None,
    *,
    db_path: str | Path | None = None,
) -> IngestReport:
    """Ingest a saved API response file."""
    return ingest_records(load_json(path), conn, db_path=db_path)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ingest daily trading statistics.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--file", help="saved API response (JSON) to ingest")
    source.add_argument("--ticker", help="fetch this ticker from the live feed")
    parser.add_argument("--start", help="from date, YYYY-MM-DD (live fetch)")
    parser.add_argument("--end", help="to date, YYYY-MM-DD (live fetch)")
    parser.add_argument(
        "--source",
        choices=["auto", "market", "foreign", "all"],
        default="auto",
        help="data source to fetch (default: auto)",
    )
    parser.add_argument("--db", help="SQLite path (default: TA_AGENT_DB or var/ta.sqlite)")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        if args.file:
            raws = load_json(args.file)
        else:
            if not (args.start and args.end):
                parser.error("--ticker requires --start and --end")
            raws = fetch_trading_statistics(args.ticker, args.start, args.end, source=args.source)
    except IngestError as exc:
        print(f"ingest failed: {exc}", file=sys.stderr)
        return 1
    report = ingest_records(raws, db_path=args.db)
    print(report.summary())
    for raw, reason in report.rejected[:20]:
        print(f"  rejected {raw.get('Symbol')} {raw.get('Date')}: {reason}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
