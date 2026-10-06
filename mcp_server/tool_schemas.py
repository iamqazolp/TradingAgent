"""Pydantic models validating every MCP tool input.

The tool functions take flat primitive arguments, which keeps the generated JSON
schema readable for the model, and then validate them through these models. So
nothing reaches the engine unchecked, and a bad call gets a precise error instead
of a pandas traceback.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from indicators.engine import GROUPS

GroupName = Literal["trend", "momentum", "volatility", "volume_flow", "trade_flow", "value_flow", "foreign_flow"]

Ticker = Annotated[str, Field(min_length=1, max_length=12, pattern=r"^[A-Za-z0-9]+$")]
IsoDate = Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$")]


def _check_iso(value: str | None) -> str | None:
    if value is None:
        return None
    date.fromisoformat(value)  # raises ValueError on a nonsense date like 2024-13-45
    return value


class PriceRow(BaseModel):
    """One stored trading day or hourly bar, as returned by `get_price_data`."""

    model_config = ConfigDict(extra="ignore")

    date: str
    datetime: str | None = None
    session_index: int | None = None
    prev_close: float | None = Field(default=None, description="previous close, VND")
    open: float | None = Field(default=None, description="open price, VND")
    high: float | None = Field(default=None, description="high price, VND")
    low: float | None = Field(default=None, description="low price, VND")
    close: float = Field(gt=0, description="close, VND")
    volume: int | None = None
    value: float | None = None
    total_trade: int = Field(default=0, description="matched trade count")
    total_value: float = Field(default=0.0, description="total traded value, VND")
    total_volume: int = Field(default=0, description="total traded volume, shares")
    buy_count: int = Field(default=0)
    sell_count: int = Field(default=0)
    buy_volume: int = Field(default=0)
    sell_volume: int = Field(default=0)
    foreign_buy_volume: int = Field(default=0)
    foreign_sell_volume: int = Field(default=0)
    foreign_buy_value: float = Field(default=0.0)
    foreign_sell_value: float = Field(default=0.0)
    foreign_room: int = Field(default=0, description="remaining foreign room, shares")
    ticker: str | None = None

    @field_validator("date")
    @classmethod
    def _valid_date(cls, value: str) -> str:
        _check_iso(value[:10])
        return value


class RowSource(BaseModel):
    """Either explicit rows, or a ticker for the server to load them itself.

    The plan's contract passes `rows` in from a prior `get_price_data` call. That
    round trip costs a lot of context for 300 rows, so `ticker` is accepted as an
    equivalent alternative that loads the same rows server-side.
    """

    model_config = ConfigDict(extra="forbid")

    # Deliberately NOT `| str`: the before-validator below accepts a stringified
    # list from local models and converts it, but leaving `str` in the annotation
    # also let an unconvertible string pass validation and reach the engine,
    # where iterating it per-row raised AttributeError instead of a readable
    # error the model could act on.
    rows: list[PriceRow] | None = None
    ticker: Ticker | None = None
    lookback_days: int = Field(
        default=300,
        ge=2,
        le=5000,
        description="most recent N trading rows (default 300). Trend indicators require at least 200 rows for SMA200.",
    )
    timeframe: Literal["1d", "1h"] = Field(
        default="1d",
        description="Bar timeframe: '1d' for daily bars (default), '1h' for hourly bars.",
    )
    as_of: IsoDate | None = Field(
        default=None,
        description=(
            "Compute the indicators AS OF this date (YYYY-MM-DD) instead of the "
            "latest session. Use for historical questions like 'RSI của VNM ngày "
            "2026-01-02'. Only rows up to and including this date are used, so "
            "every `latest` value is the value that stood on that date. If the "
            "date is not a trading day, the previous trading day is used and "
            "reported in `as_of_effective`. Omit for the current reading."
        ),
    )

    @field_validator("as_of")
    @classmethod
    def _real_as_of(cls, value: str | None) -> str | None:
        return _check_iso(value)

    @field_validator("rows", mode="before")
    @classmethod
    def _normalize_rows(cls, value: Any) -> list | None:
        """Normalize stringified None/null/empty/JSON from local LLMs (Ollama etc.).

        A string that parses to a single row object becomes a one-item list, and
        anything else unparseable becomes None so the model is told to pass
        `ticker` instead — rather than the string surviving into the engine.
        """
        if value is None:
            return None
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.lower() in ("none", "null", "[]", "''", '""', ""):
                return None
            try:
                import json
                parsed = json.loads(stripped)
            except Exception:
                return None
            if isinstance(parsed, list):
                return parsed or None
            if isinstance(parsed, dict):
                return [parsed]
            return None
        if isinstance(value, dict):
            return [value]
        if isinstance(value, list) and len(value) == 0:
            return None
        return value

    @model_validator(mode="after")
    def _one_source(self):
        if not self.rows and not self.ticker:
            raise ValueError("supply either `rows` (from get_price_data) or `ticker`")
        if self.rows and self.ticker:
            raise ValueError("supply `rows` or `ticker`, not both")
        if self.rows is not None and len(self.rows) < 2:
            raise ValueError("at least 2 rows are needed to compute anything")
        if self.as_of and self.rows:
            raise ValueError(
                "`as_of` only applies when the server loads rows; pass `ticker` "
                "with `as_of`, or trim the `rows` you supply yourself"
            )
        return self


class GetPriceDataInput(BaseModel):
    """Input for `get_price_data`."""

    model_config = ConfigDict(extra="forbid")

    ticker: Ticker
    lookback_days: int = Field(
        default=300,
        ge=1,
        le=5000,
        description="most recent N trading rows (e.g. 10 for the last 10 sessions). Use this for any 'recent' or 'last N days' questions.",
    )
    timeframe: Literal["1d", "1h"] = Field(
        default="1d",
        description="Bar timeframe: '1d' for daily bars (default), '1h' for hourly bars.",
    )
    start: IsoDate | None = Field(
        default=None,
        description="Optional start date (YYYY-MM-DD). ONLY use when the user specifically requested a past historical date range. Do NOT invent or guess dates.",
    )
    end: IsoDate | None = Field(
        default=None,
        description="Optional end date (YYYY-MM-DD). ONLY use when the user specifically requested a past historical date range. Do NOT invent or guess dates.",
    )

    @field_validator("ticker")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.upper()

    @field_validator("start", "end")
    @classmethod
    def _real_calendar_date(cls, value: str | None) -> str | None:
        # Per field rather than in the model validator, so the error the model
        # sees names the offending field instead of just "input".
        return _check_iso(value)

    @model_validator(mode="after")
    def _valid_range(self):
        if self.start and self.end and self.start > self.end:
            raise ValueError(f"start ({self.start}) is after end ({self.end})")
        return self


def _parse_group_list(value: Any) -> list[str]:
    """Coerce whatever a model sent into a validated list of group names.

    Local models send this field as a bare string, a comma-joined string, or a
    string holding a Python/JSON list literal. Shared by every tool that takes
    `groups` — it was previously duplicated verbatim in two schema classes,
    so a fix to one silently left the other behind.
    """
    if isinstance(value, str):
        text = value.strip()
        if (text.startswith("[") and text.endswith("]")) or (
            text.startswith("(") and text.endswith(")")
        ):
            import ast
            try:
                parsed = ast.literal_eval(text)
                if isinstance(parsed, (list, tuple)):
                    items = [str(x).strip() for x in parsed]
                else:
                    items = [str(parsed).strip()]
            except Exception:
                cleaned = text.strip("[]()").replace('"', "").replace("'", "")
                items = [g.strip().strip("'\"") for g in cleaned.split(",") if g.strip().strip("'\"")]
        else:
            items = [g.strip().strip("'\"") for g in text.split(",") if g.strip().strip("'\"")]
        value = items
    elif isinstance(value, (tuple, set)):
        value = list(value)
    elif not isinstance(value, list):
        value = [value]

    unknown = [g for g in value if g not in GROUPS]
    if unknown:
        raise ValueError(
            f"unknown group(s): {', '.join(unknown)}; valid: {', '.join(GROUPS)}"
        )
    return list(dict.fromkeys(value)) or list(GROUPS)


class GroupSelection(RowSource):
    """A RowSource that also takes a subset of indicator groups."""

    groups: list[GroupName] | str = Field(
        default_factory=lambda: list(GROUPS),
        description=f"subset of: {', '.join(GROUPS)}",
    )

    @field_validator("groups", mode="before")
    @classmethod
    def _known_groups(cls, value: Any) -> list[str]:
        return _parse_group_list(value)


class ComputeIndicatorsInput(GroupSelection):
    """Input for `compute_indicators`."""

    params: dict[str, Any] | None = Field(
        default=None,
        description=(
            "optional window overrides: sma_windows, ema_windows, macd_fast, "
            "macd_slow, macd_signal, rsi_window, bollinger_window, bollinger_k, "
            "volatility_window, flow_window, trade_size_window, value_window"
        ),
    )
    series_tail: int = Field(
        default=20, ge=0, le=250, description="points of each series to return; 0 for none. Default 20 provides ~1 month of history for divergence and trajectory analysis."
    )


class GetFlowSummaryInput(RowSource):
    """Input for `get_flow_summary`."""

    window: int = Field(default=5, ge=2, le=250, description="rolling window in trading days")


class AnalyzeMultiHorizonInput(BaseModel):
    """Input for `analyze_multi_horizon`.

    This tool always loads rows server-side (by ticker) because it needs
    a large lookback for weekly aggregation and long-term indicators.
    """

    model_config = ConfigDict(extra="forbid")

    ticker: Ticker
    lookback_days: int = Field(
        default=500,
        ge=10,
        le=5000,
        description=(
            "Number of most recent trading rows to load. Default 500 "
            "(~2 years). Can be smaller (e.g. 60-90 for 3 months) for shorter queries."
        ),
    )
    series_tail: int = Field(
        default=5, ge=0, le=250,
        description="Daily series tail length (default 5).",
    )
    weekly_series_tail: int = Field(
        default=5, ge=0, le=100,
        description="Weekly series tail length (default 5).",
    )
    detail: Literal["compact", "full"] = Field(
        default="compact",
        description=(
            "'compact' (default) omits fields that duplicate data found elsewhere "
            "in the same response, roughly halving the payload. 'full' keeps "
            "everything — only needed when a caller wants the redundant copies."
        ),
    )
    scope: Literal["full", "short_term", "mid_term", "long_term", "levels"] = Field(
        default="full",
        description=(
            "Which sections to compute, matched to the question being asked. "
            "'full' = comprehensive analysis, all three horizons (phân tích toàn diện). "
            "'short_term' = only the short-term horizon (ngắn hạn, 1-4 weeks). "
            "'mid_term' = only the mid-term horizon (trung hạn, 1-3 months). "
            "'long_term' = only the long-term horizon (dài hạn, over 3 months). "
            "'levels' = only support/resistance levels and current price position "
            "(hỗ trợ kháng cự). "
            "A scoped call is much cheaper: 'short_term' is ~43% and 'levels' ~23% "
            "of the full payload. Sections a scope skips are listed in "
            "`sections_omitted` — they are NOT missing data."
        ),
    )

    as_of: IsoDate | None = Field(
        default=None,
        description=(
            "Run the whole analysis AS OF this date (YYYY-MM-DD) instead of the "
            "latest session, using only rows up to and including it. For "
            "historical questions like 'phân tích VNM tại ngày 2026-01-02'. Omit "
            "for the current reading."
        ),
    )

    @field_validator("ticker")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.upper()

    @field_validator("as_of")
    @classmethod
    def _real_as_of(cls, value: str | None) -> str | None:
        return _check_iso(value)

    @field_validator("detail", mode="before")
    @classmethod
    def _normalize_detail(cls, value: Any) -> Any:
        """Tolerate case and stray quoting from local models."""
        if isinstance(value, str):
            cleaned = value.strip().strip("'\"").lower()
            return cleaned or "compact"
        return value

    @field_validator("scope", mode="before")
    @classmethod
    def _normalize_scope(cls, value: Any) -> Any:
        """Accept the shapes a local model is likely to send for the scope."""
        if not isinstance(value, str):
            return value
        cleaned = value.strip().strip("'\"").lower().replace("-", "_").replace(" ", "_")
        aliases = {
            "": "full",
            "all": "full",
            "comprehensive": "full",
            "short": "short_term",
            "shortterm": "short_term",
            "ngan_han": "short_term",
            "mid": "mid_term",
            "midterm": "mid_term",
            "medium": "mid_term",
            "medium_term": "mid_term",
            "trung_han": "mid_term",
            "long": "long_term",
            "longterm": "long_term",
            "dai_han": "long_term",
            "level": "levels",
            "support_resistance": "levels",
        }
        return aliases.get(cleaned, cleaned)


class ComputeWeeklyInput(GroupSelection):
    """Input for `compute_weekly_indicators`."""

    series_tail: int = Field(
        default=26, ge=0, le=100,
        description="points of each weekly series to return; default 26 (~6 months).",
    )


class CompareTickersInput(BaseModel):
    """Input for `compare_tickers`."""

    model_config = ConfigDict(extra="forbid")

    tickers: list[Ticker] | str = Field(
        description="List of 2 to 5 stock ticker symbols to compare (e.g. ['CTG', 'VCB'] or 'CTG,VCB')."
    )
    lookback_days: int = Field(
        default=250,
        ge=20,
        le=5000,
        description="Trading sessions to look back (default 250 for ~1 year performance analysis).",
    )
    detail: Literal["compact", "full"] = Field(
        default="compact",
        description=(
            "'compact' (default) omits the full per-ticker indicator dump, which "
            "no comparison table needs. 'full' includes it."
        ),
    )

    @field_validator("detail", mode="before")
    @classmethod
    def _normalize_detail(cls, value: Any) -> Any:
        if isinstance(value, str):
            cleaned = value.strip().strip("'\"").lower()
            return cleaned or "compact"
        return value

    @field_validator("tickers", mode="before")
    @classmethod
    def _normalize_tickers(cls, value: Any) -> list[str]:
        if isinstance(value, str):
            v = value.strip().strip("[]()")
            parts = [t.strip().strip("'\"").upper() for t in v.split(",") if t.strip().strip("'\"")]
            return parts
        if isinstance(value, (list, tuple)):
            return [str(t).strip().upper() for t in value if str(t).strip()]
        return value

    @model_validator(mode="after")
    def _check_ticker_count(self):
        if not self.tickers or len(self.tickers) < 2:
            raise ValueError("compare_tickers requires at least 2 tickers")
        if len(self.tickers) > 5:
            raise ValueError("compare_tickers supports a maximum of 5 tickers")
        return self


class GetMarketBreadthInput(BaseModel):
    """Input for `get_market_breadth`."""

    model_config = ConfigDict(extra="forbid")

    exchange: str = Field(
        default="VNINDEX",
        description="Exchange or index symbol (e.g. 'VNINDEX', 'HNX', 'UPCOM', or 'ALL' for all markets).",
    )

    @field_validator("exchange")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.strip().upper()


def rows_as_dicts(rows: list[PriceRow]) -> list[dict]:
    """Validated rows back to plain dicts for the engine."""
    return [row.model_dump(exclude_none=True) for row in rows]
