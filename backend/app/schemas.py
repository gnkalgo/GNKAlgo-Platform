from datetime import datetime
from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator
from .models import ApiKeyStatus, BrokerName, BrokerStatus, Role

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

class MarketStatusOut(BaseModel):
    provider: str
    redis_connected: bool
    active_clients: int
    active_subscriptions: int
