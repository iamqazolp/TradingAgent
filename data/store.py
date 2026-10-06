"""SQLite store for daily trading statistics, hourly bars, snapshots, and market indices."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterable, Sequence
from pathlib import Path

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

HOURLY_COLUMNS: tuple[str, ...] = (
    "ticker",
    "datetime",
    "date",
    "session_index",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "value",
    "is_closed",
)

SNAPSHOT_COLUMNS: tuple[str, ...] = (
    "ticker",
    "timestamp",
    "price",
    "accumulated_volume",
    "accumulated_value",
)

MARKET_INDEX_COLUMNS: tuple[str, ...] = (
    "exchange",
    "date",
    "index_current",
    "index_change",
    "index_percent_change",
    "total_trade",
    "total_value",
    "total_volume",
    "advances",
    "declines",
    "unchanged",
)

_UPSERT_SQL = """
INSERT INTO daily_prices ({cols})
VALUES ({placeholders})
ON CONFLICT(ticker, date) DO UPDATE SET {updates}
""".format(
    cols=", ".join(COLUMNS),
    placeholders=", ".join(f":{c}" for c in COLUMNS),
    updates=", ".join(f"{c}=excluded.{c}" for c in COLUMNS if c not in ("ticker", "date")),
)

_UPSERT_HOURLY_SQL = """
INSERT INTO hourly_bars ({cols})
VALUES ({placeholders})
ON CONFLICT(ticker, datetime) DO UPDATE SET {updates}
""".format(
    cols=", ".join(HOURLY_COLUMNS),
    placeholders=", ".join(f":{c}" for c in HOURLY_COLUMNS),
    updates=", ".join(
        f"{c}=excluded.{c}"
        for c in HOURLY_COLUMNS
        if c not in ("ticker", "datetime")
    ),
)

_INSERT_SNAPSHOT_SQL = """
INSERT OR REPLACE INTO realtime_snapshots ({cols})
VALUES ({placeholders})
""".format(
    cols=", ".join(SNAPSHOT_COLUMNS),
    placeholders=", ".join(f":{c}" for c in SNAPSHOT_COLUMNS),
)

_UPSERT_MARKET_INDICES_SQL = """
INSERT INTO market_indices ({cols})
VALUES ({placeholders})
ON CONFLICT(exchange, date) DO UPDATE SET {updates}
""".format(
    cols=", ".join(MARKET_INDEX_COLUMNS),
    placeholders=", ".join(f":{c}" for c in MARKET_INDEX_COLUMNS),
    updates=", ".join(
        f"{c}=excluded.{c}"
        for c in MARKET_INDEX_COLUMNS
        if c not in ("exchange", "date")
    ),
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
    """Apply schema.sql and migrate existing tables if needed. Idempotent."""
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    cursor = conn.execute("PRAGMA table_info(daily_prices)")
    existing_cols = {row[1] for row in cursor.fetchall()}
    for col in ("open", "high", "low"):
        if col not in existing_cols:
            conn.execute(f"ALTER TABLE daily_prices ADD COLUMN {col} REAL")
    conn.commit()


def upsert_rows(conn: sqlite3.Connection, rows: Iterable[dict]) -> int:
    """Insert or update daily rows. Returns the number of rows written."""
    payload = []
    for r in rows:
        row_dict = r.to_dict() if hasattr(r, "to_dict") else dict(r)
        payload.append({c: row_dict.get(c, None) for c in COLUMNS})
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
) -> list[dict]:
    """Rows for `ticker` between the inclusive ISO dates `start` and `end`."""
    sql = f"SELECT {', '.join(COLUMNS)} FROM daily_prices WHERE ticker = ?"
    params: list[object] = [ticker.upper()]
    if start:
        sql += " AND date >= ?"
        params.append(start)
    if end:
        sql += " AND date <= ?"
        params.append(end)
    sql += " ORDER BY date ASC"
    return [dict(r) for r in conn.execute(sql, params)]


def get_recent(conn: sqlite3.Connection, ticker: str, lookback_days: int) -> list[dict]:
    """The most recent `lookback_days` trading rows for `ticker`, oldest first."""
    if lookback_days <= 0:
        return []
    sql = (
        f"SELECT {', '.join(COLUMNS)} FROM daily_prices "
        "WHERE ticker = ? ORDER BY date DESC LIMIT ?"
    )
    rows = [dict(r) for r in conn.execute(sql, (ticker.upper(), lookback_days))]
    rows.reverse()
    return rows


def list_tickers(conn: sqlite3.Connection) -> list[str]:
    """Every ticker in the store, alphabetically."""
    return [r[0] for r in conn.execute("SELECT DISTINCT ticker FROM daily_prices ORDER BY ticker")]


def row_count(conn: sqlite3.Connection, ticker: str | None = None) -> int:
    """Stored row count, overall or for one ticker."""
    if ticker is None:
        return int(conn.execute("SELECT COUNT(*) FROM daily_prices").fetchone()[0])
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM daily_prices WHERE ticker = ?", (ticker.upper(),)
        ).fetchone()[0]
    )


def date_bounds(conn: sqlite3.Connection, ticker: str) -> tuple[str | None, str | None]:
    """First and last stored date for `ticker`, or (None, None) if it is unknown."""
    row = conn.execute(
        "SELECT MIN(date), MAX(date) FROM daily_prices WHERE ticker = ?", (ticker.upper(),)
    ).fetchone()
    return (row[0], row[1]) if row else (None, None)


def rows_to_columns(rows: Sequence[dict]) -> dict[str, list]:
    """Transpose row dicts into column lists. Convenience for callers building frames."""
    return {c: [row.get(c, None) for row in rows] for c in COLUMNS}


# --------------------------------------------------------------------------- hourly bars


def upsert_hourly_bars(conn: sqlite3.Connection, rows: Iterable[dict | Any]) -> int:
    """Insert or update 1-hour bars. Returns the number of bars written."""
    payload = []
    for r in rows:
        row_dict = r.to_dict() if hasattr(r, "to_dict") else dict(r)
        if "ticker" in row_dict:
            row_dict["ticker"] = str(row_dict["ticker"]).upper()
        payload.append({c: row_dict.get(c, None) for c in HOURLY_COLUMNS})
    if not payload:
        return 0
    conn.executemany(_UPSERT_HOURLY_SQL, payload)
    conn.commit()
    return len(payload)


def get_recent_hourly(
    conn: sqlite3.Connection, ticker: str, lookback_hours: int
) -> list[dict]:
    """The most recent `lookback_hours` 1H bars for `ticker`, oldest first."""
    if lookback_hours <= 0:
        return []
    sql = (
        f"SELECT {', '.join(HOURLY_COLUMNS)} FROM hourly_bars "
        "WHERE ticker = ? ORDER BY datetime DESC LIMIT ?"
    )
    rows = [dict(r) for r in conn.execute(sql, (ticker.upper(), lookback_hours))]
    rows.reverse()
    return rows


def get_hourly_range(
    conn: sqlite3.Connection,
    ticker: str,
    start: str | None = None,
    end: str | None = None,
) -> list[dict]:
    """1H bars for `ticker` between `start` and `end` (datetime or date prefix), oldest first."""
    sql = f"SELECT {', '.join(HOURLY_COLUMNS)} FROM hourly_bars WHERE ticker = ?"
    params: list[object] = [ticker.upper()]
    if start:
        start_dt = f"{start} 00:00:00" if len(start) == 10 else start
        sql += " AND datetime >= ?"
        params.append(start_dt)
    if end:
        end_dt = f"{end} 23:59:59" if len(end) == 10 else end
        sql += " AND datetime <= ?"
        params.append(end_dt)
    sql += " ORDER BY datetime ASC"
    return [dict(r) for r in conn.execute(sql, params)]


def get_hourly_bar(
    conn: sqlite3.Connection, ticker: str, datetime_str: str
) -> dict | None:
    """Retrieve a single hourly bar by (ticker, datetime)."""
    sql = f"SELECT {', '.join(HOURLY_COLUMNS)} FROM hourly_bars WHERE ticker = ? AND datetime = ?"
    row = conn.execute(sql, (ticker.upper(), datetime_str)).fetchone()
    return dict(row) if row else None


def hourly_row_count(conn: sqlite3.Connection, ticker: str | None = None) -> int:
    """Stored hourly bar count, overall or for one ticker."""
    if ticker is None:
        return int(conn.execute("SELECT COUNT(*) FROM hourly_bars").fetchone()[0])
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM hourly_bars WHERE ticker = ?", (ticker.upper(),)
        ).fetchone()[0]
    )


# --------------------------------------------------------------------------- realtime snapshots


def insert_snapshot(conn: sqlite3.Connection, snapshot: dict | Any) -> None:
    """Insert or replace a realtime market snapshot."""
    snap_dict = snapshot.to_dict() if hasattr(snapshot, "to_dict") else dict(snapshot)
    if "ticker" in snap_dict:
        snap_dict["ticker"] = str(snap_dict["ticker"]).upper()
    payload = {c: snap_dict.get(c, None) for c in SNAPSHOT_COLUMNS}
    conn.execute(_INSERT_SNAPSHOT_SQL, payload)
    conn.commit()


def get_snapshots(
    conn: sqlite3.Connection, ticker: str, date: str | None = None
) -> list[dict]:
    """Retrieve stored snapshots for `ticker`, optionally filtered by date prefix."""
    sql = f"SELECT {', '.join(SNAPSHOT_COLUMNS)} FROM realtime_snapshots WHERE ticker = ?"
    params: list[object] = [ticker.upper()]
    if date:
        sql += " AND timestamp LIKE ?"
        params.append(f"{date}%")
    sql += " ORDER BY timestamp ASC"
    return [dict(r) for r in conn.execute(sql, params)]


# --------------------------------------------------------------------------- market indices


def upsert_market_indices(conn: sqlite3.Connection, rows: Iterable[dict]) -> int:
    """Insert or update market indices. Returns the number of rows written."""
    payload = [{c: row.get(c, None) for c in MARKET_INDEX_COLUMNS} for row in rows]
    if not payload:
        return 0
    for item in payload:
        if item.get("exchange") is not None:
            item["exchange"] = str(item["exchange"]).upper()
    conn.executemany(_UPSERT_MARKET_INDICES_SQL, payload)
    conn.commit()
    return len(payload)


def get_latest_market_indices(
    conn: sqlite3.Connection, exchange: str | None = None
) -> list[dict]:
    """Latest market index rows, either for all exchanges or a specific exchange."""
    if exchange is not None:
        sql = (
            f"SELECT {', '.join(MARKET_INDEX_COLUMNS)} FROM market_indices "
            "WHERE exchange = ? ORDER BY date DESC LIMIT 1"
        )
        return [dict(r) for r in conn.execute(sql, (exchange.upper(),))]

    sql = (
        f"SELECT {', '.join(MARKET_INDEX_COLUMNS)} FROM market_indices m "
        "WHERE m.date = ("
        "  SELECT MAX(m2.date) FROM market_indices m2 WHERE m2.exchange = m.exchange"
        ") ORDER BY m.exchange ASC"
    )
    return [dict(r) for r in conn.execute(sql)]


def get_market_indices_range(
    conn: sqlite3.Connection,
    exchange: str,
    start: str | None = None,
    end: str | None = None,
) -> list[dict]:
    """Market index rows for `exchange` between inclusive ISO dates `start` and `end`."""
    sql = f"SELECT {', '.join(MARKET_INDEX_COLUMNS)} FROM market_indices WHERE exchange = ?"
    params: list[object] = [exchange.upper()]
    if start:
        sql += " AND date >= ?"
        params.append(start)
    if end:
        sql += " AND date <= ?"
        params.append(end)
    sql += " ORDER BY date ASC"
    return [dict(r) for r in conn.execute(sql, params)]
