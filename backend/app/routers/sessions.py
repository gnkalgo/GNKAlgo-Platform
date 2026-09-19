from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session
from ..audit import audit
from ..database import get_db
from ..dependencies import Principal, require_session
from ..models import UserSession
from ..schemas import Message, SessionOut
from ..security import utcnow

router = APIRouter(prefix="/sessions", tags=["sessions"])

@router.get("", response_model=list[SessionOut])
def list_sessions(principal: Principal = Depends(require_session), db: Session = Depends(get_db)):
    rows = db.scalars(select(UserSession).where(UserSession.user_id == principal.user.id, UserSession.revoked_at.is_(None)).order_by(UserSession.created_at.desc())).all()
    return [SessionOut.model_validate(x).model_copy(update={"current": x.id == principal.session_id}) for x in rows]

@router.post("/{session_id}/revoke", response_model=Message)
def revoke(session_id: str, request: Request, principal: Principal = Depends(require_session), db: Session = Depends(get_db)):
    session = db.get(UserSession, session_id)
    if not session or session.user_id != principal.user.id: raise HTTPException(status_code=404, detail="Session not found")
    if not session.revoked_at: session.revoked_at = utcnow()
    audit(db, "SESSION_REVOKED", principal.user.id, request, "session", session.id); db.commit()
    return Message(message="Session revoked")
