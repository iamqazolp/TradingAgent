"""Stockbiz SOAP client for Vietnamese equities and market indices data feed.

Communicates with Stockbiz MarketDataService.asmx using SOAP 1.1 protocol.
Provides methods for trading statistics, historical quotes, market indices,
and intraday quotes.
"""

from __future__ import annotations

import logging
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime
from typing import Any
from xml.sax.saxutils import escape

logger = logging.getLogger(__name__)

DEFAULT_STOCKBIZ_URL = "http://datafeed.stockbiz.vn/MarketDataService.asmx"
SOAP_NAMESPACE = "http://datafeed.stockbiz.vn/"


class StockbizError(Exception):
    """Base exception for Stockbiz SOAP service errors."""


def _format_datetime(val: str | date | datetime) -> str:
    """Format input date/time to ISO YYYY-MM-DDTHH:MM:SS format."""
    if isinstance(val, datetime):
        return val.strftime("%Y-%m-%dT%H:%M:%S")
    if isinstance(val, date):
        return val.strftime("%Y-%m-%dT00:00:00")
    text = str(val).strip()
    if not text:
        return ""
    if "T" in text:
        parts = text.split("T")
        d_part = parts[0]
        t_part = parts[1] if len(parts) > 1 and parts[1] else "00:00:00"
        if len(t_part) == 5:
            t_part += ":00"
        return f"{d_part}T{t_part}"
    if " " in text:
        parts = text.split(" ")
        d_part = parts[0]
        t_part = parts[1] if len(parts) > 1 and parts[1] else "00:00:00"
        if len(t_part) == 5:
            t_part += ":00"
        return f"{d_part}T{t_part}"
    if len(text) == 10 and text[4] in ("-", "/") and text[7] in ("-", "/"):
        normalized = text.replace("/", "-")
        return f"{normalized}T00:00:00"
    if "/" in text:
        parts = text.split("/")
        if len(parts) == 3 and len(parts[0]) <= 2:
            return f"{parts[2]}-{int(parts[1]):02d}-{int(parts[0]):02d}T00:00:00"
    return f"{text}T00:00:00"


def parse_response(xml_text: str, item_tag: str) -> list[dict[str, str]]:
    """Parse SOAP response XML into a list of dicts using xml.etree.ElementTree."""
    if not xml_text or not xml_text.strip():
        return []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise StockbizError(f"Malformed XML response: {exc}") from exc

    # Check for SOAP Fault
    for elem in root.iter():
        local_tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
        if local_tag == "Fault":
            faultcode = ""
            faultstring = ""
            for child in elem:
                c_tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                if c_tag == "faultcode":
                    faultcode = (child.text or "").strip()
                elif c_tag == "faultstring":
                    faultstring = (child.text or "").strip()
            msg = faultstring or faultcode or "SOAP Fault occurred"
            raise StockbizError(f"SOAP Fault: {msg}")

    records: list[dict[str, str]] = []
    for elem in root.iter():
        local_tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
        if local_tag == item_tag:
            record: dict[str, str] = {}
            for child in elem:
                c_tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                record[c_tag] = child.text.strip() if child.text else ""
            records.append(record)
    return records


class StockbizClient:
    """Client for Stockbiz MarketDataService SOAP web service."""

    def __init__(
        self,
        username: str,
        password: str,
        url: str = DEFAULT_STOCKBIZ_URL,
        timeout: float = 30.0,
    ) -> None:
        self.username = username
        self.password = password
        self.url = url
        self.timeout = timeout

    def build_soap_envelope(self, method: str, inner_xml: str) -> str:
        """Wrap method call and parameters in SOAP 1.1 envelope."""
        creds = ""
        if "<username>" not in inner_xml:
            creds = (
                f"<username>{escape(self.username)}</username>"
                f"<password>{escape(self.password)}</password>"
            )
        return (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<soap:Envelope xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
            'xmlns:xsd="http://www.w3.org/2001/XMLSchema" '
            'xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
            "<soap:Body>"
            f'<{method} xmlns="{SOAP_NAMESPACE}">'
            f"{creds}"
            f"{inner_xml}"
            f"</{method}>"
            "</soap:Body>"
            "</soap:Envelope>"
        )

    def parse_response(self, xml_text: str, item_tag: str) -> list[dict[str, str]]:
        """Parse SOAP response XML into a list of dicts."""
        return parse_response(xml_text, item_tag)

    def _call(self, method: str, inner_xml: str, item_tag: str) -> list[dict[str, str]]:
        """Send SOAP POST request and return parsed records."""
        envelope = self.build_soap_envelope(method, inner_xml)
        headers = {
            "Content-Type": "text/xml; charset=utf-8",
            "SOAPAction": f'"{SOAP_NAMESPACE}{method}"',
        }
        req = urllib.request.Request(
            self.url,
            data=envelope.encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode("utf-8", errors="replace").strip()
            try:
                return self.parse_response(err_body, item_tag)
            except StockbizError:
                raise
            except Exception:
                raise StockbizError(f"HTTP {exc.code} {exc.reason}: {err_body[:200]}") from exc
        except urllib.error.URLError as exc:
            raise StockbizError(f"Network error connecting to {self.url}: {exc.reason}") from exc
        except Exception as exc:
            raise StockbizError(f"Request failed for {method}: {exc}") from exc

        return self.parse_response(body, item_tag)

    def get_trading_statistics(self, symbol: str, start: str, end: str) -> list[dict[str, str]]:
        """Call GetTradingStatistics for a symbol between start and end dates."""
        start_fmt = _format_datetime(start)
        end_fmt = _format_datetime(end)
        inner = (
            f"<symbol>{escape(symbol.upper())}</symbol>"
            f"<startDate>{escape(start_fmt)}</startDate>"
            f"<endDate>{escape(end_fmt)}</endDate>"
        )
        return self._call("GetTradingStatistics", inner, "TradingStatistics")

    def get_latest_trading_statistics(self, symbol: str) -> list[dict[str, str]]:
        """Call GetLastestTradingStatistics for a symbol."""
        inner = f"<symbol>{escape(symbol.upper())}</symbol>"
        return self._call("GetLastestTradingStatistics", inner, "TradingStatistics")

    def get_historical_quotes(self, symbol: str, start: str, end: str) -> list[dict[str, str]]:
        """Call GetHistoricalQuotes for a symbol between start and end dates."""
        start_fmt = _format_datetime(start)
        end_fmt = _format_datetime(end)
        inner = (
            f"<symbol>{escape(symbol.upper())}</symbol>"
            f"<startDate>{escape(start_fmt)}</startDate>"
            f"<endDate>{escape(end_fmt)}</endDate>"
        )
        return self._call("GetHistoricalQuotes", inner, "HistoricalQuote")

    def get_all_market_info(self) -> list[dict[str, str]]:
        """Call GetAllMarketInfo to retrieve indices across all exchanges."""
        return self._call("GetAllMarketInfo", "", "MarketInfo")

    def get_market_info(self, market: str) -> list[dict[str, str]]:
        """Call GetMarketInfo for a specific exchange/market (e.g. HOSE, HNX, UPCOM)."""
        inner = f"<market>{escape(market.upper())}</market>"
        return self._call("GetMarketInfo", inner, "MarketInfo")

    def get_exchange_internals(self, market: str) -> list[dict[str, str]]:
        """Call GetExchangeInternals for a specific market."""
        inner = f"<market>{escape(market.upper())}</market>"
        return self._call("GetExchangeInternals", inner, "ExchangeInternal")

    def get_current_quotes(self, symbols: list[str]) -> list[dict[str, str]]:
        """Call GetCurrentQuotes for a list of symbols."""
        sym_str = ",".join(s.upper() for s in symbols)
        inner = f"<symbols>{escape(sym_str)}</symbols>"
        return self._call("GetCurrentQuotes", inner, "Quote")

    def get_intraday_quotes(self, symbol: str, date_str: str) -> list[dict[str, str]]:
        """Call GetIntradayQuotes for a symbol and date."""
        date_fmt = _format_datetime(date_str)
        inner = (
            f"<symbol>{escape(symbol.upper())}</symbol>"
            f"<date>{escape(date_fmt)}</date>"
        )
        return self._call("GetIntradayQuotes", inner, "IntradayQuote")
