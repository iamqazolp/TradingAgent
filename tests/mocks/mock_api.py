"""HTTP Mock API Server exposing simulated Vietnamese market and foreign data feeds."""

from __future__ import annotations

import argparse
import json
import urllib.parse
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from tests.mocks.generators import generate_foreign_flow, generate_market_bars


class MockApiHandler(BaseHTTPRequestHandler):
    server_version = "MockDataServer/1.0"
    token: str | None = None
    envelope_style: str = "plain"

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        # Health check endpoint
        if path in ("/", "/health", "/ping"):
            self._send_json({"status": "ok", "endpoints": ["/api/foreign_flow", "/api/market_bars"]})
            return

        # Check authentication token if configured
        if self.token:
            auth = self.headers.get("Authorization", "")
            if auth != f"Bearer {self.token}":
                self._send_error(401, "invalid or missing bearer token; check TA_AGENT_API_TOKEN")
                return

        # Parameter extraction (support various parameter aliases)
        symbol = (
            query.get("symbol", [None])[0]
            or query.get("stockCode", [None])[0]
            or query.get("ticker", ["VNM"])[0]
        )
        start = (
            query.get("fromDate", [None])[0]
            or query.get("from", [None])[0]
            or query.get("start", [None])[0]
            or (date.today() - timedelta(days=30)).isoformat()
        )
        end = (
            query.get("toDate", [None])[0]
            or query.get("to", [None])[0]
            or query.get("end", [None])[0]
            or date.today().isoformat()
        )
        timeframe = (
            query.get("timeframe", [None])[0]
            or query.get("resolution", [None])[0]
            or query.get("tf", ["1D"])[0]
        )

        if path in ("/api/foreign_flow", "/GetTradingStatistics"):
            records = generate_foreign_flow(symbol, start, end)
            self._send_data(records)
        elif path in ("/api/market_bars", "/GetHistoricalBars"):
            records = generate_market_bars(symbol, start, end, timeframe=timeframe)
            self._send_data(records)
        else:
            self._send_error(404, f"endpoint '{path}' not found")

    def _send_data(self, records: list[dict]) -> None:
        body: Any = {"Success": True, "Message": "", "Data": records}
        if self.envelope_style == "aspnet":
            body = {"d": json.dumps(body, ensure_ascii=False)}
        self._send_json(body)

    def _send_json(self, payload: Any, status: int = 200) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _send_error(self, status: int, message: str) -> None:
        self._send_json({"Success": False, "Message": message, "Data": []}, status=status)

    def log_message(self, fmt: str, *args: Any) -> None:
        pass  # Suppress default stdio log noise in tests


def make_server(
    port: int = 8765, token: str | None = None, envelope: str = "plain"
) -> ThreadingHTTPServer:
    class ConfiguredHandler(MockApiHandler):
        pass

    ConfiguredHandler.token = token
    ConfiguredHandler.envelope_style = envelope
    return ThreadingHTTPServer(("127.0.0.1", port), ConfiguredHandler)


def main() -> int:
    parser = argparse.ArgumentParser(description="Mock stock data server.")
    parser.add_argument("--port", type=int, default=8765, help="HTTP port (default: 8765)")
    parser.add_argument("--token", help="optional Bearer token")
    parser.add_argument(
        "--envelope", choices=["plain", "aspnet"], default="plain", help="payload envelope"
    )
    args = parser.parse_args()

    server = make_server(port=args.port, token=args.token, envelope=args.envelope)
    print(f"Mock Data Server running on http://127.0.0.1:{args.port}")
    print(f"  • Source 1 (Foreign Flow): http://127.0.0.1:{args.port}/api/foreign_flow")
    print(f"  • Source 2 (Market OHLC):  http://127.0.0.1:{args.port}/api/market_bars")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server.")
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
