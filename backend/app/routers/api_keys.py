from datetime import timezone
from fastapi import APIRouter, Depends, HTTPException, Request, Security
from sqlalchemy import select
from sqlalchemy.orm import Session
from ..audit import audit
from ..database import get_db
from ..dependencies import Principal, get_principal, require_session
from ..models import ApiKey, ApiKeyStatus, Role
from ..schemas import ApiKeyCreate, ApiKeyCreated, ApiKeyOut, Message
from ..security import new_api_key, utcnow

router = APIRouter(prefix="/api-keys", tags=["api keys"])
RETAIL_SCOPES = {"profile:read", "broker:read", "broker:write", "account:read", "market:read", "orders:read", "strategies:read"}
ALL_SCOPES = RETAIL_SCOPES | {"orders:write", "strategies:write", "admin:read", "admin:write"}

def _owned(db: Session, key_id: str, principal: Principal) -> ApiKey:
    key = db.get(ApiKey, key_id)
    if not key or key.user_id != principal.user.id: raise HTTPException(status_code=404, detail="API key not found")
    return key

@router.get("", response_model=list[ApiKeyOut])
def list_keys(principal: Principal = Depends(require_session), db: Session = Depends(get_db)):
    return db.scalars(select(ApiKey).where(ApiKey.user_id == principal.user.id).order_by(ApiKey.created_at.desc())).all()

@router.post("", response_model=ApiKeyCreated, status_code=201)
def create_key(payload: ApiKeyCreate, request: Request, principal: Principal = Depends(require_session), db: Session = Depends(get_db)):
    allowed = ALL_SCOPES if principal.user.role == Role.ADMIN else RETAIL_SCOPES
    scopes = sorted(set(payload.scopes))
    if not set(scopes) <= allowed: raise HTTPException(status_code=403, detail="One or more scopes are not permitted")
    if payload.expires_at:
        expires = payload.expires_at if payload.expires_at.tzinfo else payload.expires_at.replace(tzinfo=timezone.utc)
        if expires <= utcnow(): raise HTTPException(status_code=422, detail="Expiration must be in the future")
    full, public_id, digest = new_api_key()
    key = ApiKey(user_id=principal.user.id, name=payload.name.strip(), public_id=public_id, secret_hash=digest, scopes=scopes, expires_at=payload.expires_at)
    db.add(key); db.flush(); audit(db, "API_KEY_CREATED", principal.user.id, request, "api_key", key.id, {"scopes": scopes}); db.commit()
    return ApiKeyCreated(**ApiKeyOut.model_validate(key).model_dump(), key=full)

@router.get("/validate/profile", response_model=dict)
def validate_scoped_key(principal: Principal = Security(get_principal, scopes=["profile:read"])):
    return {"user_id": principal.user.id, "credential": "api_key" if principal.api_key_id else "session"}

@router.get("/{key_id}", response_model=ApiKeyOut)
def get_key(key_id: str, principal: Principal = Depends(require_session), db: Session = Depends(get_db)):
    return _owned(db, key_id, principal)

@router.post("/{key_id}/rotate", response_model=ApiKeyCreated)
def rotate(key_id: str, request: Request, principal: Principal = Depends(require_session), db: Session = Depends(get_db)):
    old = _owned(db, key_id, principal)
    if old.status != ApiKeyStatus.ACTIVE: raise HTTPException(status_code=409, detail="Only active keys can be rotated")
    full, public_id, digest = new_api_key()
    new = ApiKey(user_id=old.user_id, name=old.name, public_id=public_id, secret_hash=digest, scopes=old.scopes, expires_at=old.expires_at, rotated_from_id=old.id)
    old.status = ApiKeyStatus.REVOKED; old.revoked_at = utcnow(); db.add(new); db.flush()
    audit(db, "API_KEY_ROTATED", principal.user.id, request, "api_key", new.id, {"rotated_from": old.id}); db.commit()
    return ApiKeyCreated(**ApiKeyOut.model_validate(new).model_dump(), key=full)

@router.post("/{key_id}/revoke", response_model=Message)
def revoke(key_id: str, request: Request, principal: Principal = Depends(require_session), db: Session = Depends(get_db)):
    key = _owned(db, key_id, principal)
    if key.status == ApiKeyStatus.ACTIVE: key.status = ApiKeyStatus.REVOKED; key.revoked_at = utcnow()
    audit(db, "API_KEY_REVOKED", principal.user.id, request, "api_key", key.id); db.commit(); return Message(message="API key revoked")

@router.delete("/{key_id}", response_model=Message)
def delete(key_id: str, principal: Principal = Depends(require_session), db: Session = Depends(get_db)):
    key = _owned(db, key_id, principal)
    if key.status == ApiKeyStatus.ACTIVE: raise HTTPException(status_code=409, detail="Revoke the key before deletion")
    db.delete(key); db.commit(); return Message(message="API key deleted")
