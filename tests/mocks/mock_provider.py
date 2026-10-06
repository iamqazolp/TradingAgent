"""Mock implementation of MarketDataProvider for testing."""

from __future__ import annotations

from collections.abc import Sequence

from data.provider import DailyRecord, MarketDataProvider, RealtimeSnapshot


class MockDataProvider(MarketDataProvider):
    """Configurable mock data provider implementing MarketDataProvider."""

    def __init__(
        self,
        daily_records: dict[str, list[DailyRecord]] | None = None,
        realtime_quotes: dict[str, RealtimeSnapshot] | None = None,
    ) -> None:
        self._daily_records: dict[str, list[DailyRecord]] = daily_records or {}
        self._realtime_quotes: dict[str, RealtimeSnapshot] = realtime_quotes or {}
        self.fetch_daily_history_calls: list[tuple[str, str, str]] = []
        self.fetch_realtime_quote_calls: list[str] = []

    def set_daily_records(self, ticker: str, records: list[DailyRecord]) -> None:
        self._daily_records[ticker.upper()] = records

    def set_realtime_quote(self, quote: RealtimeSnapshot) -> None:
        self._realtime_quotes[quote.ticker.upper()] = quote

    def fetch_daily_history(
        self, ticker: str, start: str, end: str
    ) -> list[DailyRecord]:
        self.fetch_daily_history_calls.append((ticker.upper(), start, end))
        records = self._daily_records.get(ticker.upper(), [])
        return [r for r in records if start <= r.date <= end]

    def fetch_realtime_quote(self, ticker: str) -> RealtimeSnapshot:
        self.fetch_realtime_quote_calls.append(ticker.upper())
        quote = self._realtime_quotes.get(ticker.upper())
        if quote is None:
            raise KeyError(f"No realtime quote configured for ticker: {ticker}")
        return quote

    def fetch_realtime_quotes(
        self, tickers: Sequence[str]
    ) -> list[RealtimeSnapshot]:
        results: list[RealtimeSnapshot] = []
        for t in tickers:
            quote = self._realtime_quotes.get(t.upper())
            if quote is not None:
                results.append(quote)
        return results
