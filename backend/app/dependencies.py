from dataclasses import dataclass
from datetime import datetime, timezone
from fastapi import Depends, HTTPException, Security, status
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer, SecurityScopes
from sqlalchemy import select
from sqlalchemy.orm import Session
import jwt
from .database import get_db
from .models import ApiKey, ApiKeyStatus, AuditLog, Role, User, UserSession
from .security import decode_access_token, verify_api_secret

bearer = HTTPBearer(auto_error=False)
api_header = APIKeyHeader(name="X-GnK-API-Key", auto_error=False)

@dataclass
class Principal:
    user: User
    session_id: str | None = None
    api_key_id: str | None = None
    scopes: set[str] | None = None

def _aware(value):
    if value is None or value.tzinfo: return value
    return value.replace(tzinfo=timezone.utc)

def get_principal(security_scopes: SecurityScopes, credentials: HTTPAuthorizationCredentials | None = Depends(bearer), raw_api_key: str | None = Depends(api_header), db: Session = Depends(get_db)) -> Principal:
    now = datetime.now(timezone.utc)
    if raw_api_key:
        try:
            prefix, secret = raw_api_key.split(".", 1)
            public_id = prefix.removeprefix("gnk_live_")
        except ValueError:
            raise HTTPException(status_code=401, detail="Invalid credentials")
        key = db.scalar(select(ApiKey).where(ApiKey.public_id == public_id))
        if key and key.expires_at and _aware(key.expires_at) <= now and key.status == ApiKeyStatus.ACTIVE:
            key.status = ApiKeyStatus.EXPIRED
            db.add(AuditLog(user_id=key.user_id, event="API_KEY_EXPIRED", target_type="api_key", target_id=key.id, metadata_json={}))
            db.commit()
        if not key or key.status != ApiKeyStatus.ACTIVE or not verify_api_secret(secret, key.secret_hash):
            raise HTTPException(status_code=401, detail="Invalid credentials")
        missing = set(security_scopes.scopes) - set(key.scopes)
        if missing: raise HTTPException(status_code=403, detail="Insufficient API key scope")
        user = db.get(User, key.user_id)
        if not user or not user.is_active: raise HTTPException(status_code=401, detail="Invalid credentials")
        key.last_used_at = now
        db.add(AuditLog(user_id=key.user_id, event="API_KEY_USED", target_type="api_key", target_id=key.id, metadata_json={"scopes_requested": security_scopes.scopes}))
        db.commit()
        return Principal(user=user, api_key_id=key.id, scopes=set(key.scopes))
    if not credentials:
        raise HTTPException(status_code=401, detail="Authentication required")
    try:
        payload = decode_access_token(credentials.credentials)
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired credentials")
    session = db.get(UserSession, payload["sid"])
    user = db.get(User, payload["sub"])
    if not session or session.user_id != payload["sub"] or session.revoked_at or _aware(session.expires_at) <= now or not user or not user.is_active:
        raise HTTPException(status_code=401, detail="Session is expired or revoked")
    session.last_seen_at = now
    db.commit()
    return Principal(user=user, session_id=session.id)

def current_user(principal: Principal = Security(get_principal, scopes=[])) -> User:
    return principal.user

def require_admin(principal: Principal = Security(get_principal, scopes=[])) -> User:
    if principal.api_key_id: raise HTTPException(status_code=403, detail="Administrative actions require an interactive session")
    if principal.user.role != Role.ADMIN: raise HTTPException(status_code=403, detail="Administrator access required")
    return principal.user

def require_session(principal: Principal = Security(get_principal, scopes=[])) -> Principal:
    if not principal.session_id: raise HTTPException(status_code=403, detail="An interactive session is required")
    return principal
