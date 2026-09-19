from fastapi import Request
from sqlalchemy.orm import Session
from .models import AuditLog

SENSITIVE_KEYS = {"password", "token", "secret", "authorization", "refresh_token", "access_token", "totp"}

def _sanitize(value):
    if isinstance(value, dict):
        return {k: "[REDACTED]" if any(s in k.lower() for s in SENSITIVE_KEYS) else _sanitize(v) for k, v in value.items()}
    if isinstance(value, list): return [_sanitize(x) for x in value]
    return value

def audit(db: Session, event: str, user_id: str | None = None, request: Request | None = None, target_type: str | None = None, target_id: str | None = None, metadata: dict | None = None) -> None:
    ip = request.client.host if request and request.client else None
    db.add(AuditLog(user_id=user_id, event=event, target_type=target_type, target_id=target_id, ip_address=ip, metadata_json=_sanitize(metadata or {})))
