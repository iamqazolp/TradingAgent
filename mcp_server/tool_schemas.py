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
from data.resample import TimeframeStr, TIMEFRAMES

GroupName = Literal["trend", "momentum", "volatility", "volume_flow", "trade_flow", "value_flow", "foreign_flow"]
Timeframe = Literal["1H", "4H", "1D", "3D", "1W", "1M", "1Y"]

Ticker = Annotated[str, Field(min_length=1, max_length=12, pattern=r"^[A-Za-z0-9]+$")]
IsoDate = Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$")]


def _check_iso(value: str | None) -> str | None:
    if value is None:
        return None
    val = value.split(" ")[0].split("T")[0]
    date.fromisoformat(val)  # raises ValueError on a nonsense date like 2024-13-45
    return value


def _norm_timeframe(value: Any) -> str:
    """Normalize user-supplied timeframe string using the resampler's aliases."""
    if isinstance(value, str):
        from data.resample import normalize_timeframe
        return normalize_timeframe(value)
    return "1D"


class PriceRow(BaseModel):
    """One stored trading bar, as returned by `get_price_data`."""

    model_config = ConfigDict(extra="ignore")

    date: str
    prev_close: float = Field(gt=0, description="previous close, VND")
    open: float | None = Field(default=None, gt=0, description="open price, VND")
    high: float | None = Field(default=None, gt=0, description="high price, VND")
    low: float | None = Field(default=None, gt=0, description="low price, VND")
    close: float = Field(gt=0, description="close price, VND")
    total_trade: int = Field(ge=0, description="matched trade count")
    total_value: float = Field(ge=0, description="matched traded value, VND")
    total_volume: int = Field(ge=0, description="matched traded volume, shares")
    buy_count: int = Field(ge=0, description="buy-side trade count")
    sell_count: int = Field(ge=0, description="sell-side trade count")
    buy_volume: int = Field(ge=0, description="buy-side volume, shares")
    sell_volume: int = Field(ge=0, description="sell-side volume, shares")
    foreign_buy_volume: int = Field(ge=0, description="foreign buy volume, shares")
    foreign_sell_volume: int = Field(ge=0, description="foreign sell volume, shares")
    foreign_buy_value: float = Field(ge=0, description="foreign buy value, VND")
    foreign_sell_value: float = Field(ge=0, description="foreign sell value, VND")
    foreign_room: int = Field(ge=0, description="remaining foreign room, shares")
    ticker: str | None = None

    @field_validator("date")
    @classmethod
    def _valid_date(cls, value: str) -> str:
        return _check_iso(value)  # type: ignore[return-value]


class RowSource(BaseModel):
    """Common shape for tools that can either take raw rows or load by ticker.

    The model can pass rows it already has from `get_price_data`, or use the
    equivalent alternative that loads the same rows server-side.
    """

    model_config = ConfigDict(extra="forbid")

    rows: list[PriceRow] | None = None
    ticker: Ticker | None = None
    lookback_days: int = Field(default=300, ge=2, le=5000)
    timeframe: Timeframe = Field(default="1D", description="timeframe: 1H, 4H, 1D, 3D, 1W, 1M, 1Y")

    @field_validator("timeframe", mode="before")
    @classmethod
    def _norm_tf(cls, value: Any) -> str:
        return _norm_timeframe(value)

    @model_validator(mode="after")
    def _one_source(self):
        if not self.rows and not self.ticker:
            raise ValueError("supply either `rows` (from get_price_data) or `ticker`")
        if self.rows and self.ticker:
            raise ValueError("supply `rows` or `ticker`, not both")
        if self.rows is not None and len(self.rows) < 2:
            raise ValueError("at least 2 rows are needed to compute anything")
        return self


class GetPriceDataInput(BaseModel):
    """Input for `get_price_data`."""

    model_config = ConfigDict(extra="forbid")

    ticker: Ticker
    lookback_days: int = Field(
        default=300,
        ge=1,
        le=5000,
        description="most recent N trading rows, not calendar days",
    )
    start: IsoDate | None = None
    end: IsoDate | None = None
    timeframe: Timeframe = Field(default="1D", description="timeframe: 1H, 4H, 1D, 3D, 1W, 1M, 1Y")

    @field_validator("timeframe", mode="before")
    @classmethod
    def _norm_tf(cls, value: Any) -> str:
        return _norm_timeframe(value)

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


class ComputeIndicatorsInput(RowSource):
    """Input for `compute_indicators`."""

    groups: list[GroupName] = Field(
        default_factory=lambda: list(GROUPS),
        description=f"subset of: {', '.join(GROUPS)}",
    )
    params: dict[str, Any] | None = Field(
        default=None,
        description=(
            "optional window overrides: sma_windows, ema_windows, macd_fast, "
            "macd_slow, macd_signal, rsi_window, bollinger_window, bollinger_k, "
            "volatility_window, flow_window, trade_size_window, value_window"
        ),
    )
    series_tail: int = Field(
        default=10, ge=0, le=100, description="points of each series to return; 0 for none"
    )

    @field_validator("groups", mode="before")
    @classmethod
    def _known_groups(cls, value: Any) -> list[str]:
        if isinstance(value, str):
            value = [g.strip() for g in value.split(",") if g.strip()]
        elif not isinstance(value, (list, tuple)):
            value = [value]
        unknown = [g for g in value if g not in GROUPS]
        if unknown:
            raise ValueError(
                f"unknown group(s): {', '.join(unknown)}; valid: {', '.join(GROUPS)}"
            )
        return list(dict.fromkeys(value)) or list(GROUPS)


class GetFlowSummaryInput(RowSource):
    """Input for `get_flow_summary`."""

    window: int = Field(default=5, ge=2, le=250, description="rolling window in trading days")


def rows_as_dicts(rows: list[PriceRow]) -> list[dict]:
    """Validated rows back to plain dicts for the engine."""
    return [row.model_dump(exclude_none=True) for row in rows]
