from fastapi import APIRouter, Depends, Security
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session
from ..database import get_db
from ..dependencies import Principal, get_principal, require_session
from ..models import UserSession
from ..schemas import ChangePasswordRequest, Message, SecurityOut, UserOut
from ..security import hash_password, utcnow, validate_password, verify_password
from ..audit import audit
from fastapi import HTTPException, Request

router = APIRouter(prefix="/users", tags=["users"])

@router.get("/me", response_model=UserOut)
def me(principal: Principal = Security(get_principal, scopes=["profile:read"])):
    return principal.user

@router.get("/me/security", response_model=SecurityOut)
def security(principal: Principal = Depends(require_session), db: Session = Depends(get_db)):
    count = db.scalar(select(func.count()).select_from(UserSession).where(UserSession.user_id == principal.user.id, UserSession.revoked_at.is_(None), UserSession.expires_at > utcnow()))
    return SecurityOut(mfa_enabled=principal.user.mfa_enabled, active_sessions=count)

@router.post("/me/password", response_model=Message)
def change_password(payload: ChangePasswordRequest, request: Request, principal: Principal = Depends(require_session), db: Session = Depends(get_db)):
    if not verify_password(payload.current_password, principal.user.password_hash): raise HTTPException(status_code=401, detail="Invalid credentials")
    try: validate_password(payload.new_password)
    except ValueError as exc: raise HTTPException(status_code=422, detail=str(exc))
    principal.user.password_hash = hash_password(payload.new_password)
    db.execute(update(UserSession).where(UserSession.user_id == principal.user.id, UserSession.id != principal.session_id, UserSession.revoked_at.is_(None)).values(revoked_at=utcnow()))
    audit(db, "PASSWORD_CHANGED", principal.user.id, request); db.commit()
    return Message(message="Password changed; other sessions were revoked")
