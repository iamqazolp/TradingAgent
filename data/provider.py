"""Market data provider protocol and domain models.

Establishes the boundary layer between external data feeds (APIs/websockets/scrapers)
and internal storage/indicator calculation modules.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Protocol, Sequence, runtime_checkable


@dataclass(frozen=True)
class DailyRecord:
    """Domain model for a single daily trading record (1D OHLCV + flows)."""

    ticker: str
    date: str  # ISO YYYY-MM-DD
    open: float | None
    high: float | None
    low: float | None
    close: float
    prev_close: float
    total_volume: int
    total_value: float
    total_trade: int = 0
    buy_count: int = 0
    sell_count: int = 0
    buy_volume: int = 0
    sell_volume: int = 0
    foreign_buy_volume: int = 0
    foreign_sell_volume: int = 0
    foreign_buy_value: float = 0.0
    foreign_sell_value: float = 0.0
    foreign_room: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class RealtimeSnapshot:
    """Domain model for an intraday quote / market snapshot at a specific point in time."""

    ticker: str
    timestamp: str  # ISO YYYY-MM-DD HH:MM:SS or ISO format (Vietnam local time)
    price: float
    accumulated_volume: int
    accumulated_value: float
    change: float = 0.0
    percent_change: float = 0.0
    high_day: float | None = None
    low_day: float | None = None
    open_day: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class HourlyBar:
    """Domain model for a 1-hour trading bar (1H OHLCV)."""

    ticker: str
    datetime: str  # Start time of the bar: 'YYYY-MM-DD HH:MM:SS'
    date: str  # 'YYYY-MM-DD'
    session_index: int  # 1..4 (Vietnam trading session)
    open: float
    high: float
    low: float
    close: float
    volume: int
    value: float
    is_closed: int = 0  # 0: actively forming, 1: closed/complete

    def to_dict(self) -> dict:
        return asdict(self)


@runtime_checkable
class MarketDataProvider(Protocol):
    """Abstract provider contract for fetching market data."""

    def fetch_daily_history(
        self, ticker: str, start: str, end: str
    ) -> list[DailyRecord]:
        """Fetch historical daily records for ticker between start and end (inclusive)."""
        ...

    def fetch_realtime_quote(
        self, ticker: str
    ) -> RealtimeSnapshot:
        """Fetch latest intraday quote/snapshot for a single ticker."""
        ...

    def fetch_realtime_quotes(
        self, tickers: Sequence[str]
    ) -> list[RealtimeSnapshot]:
        """Fetch latest intraday quotes for multiple tickers in batch."""
        ...
