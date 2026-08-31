#!/usr/bin/env python3
"""A local stand-in for the VietinBank `GetTradingStatistics` endpoint.

Its only job is to prove the live-feed wiring without the live feed: it serves
the *documented response schema* (every numeric a string, some with thousands
separators, dates `dd/mm/yyyy`) out of the bundled fixture, so
`data.ingest.fetch_trading_statistics` can be exercised over real HTTP with
nothing but environment variables.

    uv run python scripts/mock_feed.py --port 8765 --token secret &
    export TA_AGENT_API_URL=http://127.0.0.1:8765/GetTradingStatistics
    export TA_AGENT_API_TOKEN=secret
    uv run python -m data.ingest --ticker VNM --start 2025-12-01 --end 2026-01-22

The flags exist to make the mock *wrong* in the ways a real endpoint might be,
so the ingest path can be checked against each: `--param-style alt` renames the
query parameters (then `TA_AGENT_API_PARAMS` has to remap them) and
`--envelope aspnet` wraps the payload in the double-encoded ASP.NET `{"d": ...}`
form.

This is a test fixture server. It is not hardened and must not be exposed
beyond localhost.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

DEFAULT_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "sample_daily_data.json"

#: The documented parameter names, and a deliberately different spelling.
PARAM_STYLES = {
    "documented": {"symbol": "symbol", "fromDate": "fromDate", "toDate": "toDate"},
    "alt": {"symbol": "stockCode", "fromDate": "from", "toDate": "to"},
}


def load_records(path: Path) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    records = payload["Data"] if isinstance(payload, dict) else payload
    if not isinstance(records, list):
        raise SystemExit(f"{path}: expected a list of records under 'Data'")
    return records


def record_date(record: dict) -> date | None:
    """The fixture's `dd/mm/yyyy`, as a date; None if unparseable."""
    try:
        return datetime.strptime(record.get("Date", ""), "%d/%m/%Y").date()
    except ValueError:
        return None


def select(records: list[dict], symbol: str, start: str | None, end: str | None) -> list[dict]:
    """Filter by symbol and inclusive ISO date range, oldest first."""
    lower = date.fromisoformat(start) if start else date.min
    upper = date.fromisoformat(end) if end else date.max
    hits = []
    for record in records:
        if record.get("Symbol", "").upper() != symbol.upper():
            continue
        when = record_date(record)
        if when is None or not (lower <= when <= upper):
            continue
        hits.append(record)
    hits.sort(key=lambda r: record_date(r) or date.min)
    return hits


def envelope(records: list[dict], style: str) -> dict:
    body = {"Success": True, "Message": "", "Data": records}
    if style == "aspnet":
        # ASP.NET ScriptService: the payload arrives as a JSON string inside "d".
        return {"d": json.dumps(body, ensure_ascii=False)}
    return body


class Handler(BaseHTTPRequestHandler):
    server_version = "MockGetTradingStatistics/1.0"

    # Set by make_server.
    records: list[dict] = []
    names: dict[str, str] = PARAM_STYLES["documented"]
    envelope_style = "plain"
    token: str | None = None

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's spelling
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)

        if self.token:
            if self.headers.get("Authorization") != f"Bearer {self.token}":
                self._json(401, {"Success": False, "Message": "missing or wrong bearer token"})
                return

        symbol = (query.get(self.names["symbol"]) or [""])[0]
        if not symbol:
            self._json(
                400,
                {
                    "Success": False,
                    "Message": f"expected query parameters: {', '.join(self.names.values())}",
                },
            )
            return

        try:
            rows = select(
                self.records,
                symbol,
                (query.get(self.names["fromDate"]) or [None])[0],
                (query.get(self.names["toDate"]) or [None])[0],
            )
        except ValueError as exc:  # a bad date in the request
            self._json(400, {"Success": False, "Message": str(exc)})
            return

        self._json(200, envelope(rows, self.envelope_style))

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        # stderr, one line per request, so a run is auditable without noise.
        sys.stderr.write(f"mock_feed: {fmt % args}\n")


def make_server(
    port: int,
    records: list[dict],
    *,
    param_style: str = "documented",
    envelope_style: str = "plain",
    token: str | None = None,
) -> ThreadingHTTPServer:
    handler = type(
        "BoundHandler",
        (Handler,),
        {
            "records": records,
            "names": PARAM_STYLES[param_style],
            "envelope_style": envelope_style,
            "token": token,
        },
    )
    return ThreadingHTTPServer(("127.0.0.1", port), handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--fixture", default=str(DEFAULT_FIXTURE))
    parser.add_argument("--param-style", choices=sorted(PARAM_STYLES), default="documented")
    parser.add_argument("--envelope", choices=["plain", "aspnet"], default="plain")
    parser.add_argument("--token", help="require this bearer token")
    args = parser.parse_args(argv)

    records = load_records(Path(args.fixture))
    httpd = make_server(
        args.port,
        records,
        param_style=args.param_style,
        envelope_style=args.envelope,
        token=args.token,
    )
    names = PARAM_STYLES[args.param_style]
    print(
        f"serving {len(records)} fixture records on "
        f"http://127.0.0.1:{args.port}/GetTradingStatistics"
        f"?{names['symbol']}=VNM&{names['fromDate']}=2026-01-01&{names['toDate']}=2026-01-22",
        flush=True,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
