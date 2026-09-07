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
    """One stored trading day, as returned by `get_price_data`."""

    model_config = ConfigDict(extra="ignore")

    date: IsoDate
    prev_close: float = Field(gt=0, description="previous close, VND")
    close: float = Field(gt=0, description="close, VND")
    total_trade: int = Field(ge=0, description="matched trade count")
    total_value: float = Field(ge=0, description="total traded value, VND")
    total_volume: int = Field(ge=0, description="total traded volume, shares")
    buy_count: int = Field(ge=0)
    sell_count: int = Field(ge=0)
    buy_volume: int = Field(ge=0)
    sell_volume: int = Field(ge=0)
    foreign_buy_volume: int = Field(ge=0)
    foreign_sell_volume: int = Field(ge=0)
    foreign_buy_value: float = Field(ge=0)
    foreign_sell_value: float = Field(ge=0)
    foreign_room: int = Field(ge=0, description="remaining foreign room, shares")
    ticker: str | None = None

    @field_validator("date")
    @classmethod
    def _valid_date(cls, value: str) -> str:
        return _check_iso(value)  # type: ignore[return-value]


class RowSource(BaseModel):
    """Either explicit rows, or a ticker for the server to load them itself.

    The plan's contract passes `rows` in from a prior `get_price_data` call. That
    round trip costs a lot of context for 300 rows, so `ticker` is accepted as an
    equivalent alternative that loads the same rows server-side.
    """

    model_config = ConfigDict(extra="forbid")

    rows: list[PriceRow] | str | None = None
    ticker: Ticker | None = None
    lookback_days: int = Field(
        default=300,
        ge=2,
        le=5000,
        description="most recent N trading rows (default 300). Trend indicators require at least 200 rows for SMA200.",
    )

    @field_validator("rows", mode="before")
    @classmethod
    def _normalize_rows(cls, value: Any) -> list[PriceRow] | None:
        """Normalize stringified None/null/empty from local LLMs (Ollama etc.)."""
        if value is None:
            return None
        if isinstance(value, str):
            v = value.strip().lower()
            if v in ("none", "null", "[]", "''", '""', ""):
                return None
            try:
                import json
                parsed = json.loads(value)
                if isinstance(parsed, list):
                    return parsed if len(parsed) > 0 else None
            except Exception:
                return None
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


class ComputeIndicatorsInput(RowSource):
    """Input for `compute_indicators`."""

    groups: list[GroupName] | str = Field(
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
        default=2, ge=0, le=100, description="points of each series to return; 0 for none"
    )

    @field_validator("groups", mode="before")
    @classmethod
    def _known_groups(cls, value: Any) -> list[str]:
        if isinstance(value, str):
            v = value.strip()
            if (v.startswith("[") and v.endswith("]")) or (v.startswith("(") and v.endswith(")")):
                import ast
                try:
                    parsed = ast.literal_eval(v)
                    if isinstance(parsed, (list, tuple)):
                        v_list = [str(x).strip() for x in parsed]
                    else:
                        v_list = [str(parsed).strip()]
                except Exception:
                    v_clean = v.strip("[]()").replace('"', "").replace("'", "")
                    v_list = [g.strip().strip("'\"") for g in v_clean.split(",") if g.strip().strip("'\"")]
            else:
                v_list = [g.strip().strip("'\"") for g in v.split(",") if g.strip().strip("'\"")]
            value = v_list
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


class GetFlowSummaryInput(RowSource):
    """Input for `get_flow_summary`."""

    window: int = Field(default=5, ge=2, le=250, description="rolling window in trading days")


def rows_as_dicts(rows: list[PriceRow]) -> list[dict]:
    """Validated rows back to plain dicts for the engine."""
    return [row.model_dump(exclude_none=True) for row in rows]
