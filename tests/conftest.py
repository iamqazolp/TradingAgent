"""Shared fixtures: small hand-built row sets and frames.

Tests are written against short, hand-calculable inputs so the expected values in
the assertions can be derived on paper. The big synthetic fixture in
`tests/fixtures/` is for the ground-truth cross-check in `scripts/validate.py`,
not for unit tests.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

from indicators.engine import rows_to_frame

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures"

#: Neutral defaults. Every field the engine requires, so a test only has to
#: supply the columns it actually cares about.
DEFAULT_ROW: dict[str, float | int] = {
    "open": 10_000.0,
    "high": 10_000.0,
    "low": 10_000.0,
    "close": 10_000.0,
    "total_trade": 100,
    "total_value": 1_000_000_000.0,
    "total_volume": 1_000,
    "buy_count": 50,
    "sell_count": 50,
    "buy_volume": 500,
    "sell_volume": 500,
    "foreign_buy_volume": 0,
    "foreign_sell_volume": 0,
    "foreign_buy_value": 0.0,
    "foreign_sell_value": 0.0,
    "foreign_room": 1_000_000,
}


def weekdays(count: int, start: str = "2026-01-05") -> list[str]:
    """`count` consecutive weekdays as ISO strings, starting at `start` (a Monday)."""
    out: list[str] = []
    day = date.fromisoformat(start)
    while len(out) < count:
        if day.weekday() < 5:
            out.append(day.isoformat())
        day += timedelta(days=1)
    return out


def build_rows(
    count: int | None = None,
    *,
    ticker: str = "TST",
    start: str = "2026-01-05",
    dates: list[str] | None = None,
    **columns: list,
) -> list[dict]:
    """Build engine-ready rows from column lists.

    Any column not supplied comes from :data:`DEFAULT_ROW`. `prev_close` defaults
    to the previous row's close (and, for the first row, to its own close), which
    is what a consistent feed would report.
    """
    lengths = {len(v) for v in columns.values()}
    if count is None:
        if len(lengths) > 1:
            raise ValueError(f"column lists differ in length: {lengths}")
        count = lengths.pop() if lengths else 0
    if lengths and max(lengths) != count:
        raise ValueError(f"count={count} does not match column lengths {lengths}")

    stamps = dates or weekdays(count, start)
    rows: list[dict] = []
    for i in range(count):
        row: dict = {"ticker": ticker, "date": stamps[i]}
        for key, default in DEFAULT_ROW.items():
            row[key] = columns[key][i] if key in columns else default
        if "prev_close" in columns:
            row["prev_close"] = columns["prev_close"][i]
        else:
            row["prev_close"] = rows[i - 1]["close"] if i else row["close"]
        if "open" not in columns:
            row["open"] = row["prev_close"]
        if "high" not in columns:
            row["high"] = max(row["close"], row["open"], row["prev_close"])
        if "low" not in columns:
            row["low"] = min(row["close"], row["open"], row["prev_close"])
        rows.append(row)
    return rows


def build_frame(**kwargs) -> pd.DataFrame:
    """`build_rows` put through the engine's frame builder."""
    return rows_to_frame(build_rows(**kwargs))


def close_series(values: list[float]) -> pd.Series:
    """A date-indexed close series, the shape the close-only groups take."""
    return pd.Series(values, index=weekdays(len(values)), dtype="float64", name="close")


def _fixture_payload(name: str):
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


@pytest.fixture
def edge_case_payload():
    """The generated edge-case fixture, envelope and all."""
    return _fixture_payload("edge_cases.json")


@pytest.fixture
def sample_payload():
    """The generated multi-ticker fixture, envelope and all."""
    return _fixture_payload("sample_daily_data.json")


@pytest.fixture(autouse=True)
def _isolate_env_from_local_dotenv(monkeypatch):
    """Ensure local .env variables do not contaminate unit tests."""
    for var in (
        "TA_AGENT_MARKET_API_URL",
        "TA_AGENT_MARKET_API_TOKEN",
        "TA_AGENT_MARKET_API_PARAMS",
        "TA_AGENT_FOREIGN_API_URL",
        "TA_AGENT_FOREIGN_API_TOKEN",
        "TA_AGENT_FOREIGN_API_PARAMS",
        "TA_AGENT_API_URL",
        "TA_AGENT_API_TOKEN",
        "TA_AGENT_API_PARAMS",
    ):
        monkeypatch.delenv(var, raising=False)

