from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class NormalizedQuote(BaseModel):
    """Versioned, broker-independent quote event."""

    schema_version: Literal[1] = 1
    instrument_id: str
    exchange: str
    segment: str
    trading_symbol: str
    source: str
    mode: Literal["ltp", "quote", "depth"] = "quote"
    sequence: int = Field(ge=0)
    exchange_timestamp: datetime
    received_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    ltp: float = Field(gt=0)
    last_quantity: float | None = Field(default=None, ge=0)
    open: float | None = Field(default=None, ge=0)
    high: float | None = Field(default=None, ge=0)
    low: float | None = Field(default=None, ge=0)
    previous_close: float | None = Field(default=None, ge=0)
    volume: float | None = Field(default=None, ge=0)
    open_interest: float | None = Field(default=None, ge=0)
    bid: float | None = Field(default=None, ge=0)
    ask: float | None = Field(default=None, ge=0)
    depth: dict[str, Any] | None = None
    stale: bool = False

    @field_validator("exchange_timestamp", "received_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("market timestamps must be timezone-aware")
        return value.astimezone(timezone.utc)

    def wire(self) -> str:
        return self.model_dump_json()

    @classmethod
    def from_wire(cls, value: str | bytes) -> "NormalizedQuote":
        return cls.model_validate_json(value)


class SubscriptionCommand(BaseModel):
    action: Literal["subscribe", "unsubscribe"]
    user_id: str
    instrument_ids: list[str]
    mode: Literal["ltp", "quote", "depth"] = "quote"
    client_id: str
    emitted_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
