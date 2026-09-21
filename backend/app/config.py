from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from cryptography.fernet import Fernet


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    environment: Literal["development", "test", "production"] = "development"
    database_url: str = "sqlite:///./gnkalgo.db"
    redis_url: str | None = None
    jwt_secret: str = "development-only-change-this-secret-now"
    jwt_algorithm: str = "HS256"
    access_token_minutes: int = 15
    refresh_token_days: int = 30
    verification_token_hours: int = 24
    reset_token_minutes: int = 30
    field_encryption_key: str | None = None
    api_key_pepper: str = "development-only-change-this-pepper-now"
    frontend_url: str = "http://localhost:3000"
    cors_origins: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["http://localhost:3000"])
    cookie_secure: bool = False
    expose_dev_tokens: bool = False
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_from: str = "security@gnkalgo.com"
    dhan_client_id: str | None = None
    dhan_app_id: str | None = None
    dhan_app_secret: str | None = None
    dhan_redirect_uri: str = "http://localhost:8000/api/v1/brokers/dhan/callback"
    fyers_client_id: str | None = None
    fyers_client_secret: str | None = None
    fyers_redirect_uri: str = "http://localhost:8000/api/v1/brokers/fyers/callback"
    upstox_client_id: str | None = None
    upstox_client_secret: str | None = None
    upstox_redirect_uri: str = "http://localhost:8000/api/v1/brokers/upstox/callback"
    market_ws_ticket_seconds: int = 30
    market_max_subscriptions: int = 200
    market_quote_ttl_seconds: int = 30
    market_feed_health_ttl_seconds: int = Field(default=300, ge=30)
    market_stale_after_seconds: float = Field(default=15.0, gt=0)
    market_redis_prefix: str = "gnk:market"
    market_feed_provider: Literal["disabled", "simulated", "dhan", "broker"] = "disabled"
    market_simulated_interval_seconds: float = 1.0
    market_candle_intervals: str = "60,300,900,3600,86400"
    dhan_market_request_code: Literal[15, 17, 21] = 17
    dhan_market_reconnect_max_seconds: float = 30.0
    trading_mode: Literal["disabled", "paper", "live"] = "disabled"
    trading_live_confirmation: str = ""
    dhan_static_ip_confirmed: bool = False
    trading_max_order_quantity: int = Field(default=1000, ge=1)
    trading_max_order_notional: float = Field(default=500_000, gt=0)
    trading_max_open_orders: int = Field(default=20, ge=1)
    trading_max_absolute_position: int = Field(default=5000, ge=1)
    paper_slippage_bps: float = Field(default=2.0, ge=0, le=100)
    dhan_order_reconcile_seconds: float = Field(default=5.0, ge=1)
    strategy_engine_enabled: bool = False
    strategy_live_confirmation: str = ""
    strategy_poll_seconds: float = Field(default=2.0, ge=1)
    strategy_max_orders_per_user_per_day: int = Field(default=20, ge=1)
    strategy_max_order_attempts_per_minute: int = Field(default=8, ge=1, le=10)
    strategy_max_candle_lag_seconds: float = Field(default=180.0, gt=0)

    @field_validator("cors_origins", mode="before")
    @classmethod
    def split_origins(cls, value):
        return [x.strip() for x in value.split(",") if x.strip()] if isinstance(value, str) else value

    @field_validator("dhan_market_request_code", mode="before")
    @classmethod
    def parse_dhan_market_request_code(cls, value):
        return int(value) if isinstance(value, str) else value

    def validate_production(self) -> None:
        if self.environment != "production":
            return
        for name in ("jwt_secret", "api_key_pepper"):
            value = getattr(self, name)
            if len(value) < 32 or "development-only" in value or "replace-with" in value:
                raise RuntimeError(f"{name.upper()} must be a strong production secret")
        if not self.field_encryption_key:
            raise RuntimeError("FIELD_ENCRYPTION_KEY is required in production")
        try:
            Fernet(self.field_encryption_key.encode())
        except (TypeError, ValueError) as exc:
            raise RuntimeError("FIELD_ENCRYPTION_KEY must be a valid Fernet key") from exc
        if not self.cookie_secure:
            raise RuntimeError("COOKIE_SECURE must be true in production")
        if not self.smtp_host:
            raise RuntimeError("SMTP_HOST is required in production")
        if self.market_feed_provider == "simulated":
            raise RuntimeError("MARKET_FEED_PROVIDER=simulated is not permitted in production")
        if self.trading_mode == "live":
            if self.trading_live_confirmation != "ENABLE_DHAN_LIVE_ORDERS":
                raise RuntimeError("TRADING_LIVE_CONFIRMATION must explicitly enable Dhan live orders")
            if not self.dhan_static_ip_confirmed:
                raise RuntimeError("DHAN_STATIC_IP_CONFIRMED must be true for live order APIs")
            if self.strategy_engine_enabled and self.strategy_live_confirmation != "ENABLE_LIVE_STRATEGIES":
                raise RuntimeError("STRATEGY_LIVE_CONFIRMATION must explicitly enable live strategies")

    @property
    def candle_intervals(self) -> tuple[int, ...]:
        values = tuple(sorted({int(value.strip()) for value in self.market_candle_intervals.split(",") if value.strip()}))
        if not values or any(value <= 0 for value in values):
            raise ValueError("MARKET_CANDLE_INTERVALS must contain positive integers")
        return values


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.validate_production()
    return settings
