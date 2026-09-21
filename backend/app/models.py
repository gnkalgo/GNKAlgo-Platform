import enum
import uuid
from datetime import datetime, timezone
from sqlalchemy import JSON, Boolean, DateTime, Enum, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .database import Base

def utcnow() -> datetime:
    return datetime.now(timezone.utc)

def uuid_str() -> str:
    return str(uuid.uuid4())

class Role(str, enum.Enum):
    ADMIN = "ADMIN"
    RETAIL = "RETAIL"

class BrokerName(str, enum.Enum):
    DHAN = "DHAN"
    FYERS = "FYERS"
    UPSTOX = "UPSTOX"

    @classmethod
    def _missing_(cls, value):
        if isinstance(value, str):
            normalized = value.upper()
            return next((broker for broker in cls if broker.value == normalized), None)
        return None

class BrokerStatus(str, enum.Enum):
    PENDING = "PENDING"
    CONNECTED = "CONNECTED"
    DISCONNECTED = "DISCONNECTED"
    EXPIRED = "EXPIRED"
    ERROR = "ERROR"
    REAUTH_REQUIRED = "REAUTH_REQUIRED"

class ApiKeyStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    REVOKED = "REVOKED"
    EXPIRED = "EXPIRED"

class OrderSide(str, enum.Enum):
    BUY = "BUY"
    SELL = "SELL"

class OrderType(str, enum.Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"

class OrderStatus(str, enum.Enum):
    PENDING = "PENDING"
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_PENDING = "CANCEL_PENDING"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"

class ProductType(str, enum.Enum):
    CNC = "CNC"
    INTRADAY = "INTRADAY"
    MARGIN = "MARGIN"
    MTF = "MTF"

class OrderValidity(str, enum.Enum):
    DAY = "DAY"
    IOC = "IOC"

class StrategyStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    ARCHIVED = "ARCHIVED"
    ERROR = "ERROR"

class StrategyRunStatus(str, enum.Enum):
    STARTED = "STARTED"
    SUCCEEDED = "SUCCEEDED"
    NO_SIGNAL = "NO_SIGNAL"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"

class StrategySignalStatus(str, enum.Enum):
    CREATED = "CREATED"
    ORDER_SUBMITTED = "ORDER_SUBMITTED"
    SUPPRESSED = "SUPPRESSED"
    REJECTED = "REJECTED"

class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(512))
    role: Mapped[Role] = mapped_column(Enum(Role), default=Role.RETAIL, index=True)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    mfa_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    mfa_secret_encrypted: Mapped[str | None] = mapped_column(Text)
    recovery_code_hashes: Mapped[list] = mapped_column(JSON, default=list)
    failed_login_count: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    sessions: Mapped[list["UserSession"]] = relationship(back_populates="user", cascade="all, delete-orphan")

class UserSession(Base):
    __tablename__ = "user_sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    refresh_token_hash: Mapped[str] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(500))
    ip_address: Mapped[str | None] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    user: Mapped[User] = relationship(back_populates="sessions")

class OneTimeToken(Base):
    __tablename__ = "one_time_tokens"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    purpose: Mapped[str] = mapped_column(String(32), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

class OAuthState(Base):
    __tablename__ = "oauth_states"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    broker: Mapped[BrokerName] = mapped_column(Enum(BrokerName))
    state_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    context_encrypted: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

class BrokerConnection(Base):
    __tablename__ = "broker_connections"
    __table_args__ = (UniqueConstraint("user_id", "broker", name="uq_user_broker"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    broker: Mapped[BrokerName] = mapped_column(Enum(BrokerName), index=True)
    broker_client_id: Mapped[str | None] = mapped_column(String(255))
    encrypted_credentials: Mapped[str] = mapped_column(Text)
    status: Mapped[BrokerStatus] = mapped_column(Enum(BrokerStatus), default=BrokerStatus.PENDING)
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

class ApiKey(Base):
    __tablename__ = "api_keys"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    public_id: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    secret_hash: Mapped[str] = mapped_column(String(64))
    scopes: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[ApiKeyStatus] = mapped_column(Enum(ApiKeyStatus), default=ApiKeyStatus.ACTIVE, index=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rotated_from_id: Mapped[str | None] = mapped_column(ForeignKey("api_keys.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True)
    event: Mapped[str] = mapped_column(String(80), index=True)
    target_type: Mapped[str | None] = mapped_column(String(50))
    target_id: Mapped[str | None] = mapped_column(String(64))
    ip_address: Mapped[str | None] = mapped_column(String(64))
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

Index("ix_audit_user_created", AuditLog.user_id, AuditLog.created_at)

class Instrument(Base):
    """Broker-independent security master used by every market-data adapter."""
    __tablename__ = "instruments"
    __table_args__ = (
        UniqueConstraint("exchange", "segment", "symbol", name="uq_instrument_identity"),
        Index("ix_instrument_search", "exchange", "segment", "trading_symbol"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    exchange: Mapped[str] = mapped_column(String(16), index=True)
    segment: Mapped[str] = mapped_column(String(24), index=True)
    symbol: Mapped[str] = mapped_column(String(160))
    trading_symbol: Mapped[str] = mapped_column(String(160), index=True)
    name: Mapped[str | None] = mapped_column(String(255), index=True)
    instrument_type: Mapped[str] = mapped_column(String(32), index=True)
    expiry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    strike: Mapped[float | None] = mapped_column(Float)
    option_type: Mapped[str | None] = mapped_column(String(8))
    lot_size: Mapped[int | None] = mapped_column(Integer)
    tick_size: Mapped[float | None] = mapped_column(Float)
    broker_tokens: Mapped[dict] = mapped_column(JSON, default=dict)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

class MarketCandle(Base):
    """Normalized OHLCV bars. Raw high-volume ticks are deliberately not stored here."""
    __tablename__ = "market_candles"
    __table_args__ = (
        UniqueConstraint("user_id", "instrument_id", "interval_seconds", "start_at", name="uq_market_candle_bucket"),
        Index("ix_market_candle_lookup", "user_id", "instrument_id", "interval_seconds", "start_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    instrument_id: Mapped[str] = mapped_column(ForeignKey("instruments.id", ondelete="CASCADE"), index=True)
    interval_seconds: Mapped[int] = mapped_column(Integer)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    open: Mapped[float] = mapped_column(Float)
    high: Mapped[float] = mapped_column(Float)
    low: Mapped[float] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    volume: Mapped[float] = mapped_column(Float, default=0)
    open_interest: Mapped[float | None] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(24))
    is_complete: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

class MarketWsTicket(Base):
    """Short-lived, single-use credential for a market WebSocket upgrade."""
    __tablename__ = "market_ws_tickets"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    session_id: Mapped[str | None] = mapped_column(ForeignKey("user_sessions.id", ondelete="CASCADE"), index=True)
    api_key_id: Mapped[str | None] = mapped_column(ForeignKey("api_keys.id", ondelete="CASCADE"), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

class TradingOrder(Base):
    """Tenant-owned paper or live order with idempotent client correlation."""
    __tablename__ = "trading_orders"
    __table_args__ = (
        UniqueConstraint("user_id", "client_order_id", name="uq_trading_order_client_id"),
        Index("ix_trading_order_user_status", "user_id", "status", "created_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    instrument_id: Mapped[str] = mapped_column(ForeignKey("instruments.id", ondelete="RESTRICT"), index=True)
    broker_connection_id: Mapped[str | None] = mapped_column(ForeignKey("broker_connections.id", ondelete="SET NULL"), index=True)
    client_order_id: Mapped[str] = mapped_column(String(30))
    broker_order_id: Mapped[str | None] = mapped_column(String(80), index=True)
    mode: Mapped[str] = mapped_column(String(12), index=True)
    side: Mapped[OrderSide] = mapped_column(Enum(OrderSide))
    order_type: Mapped[OrderType] = mapped_column(Enum(OrderType))
    product_type: Mapped[ProductType] = mapped_column(Enum(ProductType))
    validity: Mapped[OrderValidity] = mapped_column(Enum(OrderValidity))
    quantity: Mapped[int] = mapped_column(Integer)
    filled_quantity: Mapped[int] = mapped_column(Integer, default=0)
    limit_price: Mapped[float | None] = mapped_column(Float)
    trigger_price: Mapped[float | None] = mapped_column(Float)
    average_fill_price: Mapped[float | None] = mapped_column(Float)
    status: Mapped[OrderStatus] = mapped_column(Enum(OrderStatus), default=OrderStatus.PENDING, index=True)
    rejection_reason: Mapped[str | None] = mapped_column(String(120))
    version: Mapped[int] = mapped_column(Integer, default=1)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

class TradeExecution(Base):
    __tablename__ = "trade_executions"
    __table_args__ = (
        UniqueConstraint("user_id", "broker_execution_id", name="uq_trade_execution_broker_id"),
        Index("ix_trade_execution_user_time", "user_id", "executed_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    order_id: Mapped[str] = mapped_column(ForeignKey("trading_orders.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    instrument_id: Mapped[str] = mapped_column(ForeignKey("instruments.id", ondelete="RESTRICT"), index=True)
    broker_execution_id: Mapped[str | None] = mapped_column(String(120))
    mode: Mapped[str] = mapped_column(String(12), index=True)
    side: Mapped[OrderSide] = mapped_column(Enum(OrderSide))
    quantity: Mapped[int] = mapped_column(Integer)
    price: Mapped[float] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(24))
    executed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

class TradingPosition(Base):
    __tablename__ = "trading_positions"
    __table_args__ = (
        UniqueConstraint("user_id", "instrument_id", "mode", "product_type", name="uq_trading_position"),
        Index("ix_trading_position_user_mode", "user_id", "mode"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    instrument_id: Mapped[str] = mapped_column(ForeignKey("instruments.id", ondelete="RESTRICT"), index=True)
    mode: Mapped[str] = mapped_column(String(12), index=True)
    product_type: Mapped[ProductType] = mapped_column(Enum(ProductType))
    quantity: Mapped[int] = mapped_column(Integer, default=0)
    average_price: Mapped[float] = mapped_column(Float, default=0)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0)
    last_price: Mapped[float | None] = mapped_column(Float)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

class TradingControl(Base):
    __tablename__ = "trading_controls"
    scope_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True, index=True)
    is_halted: Mapped[bool] = mapped_column(Boolean, default=False)
    reason: Mapped[str | None] = mapped_column(String(255))
    updated_by_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

class Strategy(Base):
    """Versioned, constrained strategy definition. No arbitrary code is stored or executed."""
    __tablename__ = "strategies"
    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_strategy_user_name"),
        Index("ix_strategy_worker", "status", "execution_mode", "updated_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    instrument_id: Mapped[str] = mapped_column(ForeignKey("instruments.id", ondelete="RESTRICT"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    kind: Mapped[str] = mapped_column(String(32), default="SMA_CROSS")
    status: Mapped[StrategyStatus] = mapped_column(Enum(StrategyStatus), default=StrategyStatus.DRAFT, index=True)
    execution_mode: Mapped[str] = mapped_column(String(12), index=True)
    timeframe_seconds: Mapped[int] = mapped_column(Integer)
    fast_period: Mapped[int] = mapped_column(Integer)
    slow_period: Mapped[int] = mapped_column(Integer)
    quantity: Mapped[int] = mapped_column(Integer)
    product_type: Mapped[ProductType] = mapped_column(Enum(ProductType))
    order_type: Mapped[OrderType] = mapped_column(Enum(OrderType))
    limit_offset_bps: Mapped[float] = mapped_column(Float, default=0)
    max_orders_per_day: Mapped[int] = mapped_column(Integer, default=4)
    version: Mapped[int] = mapped_column(Integer, default=1)
    last_evaluated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

class StrategyVersion(Base):
    __tablename__ = "strategy_versions"
    __table_args__ = (UniqueConstraint("strategy_id", "version", name="uq_strategy_version"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    strategy_id: Mapped[str] = mapped_column(ForeignKey("strategies.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    definition_json: Mapped[dict] = mapped_column(JSON)
    created_by_user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

class StrategyRun(Base):
    __tablename__ = "strategy_runs"
    __table_args__ = (
        UniqueConstraint("strategy_id", "candle_start_at", name="uq_strategy_candle_run"),
        Index("ix_strategy_run_user_time", "user_id", "started_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    strategy_id: Mapped[str] = mapped_column(ForeignKey("strategies.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    strategy_version: Mapped[int] = mapped_column(Integer)
    candle_start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[StrategyRunStatus] = mapped_column(Enum(StrategyRunStatus), default=StrategyRunStatus.STARTED)
    diagnostics_json: Mapped[dict] = mapped_column(JSON, default=dict)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

class StrategySignal(Base):
    __tablename__ = "strategy_signals"
    __table_args__ = (
        UniqueConstraint("run_id", name="uq_strategy_signal_run"),
        Index("ix_strategy_signal_user_time", "user_id", "generated_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    strategy_id: Mapped[str] = mapped_column(ForeignKey("strategies.id", ondelete="CASCADE"), index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("strategy_runs.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    instrument_id: Mapped[str] = mapped_column(ForeignKey("instruments.id", ondelete="RESTRICT"), index=True)
    order_id: Mapped[str | None] = mapped_column(ForeignKey("trading_orders.id", ondelete="SET NULL"), unique=True, index=True)
    action: Mapped[OrderSide] = mapped_column(Enum(OrderSide))
    status: Mapped[StrategySignalStatus] = mapped_column(Enum(StrategySignalStatus), index=True)
    quantity: Mapped[int] = mapped_column(Integer)
    reference_price: Mapped[float] = mapped_column(Float)
    client_order_id: Mapped[str] = mapped_column(String(30))
    reason: Mapped[str | None] = mapped_column(String(120))
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
