from datetime import datetime
from typing import Literal
from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator
from .models import (
    ApiKeyStatus, BrokerName, BrokerStatus, OrderSide, OrderStatus, OrderType,
    OrderValidity, ProductType, Role, StrategyRunStatus, StrategySignalStatus, StrategyStatus,
)

class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)

class Message(BaseModel):
    message: str

class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=12, max_length=128)
    password_confirmation: str
    @model_validator(mode="after")
    def match(self):
        if self.password != self.password_confirmation: raise ValueError("Passwords do not match")
        return self

class RegisterResponse(Message):
    verification_token: str | None = None

class TokenRequest(BaseModel):
    token: str = Field(min_length=20, max_length=500)

class LoginRequest(BaseModel):
    email: EmailStr
    password: str
    totp_code: str | None = Field(default=None, min_length=6, max_length=20)

class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int

class RefreshRequest(BaseModel):
    refresh_token: str

class ForgotPasswordRequest(BaseModel):
    email: EmailStr

class ResetPasswordRequest(TokenRequest):
    password: str = Field(min_length=12, max_length=128)
    password_confirmation: str
    @model_validator(mode="after")
    def match(self):
        if self.password != self.password_confirmation: raise ValueError("Passwords do not match")
        return self

class UserOut(ORMModel):
    id: str
    email: EmailStr
    role: Role
    is_verified: bool
    is_active: bool
    mfa_enabled: bool
    created_at: datetime

class SecurityOut(BaseModel):
    mfa_enabled: bool
    active_sessions: int

class MFASetupOut(BaseModel):
    provisioning_uri: str
    secret: str

class MFAVerifyRequest(BaseModel):
    code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")

class MFAEnabledOut(Message):
    recovery_codes: list[str]

class MFADisableRequest(BaseModel):
    password: str
    code: str

class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=12, max_length=128)
    new_password_confirmation: str
    @model_validator(mode="after")
    def match(self):
        if self.new_password != self.new_password_confirmation: raise ValueError("Passwords do not match")
        return self

class SessionOut(ORMModel):
    id: str
    user_agent: str | None
    ip_address: str | None
    expires_at: datetime
    last_seen_at: datetime
    created_at: datetime
    current: bool = False

class BrokerOut(ORMModel):
    id: str
    broker: BrokerName
    broker_client_id: str | None
    status: BrokerStatus
    token_expires_at: datetime | None
    last_connected_at: datetime | None
    last_checked_at: datetime | None
    created_at: datetime

class BrokerConnectOut(BaseModel):
    authorization_url: str
    expires_in: int = 600

class ApiKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    scopes: list[str] = Field(default_factory=list, max_length=20)
    expires_at: datetime | None = None

class ApiKeyOut(ORMModel):
    id: str
    name: str
    public_id: str
    scopes: list[str]
    status: ApiKeyStatus
    expires_at: datetime | None
    last_used_at: datetime | None
    created_at: datetime

class ApiKeyCreated(ApiKeyOut):
    key: str

class AdminUserUpdate(BaseModel):
    role: Role | None = None
    is_active: bool | None = None

class AuditOut(ORMModel):
    id: str
    user_id: str | None
    event: str
    target_type: str | None
    target_id: str | None
    metadata_json: dict
    created_at: datetime

class InstrumentOut(ORMModel):
    id: str
    exchange: str
    segment: str
    symbol: str
    trading_symbol: str
    name: str | None
    instrument_type: str
    expiry_at: datetime | None
    strike: float | None
    option_type: str | None
    lot_size: int | None
    tick_size: float | None

class MarketQuoteOut(BaseModel):
    schema_version: int = 1
    instrument_id: str
    exchange: str
    segment: str
    trading_symbol: str
    source: str
    mode: str
    sequence: int
    exchange_timestamp: datetime
    received_at: datetime
    ltp: float
    last_quantity: float | None = None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    previous_close: float | None = None
    volume: float | None = None
    open_interest: float | None = None
    bid: float | None = None
    ask: float | None = None
    depth: dict | None = None
    stale: bool = False

class MarketCandleOut(ORMModel):
    instrument_id: str
    interval_seconds: int
    start_at: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    open_interest: float | None
    source: str
    is_complete: bool

class MarketWsTicketOut(BaseModel):
    ticket: str
    expires_in: int

class MarketFeedHealthOut(BaseModel):
    provider: str
    state: str
    healthy: bool
    stale: bool
    subscription_count: int
    connected_at: datetime | None = None
    disconnected_at: datetime | None = None
    last_tick_at: datetime | None = None
    last_exchange_timestamp: datetime | None = None
    quote_latency_ms: float | None = None
    tick_count: int = 0
    reconnect_count: int = 0
    decode_errors: int = 0
    last_error: str | None = None
    updated_at: datetime | None = None

class MarketStatusOut(BaseModel):
    provider: str
    redis_connected: bool
    active_clients: int
    active_subscriptions: int
    feed: MarketFeedHealthOut

class OrderCreate(BaseModel):
    instrument_id: str
    client_order_id: str = Field(min_length=1, max_length=30, pattern=r"^[A-Za-z0-9_-]+$")
    side: OrderSide
    order_type: OrderType
    product_type: ProductType = ProductType.INTRADAY
    validity: OrderValidity = OrderValidity.DAY
    quantity: int = Field(ge=1)
    limit_price: float | None = Field(default=None, gt=0)
    trigger_price: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_prices(self):
        if self.order_type == OrderType.LIMIT and self.limit_price is None:
            raise ValueError("limit_price is required for LIMIT orders")
        if self.order_type == OrderType.MARKET and self.limit_price is not None:
            raise ValueError("limit_price is not allowed for MARKET orders")
        return self

class OrderModify(BaseModel):
    quantity: int = Field(ge=1)
    limit_price: float = Field(gt=0)

class TradingOrderOut(ORMModel):
    id: str
    instrument_id: str
    client_order_id: str
    broker_order_id: str | None
    mode: str
    side: OrderSide
    order_type: OrderType
    product_type: ProductType
    validity: OrderValidity
    quantity: int
    filled_quantity: int
    limit_price: float | None
    trigger_price: float | None
    average_fill_price: float | None
    status: OrderStatus
    rejection_reason: str | None
    version: int
    submitted_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime

class TradeExecutionOut(ORMModel):
    id: str
    order_id: str
    instrument_id: str
    broker_execution_id: str | None
    mode: str
    side: OrderSide
    quantity: int
    price: float
    source: str
    executed_at: datetime

class TradingPositionOut(ORMModel):
    id: str
    instrument_id: str
    mode: str
    product_type: ProductType
    quantity: int
    average_price: float
    realized_pnl: float
    last_price: float | None
    unrealized_pnl: float = 0
    updated_at: datetime

class TradingStatusOut(BaseModel):
    mode: str
    live_ready: bool
    halted: bool
    halt_reason: str | None = None
    limits: dict[str, float | int]

class KillSwitchRequest(BaseModel):
    halted: bool
    reason: str | None = Field(default=None, max_length=255)

class KillSwitchOut(BaseModel):
    halted: bool
    reason: str | None

class StrategyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    instrument_id: str
    execution_mode: Literal["PAPER", "LIVE"] = "PAPER"
    timeframe_seconds: int = Field(default=60, ge=60, le=86400)
    fast_period: int = Field(default=5, ge=2, le=200)
    slow_period: int = Field(default=20, ge=3, le=500)
    quantity: int = Field(default=1, ge=1)
    product_type: ProductType = ProductType.INTRADAY
    order_type: OrderType = OrderType.MARKET
    limit_offset_bps: float = Field(default=0, ge=0, le=100)
    max_orders_per_day: int = Field(default=4, ge=1, le=100)

    @model_validator(mode="after")
    def periods_are_ordered(self):
        if self.fast_period >= self.slow_period:
            raise ValueError("fast_period must be less than slow_period")
        return self

class StrategyUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    timeframe_seconds: int | None = Field(default=None, ge=60, le=86400)
    fast_period: int | None = Field(default=None, ge=2, le=200)
    slow_period: int | None = Field(default=None, ge=3, le=500)
    quantity: int | None = Field(default=None, ge=1)
    product_type: ProductType | None = None
    order_type: OrderType | None = None
    limit_offset_bps: float | None = Field(default=None, ge=0, le=100)
    max_orders_per_day: int | None = Field(default=None, ge=1, le=100)

class StrategyActivate(BaseModel):
    confirmation: str | None = Field(default=None, max_length=80)

class StrategyOut(ORMModel):
    id: str
    instrument_id: str
    name: str
    kind: str
    status: StrategyStatus
    execution_mode: str
    timeframe_seconds: int
    fast_period: int
    slow_period: int
    quantity: int
    product_type: ProductType
    order_type: OrderType
    limit_offset_bps: float
    max_orders_per_day: int
    version: int
    last_evaluated_at: datetime | None
    activated_at: datetime | None
    paused_at: datetime | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime

class StrategyVersionOut(ORMModel):
    id: str
    strategy_id: str
    version: int
    definition_json: dict
    created_at: datetime

class StrategyRunOut(ORMModel):
    id: str
    strategy_id: str
    strategy_version: int
    candle_start_at: datetime
    status: StrategyRunStatus
    diagnostics_json: dict
    started_at: datetime
    finished_at: datetime | None

class StrategySignalOut(ORMModel):
    id: str
    strategy_id: str
    run_id: str
    instrument_id: str
    order_id: str | None
    action: OrderSide
    status: StrategySignalStatus
    quantity: int
    reference_price: float
    client_order_id: str
    reason: str | None
    generated_at: datetime

class StrategyPreviewOut(BaseModel):
    candle_start_at: datetime
    action: OrderSide
    close: float
    fast_average: float
    slow_average: float
