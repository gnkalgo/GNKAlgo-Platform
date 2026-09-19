import base64
import hashlib
import hmac
import json
import re
import secrets
from datetime import datetime, timedelta, timezone
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken
import jwt
import pyotp
from .config import get_settings

settings = get_settings()
password_hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4)

def utcnow() -> datetime:
    return datetime.now(timezone.utc)

def validate_password(password: str) -> None:
    if len(password) < 12 or not re.search(r"[A-Z]", password) or not re.search(r"[a-z]", password) or not re.search(r"\d", password) or not re.search(r"[^A-Za-z0-9]", password):
        raise ValueError("Password must be 12+ characters and include upper, lower, number, and symbol")

def hash_password(value: str) -> str:
    return password_hasher.hash(value)

def verify_password(value: str, encoded: str) -> bool:
    try:
        return password_hasher.verify(encoded, value)
    except (VerifyMismatchError, InvalidHashError):
        return False

def hash_token(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()

def _fernet() -> Fernet:
    if settings.field_encryption_key:
        key = settings.field_encryption_key.encode()
    elif settings.environment in {"development", "test"}:
        key = base64.urlsafe_b64encode(hashlib.sha256(settings.jwt_secret.encode()).digest())
    else:
        raise RuntimeError("FIELD_ENCRYPTION_KEY is required")
    return Fernet(key)

def encrypt_text(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()

def decrypt_text(value: str) -> str:
    try:
        return _fernet().decrypt(value.encode()).decode()
    except InvalidToken as exc:
        raise ValueError("Encrypted value cannot be decrypted") from exc

def encrypt_json(value: dict) -> str:
    return encrypt_text(json.dumps(value, separators=(",", ":")))

def decrypt_json(value: str) -> dict:
    return json.loads(decrypt_text(value))

def create_access_token(user_id: str, session_id: str, role: str) -> str:
    now = utcnow()
    return jwt.encode({"sub": user_id, "sid": session_id, "role": role, "typ": "access", "iat": now, "exp": now + timedelta(minutes=settings.access_token_minutes)}, settings.jwt_secret, algorithm=settings.jwt_algorithm)

def decode_access_token(token: str) -> dict:
    payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm], options={"require": ["exp", "iat", "sub", "sid", "typ"]})
    if payload.get("typ") != "access": raise jwt.InvalidTokenError("wrong token type")
    return payload

def new_refresh_token(session_id: str) -> tuple[str, str]:
    secret = secrets.token_urlsafe(48)
    return f"{session_id}.{secret}", hash_token(secret)

def parse_refresh_token(token: str) -> tuple[str, str]:
    try: return tuple(token.split(".", 1))
    except Exception as exc: raise ValueError("Invalid refresh token") from exc

def new_one_time_token() -> tuple[str, str]:
    raw = secrets.token_urlsafe(32)
    return raw, hash_token(raw)

def new_api_key() -> tuple[str, str, str]:
    public_id, secret = secrets.token_hex(8), secrets.token_urlsafe(32)
    full = f"gnk_live_{public_id}.{secret}"
    digest = hmac.new(settings.api_key_pepper.encode(), secret.encode(), hashlib.sha256).hexdigest()
    return full, public_id, digest

def verify_api_secret(secret: str, expected: str) -> bool:
    actual = hmac.new(settings.api_key_pepper.encode(), secret.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(actual, expected)

def new_totp_secret() -> str:
    return pyotp.random_base32()

def verify_totp(secret: str, code: str) -> bool:
    return pyotp.TOTP(secret).verify(code, valid_window=1)

def new_recovery_codes() -> tuple[list[str], list[str]]:
    codes = [secrets.token_hex(5).upper() for _ in range(8)]
    return codes, [hash_token(code) for code in codes]

def consume_recovery_code(code: str, hashes: list[str]) -> list[str] | None:
    digest = hash_token(code.upper())
    if digest not in hashes: return None
    return [item for item in hashes if item != digest]
