-- Daily trading statistics, one row per ticker per trading day.
-- Mirrors the VietinBank GetTradingStatistics response; see data/ingest.py for the
-- raw-field mapping. Derived quantities (net buy volume, average trade size,
-- foreign net flow, ...) are computed on read in the indicator engine, not stored.
CREATE TABLE IF NOT EXISTS daily_prices (
    ticker TEXT NOT NULL,
    date TEXT NOT NULL,
    prev_close REAL NOT NULL,
    close REAL NOT NULL,
    total_trade INTEGER NOT NULL,
    total_value REAL NOT NULL,
    total_volume INTEGER NOT NULL,
    buy_count INTEGER NOT NULL,
    sell_count INTEGER NOT NULL,
    buy_volume INTEGER NOT NULL,
    sell_volume INTEGER NOT NULL,
    foreign_buy_volume INTEGER NOT NULL,
    foreign_sell_volume INTEGER NOT NULL,
    foreign_buy_value REAL NOT NULL,
    foreign_sell_value REAL NOT NULL,
    foreign_room INTEGER NOT NULL,
    PRIMARY KEY (ticker, date)
);
