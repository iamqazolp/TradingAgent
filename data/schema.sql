-- Daily trading statistics, one row per ticker per trading day.
-- Derived quantities (net buy volume, average trade size,
-- foreign net flow, ...) are computed on read in the indicator engine, not stored.
CREATE TABLE IF NOT EXISTS daily_prices (
    ticker TEXT NOT NULL,
    date TEXT NOT NULL,
    prev_close REAL NOT NULL,
    open REAL,
    high REAL,
    low REAL,
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

CREATE TABLE IF NOT EXISTS market_indices (
    exchange TEXT NOT NULL,
    date TEXT NOT NULL,
    index_current REAL NOT NULL,
    index_change REAL NOT NULL,
    index_percent_change REAL NOT NULL,
    total_trade INTEGER,
    total_value REAL,
    total_volume INTEGER,
    advances INTEGER,
    declines INTEGER,
    unchanged INTEGER,
    PRIMARY KEY (exchange, date)
);

-- Chuỗi nến 1 giờ (1H), khóa chính theo (ticker, datetime).
CREATE TABLE IF NOT EXISTS hourly_bars (
    ticker TEXT NOT NULL,
    datetime TEXT NOT NULL,        -- 'YYYY-MM-DD HH:MM:SS'
    date TEXT NOT NULL,            -- 'YYYY-MM-DD'
    session_index INTEGER NOT NULL,-- 1..4 (Vietnam trading session)
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume INTEGER NOT NULL,       -- Traded volume in this specific hour
    value REAL NOT NULL,          -- Traded value in this specific hour
    is_closed INTEGER DEFAULT 0,  -- 0: forming, 1: closed
    PRIMARY KEY (ticker, datetime)
);

CREATE INDEX IF NOT EXISTS idx_hourly_ticker_date 
ON hourly_bars (ticker, date);

-- Bảng cache snapshot thô phục vụ tổng hợp và kiểm toán
CREATE TABLE IF NOT EXISTS realtime_snapshots (
    ticker TEXT NOT NULL,
    timestamp TEXT NOT NULL,       -- 'YYYY-MM-DD HH:MM:SS'
    price REAL NOT NULL,
    accumulated_volume INTEGER NOT NULL,
    accumulated_value REAL NOT NULL,
    PRIMARY KEY (ticker, timestamp)
);
