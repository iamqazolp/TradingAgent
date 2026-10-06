"""Data ingestion module for daily trading statistics.

DEPRECATION NOTICE:
Legacy VietinBank-specific endpoints ('https://e-trading.vietinbank.vn/...') and
its ASP.NET envelope format are deprecated. Use the abstract `MarketDataProvider`
protocol defined in `data.provider` for new API adapters.

Historical JSON responses and Stockbiz feeds remain supported for backward compatibility.
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
import warnings
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from data import store
from data.provider import DailyRecord
from data.stockbiz import StockbizClient

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

_DATE_FORMATS = (
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
    except (ValueError, OverflowError) as exc:  # pragma: no cover - defensive
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


def cast_price_optional(value: Any, field_name: str) -> float | None:
    """Cast an optional price field. Returns None if missing or blank; raises on non-positive number."""
    text = _clean(value)
    if text is None:
        return None
    price = cast_float(value, field_name)
    if price <= 0:
        raise IngestError(f"{field_name}={value!r} is not a positive price")
    return price


def parse_date(value: Any) -> str:
    """Normalise the feed's date to an ISO YYYY-MM-DD string."""
    if isinstance(value, (date, datetime)):
        return value.strftime("%Y-%m-%d")
    text = None if value is None else str(value).strip()
    if not text:
        raise IngestError("missing Date")
    text = text.split("T")[0].split(" ")[0]
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    raise IngestError(f"Date={value!r} is not a recognised date")


# --------------------------------------------------------------------------- parsing


def parse_record(raw: dict) -> dict:
    """Cast one raw API record into a storable row. Raises IngestError on reject."""
    row: dict[str, Any] = {}
    for raw_key, (column, kind) in FIELD_MAP.items():
        value = raw.get(raw_key)
        if kind == "ticker":
            text = _clean(value)
            if not text:
                raise IngestError("missing Symbol")
            row[column] = text.upper()
        elif kind == "date":
            row[column] = parse_date(value)
        elif kind == "price":
            row[column] = cast_price(value, raw_key)
        elif kind == "price_optional":
            if column in row and row[column] is not None:
                continue
            row[column] = cast_price_optional(value, raw_key)
        elif kind == "int":
            row[column] = cast_int(value, raw_key, default=0)
        else:
            row[column] = cast_float(value, raw_key, default=0.0)
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
            report.rows[seen[key]] = row
            report.warnings.append(f"{key[0]} {key[1]}: duplicate record in payload")
        else:
            seen[key] = len(report.rows)
            report.rows.append(row)
    return report


def extract_records(payload: Any) -> list[dict]:
    """Pull the record list out of whatever envelope the response arrives in."""
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


# --------------------------------------------------------------------------- live feeds


def fetch_stockbiz_statistics(
    client: StockbizClient,
    symbol: str,
    start: str,
    end: str,
) -> list[dict]:
    """Fetch raw trading statistics from Stockbiz SOAP service."""
    return client.get_trading_statistics(symbol, start, end)


def fetch_trading_statistics(
    ticker: str,
    start: str,
    end: str,
    *,
    url: str | None = None,
    timeout: float = 30.0,
) -> list[dict]:
    """Fetch raw records from the live feed.

    .. deprecated::
        Direct HTTP fetching via TA_AGENT_API_URL is deprecated. Use a
        `MarketDataProvider` implementation instead.
    """
    warnings.warn(
        "fetch_trading_statistics using legacy URL is deprecated; use MarketDataProvider",
        DeprecationWarning,
        stacklevel=2,
    )
    url = url or os.environ.get("TA_AGENT_API_URL")
    if not url:
        raise IngestError(
            "no live endpoint configured; set TA_AGENT_API_URL or ingest a saved "
            "response with --file"
        )
    names = {"symbol": "symbol", "fromDate": "fromDate", "toDate": "toDate"}
    override = os.environ.get("TA_AGENT_API_PARAMS")
    if override:
        names.update(json.loads(override))
    query = urllib.parse.urlencode(
        {names["symbol"]: ticker.upper(), names["fromDate"]: start, names["toDate"]: end}
    )
    full_url = f"{url}{'&' if '?' in url else '?'}{query}"
    request = urllib.request.Request(full_url, headers={"Accept": "application/json"})
    token = os.environ.get("TA_AGENT_API_TOKEN")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    logger.info("fetching %s %s..%s", ticker, start, end)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
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
    raws: Sequence[dict | DailyRecord],
    conn: sqlite3.Connection | None = None,
    *,
    db_path: str | Path | None = None,
) -> IngestReport:
    """Parse and upsert raw records or DailyRecord domain objects. Re-running with the same input is a no-op."""
    owns_conn = conn is None
    conn = conn or store.connect(db_path)
    try:
        normalized_raws: list[dict] = []
        for r in raws:
            if isinstance(r, DailyRecord):
                normalized_raws.append(r.to_dict())
            else:
                normalized_raws.append(r)
        report = parse_records(normalized_raws)
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
            raws = fetch_trading_statistics(args.ticker, args.start, args.end)
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
