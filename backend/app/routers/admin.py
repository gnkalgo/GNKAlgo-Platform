from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session
from ..audit import audit
from ..database import get_db
from ..dependencies import require_admin
from ..models import AuditLog, User
from ..schemas import AdminUserUpdate, AuditOut, UserOut

router = APIRouter(prefix="/admin", tags=["admin"])

@router.get("/users", response_model=list[UserOut])
def users(admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    return db.scalars(select(User).order_by(User.created_at.desc()).limit(500)).all()

@router.patch("/users/{user_id}", response_model=UserOut)
def update_user(user_id: str, payload: AdminUserUpdate, request: Request, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    user = db.get(User, user_id)
    if not user: raise HTTPException(status_code=404, detail="User not found")
    if user.id == admin.id and payload.is_active is False: raise HTTPException(status_code=409, detail="You cannot disable your own account")
    changes = payload.model_dump(exclude_none=True)
    for name, value in changes.items(): setattr(user, name, value)
    audit(db, "ADMIN_USER_UPDATED", admin.id, request, "user", user.id, changes); db.commit(); return user

@router.get("/audit", response_model=list[AuditOut])
def logs(admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    return db.scalars(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(500)).all()
