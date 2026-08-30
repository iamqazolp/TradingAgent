"""SQLite store for trading statistics (hourly and daily).

Single table, `prices`, keyed on (ticker, date). Writes are upserts so
re-ingesting the same day is idempotent. Reads return plain dicts in ascending
date order, with optional timeframe resampling (1H, 4H, 1D, 3D, 1W, 1M, 1Y).
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterable, Sequence
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

from data.resample import resample_bars

load_dotenv()

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"

#: Column order used for inserts and for the dicts returned by queries.
COLUMNS: tuple[str, ...] = (
    "ticker",
    "date",
    "prev_close",
    "open",
    "high",
    "low",
    "close",
    "total_trade",
    "total_value",
    "total_volume",
    "buy_count",
    "sell_count",
    "buy_volume",
    "sell_volume",
    "foreign_buy_volume",
    "foreign_sell_volume",
    "foreign_buy_value",
    "foreign_sell_value",
    "foreign_room",
)

_UPSERT_SQL = """
INSERT INTO prices ({cols})
VALUES ({placeholders})
ON CONFLICT(ticker, date) DO UPDATE SET {updates}
""".format(
    cols=", ".join(COLUMNS),
    placeholders=", ".join(f":{c}" for c in COLUMNS),
    updates=", ".join(f"{c}=excluded.{c}" for c in COLUMNS if c not in ("ticker", "date")),
)


def default_db_path() -> Path:
    """Database location, overridable with the TA_AGENT_DB environment variable."""
    env = os.environ.get("TA_AGENT_DB")
    if env:
        return Path(env).expanduser()
    return REPO_ROOT / "var" / "ta.sqlite"


def connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    """Open a connection with the schema applied and dict-style row access."""
    if db_path is None:
        db_path = default_db_path()
    db_path = Path(db_path)
    if str(db_path) != ":memory:":
        db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    init_db(conn)
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Apply schema.sql. Idempotent."""
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.commit()


def upsert_rows(conn: sqlite3.Connection, rows: Iterable[dict]) -> int:
    """Insert or update rows. Returns the number of rows written."""
    payload = []
    for row in rows:
        close = row["close"]
        prev_close = row.get("prev_close", close)
        open_val = row.get("open", prev_close)
        high_val = row.get("high", max(close, open_val, prev_close))
        low_val = row.get("low", min(close, open_val, prev_close))
        
        item = {
            "ticker": row["ticker"],
            "date": row["date"],
            "prev_close": prev_close,
            "open": open_val,
            "high": high_val,
            "low": low_val,
            "close": close,
            "total_trade": row.get("total_trade", 0),
            "total_value": row.get("total_value", 0.0),
            "total_volume": row.get("total_volume", 0),
            "buy_count": row.get("buy_count", 0),
            "sell_count": row.get("sell_count", 0),
            "buy_volume": row.get("buy_volume", 0),
            "sell_volume": row.get("sell_volume", 0),
            "foreign_buy_volume": row.get("foreign_buy_volume", 0),
            "foreign_sell_volume": row.get("foreign_sell_volume", 0),
            "foreign_buy_value": row.get("foreign_buy_value", 0.0),
            "foreign_sell_value": row.get("foreign_sell_value", 0.0),
            "foreign_room": row.get("foreign_room", 0),
        }
        payload.append(item)

    if not payload:
        return 0
    conn.executemany(_UPSERT_SQL, payload)
    conn.commit()
    return len(payload)


def get_range(
    conn: sqlite3.Connection,
    ticker: str,
    start: str | None = None,
    end: str | None = None,
    timeframe: str = "1D",
) -> list[dict]:
    """Rows for `ticker` between the inclusive ISO dates/timestamps `start` and `end`."""
    sql = f"SELECT {', '.join(COLUMNS)} FROM prices WHERE ticker = ?"
    params: list[object] = [ticker.upper()]
    if start:
        sql += " AND date >= ?"
        params.append(start)
    if end:
        sql += " AND date <= ?"
        params.append(end)
    sql += " ORDER BY date ASC"
    raw_rows = [dict(r) for r in conn.execute(sql, params)]
    if not raw_rows or timeframe == "1H":
        return raw_rows
    
    # Resample to target timeframe
    df = pd.DataFrame(raw_rows).set_index("date")
    resampled = resample_bars(df, timeframe)
    out_rows = resampled.reset_index().to_dict(orient="records")
    return out_rows


def get_recent(
    conn: sqlite3.Connection,
    ticker: str,
    lookback_days: int,
    timeframe: str = "1D",
) -> list[dict]:
    """The most recent `lookback_days` *trading rows* for `ticker`, oldest first.

    If timeframe != '1H', retrieves sufficient underlying rows and resamples to
    the target timeframe, returning at most `lookback_days` aggregated bars.
    Adapts dynamically to both daily and intraday (hourly) stored data density.
    """
    if lookback_days <= 0:
        return []

    # Check density of stored data for this ticker (daily vs intraday hourly)
    sample_row = conn.execute(
        "SELECT date FROM prices WHERE ticker = ? ORDER BY date DESC LIMIT 1",
        (ticker.upper(),),
    ).fetchone()
    if not sample_row:
        return []

    is_intraday = ":" in str(sample_row[0]) or len(str(sample_row[0])) > 10

    # If asking for 1H base bars directly
    if timeframe == "1H":
        sql = (
            f"SELECT {', '.join(COLUMNS)} FROM prices "
            "WHERE ticker = ? ORDER BY date DESC LIMIT ?"
        )
        rows = [dict(r) for r in conn.execute(sql, (ticker.upper(), lookback_days))]
        rows.reverse()
        return rows

    # Multipliers to ensure we pull enough base rows for aggregated timeframes
    # In Vietnam market, there are ~5 hourly trading bars per day.
    intraday_factor = 5 if is_intraday else 1
    base_mult_map = {"4H": 4, "1D": 1, "3D": 3, "1W": 5, "1M": 22, "1Y": 252}
    base_mult = base_mult_map.get(timeframe.upper(), 1)
    effective_mult = base_mult * intraday_factor if timeframe.upper() not in ("4H",) else (4 if is_intraday else 1)

    total_rows = row_count(conn, ticker)
    fetch_limit = min(max(lookback_days * effective_mult + 50 * intraday_factor, 300), total_rows)

    sql = (
        f"SELECT {', '.join(COLUMNS)} FROM prices "
        "WHERE ticker = ? ORDER BY date DESC LIMIT ?"
    )
    rows = [dict(r) for r in conn.execute(sql, (ticker.upper(), fetch_limit))]
    rows.reverse()
    if not rows:
        return []

    df = pd.DataFrame(rows).set_index("date")
    resampled = resample_bars(df, timeframe)

    # Dynamic expansion if more rows are available and resampled bar count fell short
    while len(resampled) < lookback_days and fetch_limit < total_rows:
        fetch_limit = min(fetch_limit * 3, total_rows)
        rows = [dict(r) for r in conn.execute(sql, (ticker.upper(), fetch_limit))]
        rows.reverse()
        df = pd.DataFrame(rows).set_index("date")
        resampled = resample_bars(df, timeframe)

    if len(resampled) > lookback_days:
        resampled = resampled.tail(lookback_days)
    return resampled.reset_index().to_dict(orient="records")


def list_tickers(conn: sqlite3.Connection) -> list[str]:
    """Every ticker in the store, alphabetically."""
    return [r[0] for r in conn.execute("SELECT DISTINCT ticker FROM prices ORDER BY ticker")]


def row_count(conn: sqlite3.Connection, ticker: str | None = None) -> int:
    """Stored row count, overall or for one ticker."""
    if ticker is None:
        return int(conn.execute("SELECT COUNT(*) FROM prices").fetchone()[0])
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM prices WHERE ticker = ?", (ticker.upper(),)
        ).fetchone()[0]
    )


def date_bounds(conn: sqlite3.Connection, ticker: str) -> tuple[str | None, str | None]:
    """First and last stored date for `ticker`, or (None, None) if it is unknown."""
    row = conn.execute(
        "SELECT MIN(date), MAX(date) FROM prices WHERE ticker = ?", (ticker.upper(),)
    ).fetchone()
    return (row[0], row[1]) if row else (None, None)


def rows_to_columns(rows: Sequence[dict]) -> dict[str, list]:
    """Transpose row dicts into column lists. Convenience for callers building frames."""
    return {c: [row[c] for row in rows] for c in COLUMNS if rows and c in rows[0]}
