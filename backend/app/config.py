from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


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
    dhan_app_id: str | None = None
    dhan_app_secret: str | None = None
    dhan_redirect_uri: str = "http://localhost:8000/api/v1/brokers/dhan/callback"
    fyers_client_id: str | None = None
    fyers_client_secret: str | None = None
    fyers_redirect_uri: str = "http://localhost:8000/api/v1/brokers/fyers/callback"
    upstox_client_id: str | None = None
    upstox_client_secret: str | None = None
    upstox_redirect_uri: str = "http://localhost:8000/api/v1/brokers/upstox/callback"

    @field_validator("cors_origins", mode="before")
    @classmethod
    def split_origins(cls, value):
        return [x.strip() for x in value.split(",") if x.strip()] if isinstance(value, str) else value

    def validate_production(self) -> None:
        if self.environment != "production":
            return
        for name in ("jwt_secret", "api_key_pepper"):
            value = getattr(self, name)
            if len(value) < 32 or "development-only" in value or "replace-with" in value:
                raise RuntimeError(f"{name.upper()} must be a strong production secret")
        if not self.field_encryption_key:
            raise RuntimeError("FIELD_ENCRYPTION_KEY is required in production")
        if not self.cookie_secure:
            raise RuntimeError("COOKIE_SECURE must be true in production")
        if not self.smtp_host:
            raise RuntimeError("SMTP_HOST is required in production")


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.validate_production()
    return settings
