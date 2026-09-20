from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select, update
from sqlalchemy.orm import Session
import pyotp
from ..audit import audit
from ..config import get_settings
from ..database import get_db
from ..dependencies import Principal, get_principal, require_session
from ..models import OneTimeToken, User, UserSession
from ..mailer import MailDeliveryError, send_password_reset, send_verification
from ..rate_limit import limit_login, limit_sensitive
from ..schemas import ForgotPasswordRequest, LoginRequest, Message, MFADisableRequest, MFAEnabledOut, MFASetupOut, MFAVerifyRequest, RefreshRequest, RegisterRequest, RegisterResponse, ResetPasswordRequest, TokenPair, TokenRequest
from ..security import consume_recovery_code, create_access_token, decrypt_text, encrypt_text, hash_password, hash_token, new_one_time_token, new_recovery_codes, new_refresh_token, new_totp_secret, parse_refresh_token, utcnow, validate_password, verify_password, verify_totp

router = APIRouter(prefix="/auth", tags=["authentication"])
settings = get_settings()

def _aware(value):
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

def _issue(db: Session, user: User, request: Request) -> TokenPair:
    expires = utcnow() + timedelta(days=settings.refresh_token_days)
    session = UserSession(user_id=user.id, refresh_token_hash="pending", expires_at=expires, user_agent=request.headers.get("user-agent", "")[:500], ip_address=request.client.host if request.client else None)
    db.add(session); db.flush()
    refresh, digest = new_refresh_token(session.id)
    session.refresh_token_hash = digest
    access = create_access_token(user.id, session.id, user.role.value)
    return TokenPair(access_token=access, refresh_token=refresh, expires_in=settings.access_token_minutes * 60)

@router.post("/register", response_model=RegisterResponse, status_code=201)
def register(payload: RegisterRequest, request: Request, db: Session = Depends(get_db)):
    limit_sensitive(request)
    email = payload.email.lower().strip()
    if db.scalar(select(User).where(User.email == email)):
        raise HTTPException(status_code=409, detail="An account with this email already exists")
    try: validate_password(payload.password)
    except ValueError as exc: raise HTTPException(status_code=422, detail=str(exc))
    user = User(email=email, password_hash=hash_password(payload.password))
    db.add(user); db.flush()
    raw, digest = new_one_time_token()
    db.add(OneTimeToken(user_id=user.id, purpose="VERIFY_EMAIL", token_hash=digest, expires_at=utcnow() + timedelta(hours=settings.verification_token_hours)))
    audit(db, "USER_REGISTERED", user.id, request)
    try:
        send_verification(user.email, raw)
        db.commit()
    except MailDeliveryError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail="Verification email is temporarily unavailable. Please try again shortly.") from exc
    return RegisterResponse(message="Registration accepted. Check your email to verify the account.", verification_token=raw if settings.expose_dev_tokens else None)

@router.post("/verify-email", response_model=Message)
def verify_email(payload: TokenRequest, request: Request, db: Session = Depends(get_db)):
    token = db.scalar(select(OneTimeToken).where(OneTimeToken.token_hash == hash_token(payload.token), OneTimeToken.purpose == "VERIFY_EMAIL"))
    if not token or token.used_at or _aware(token.expires_at) <= utcnow(): raise HTTPException(status_code=400, detail="Invalid or expired verification link")
    user = db.get(User, token.user_id); user.is_verified = True; token.used_at = utcnow()
    audit(db, "EMAIL_VERIFIED", user.id, request); db.commit()
    return Message(message="Email verified")

@router.post("/login", response_model=TokenPair)
def login(payload: LoginRequest, request: Request, db: Session = Depends(get_db)):
    limit_login(request)
    user = db.scalar(select(User).where(User.email == payload.email.lower().strip()))
    generic = HTTPException(status_code=401, detail="Invalid credentials")
    if not user: raise generic
    if user.locked_until and _aware(user.locked_until) > utcnow(): raise HTTPException(status_code=423, detail="Account is temporarily locked")
    if not verify_password(payload.password, user.password_hash):
        user.failed_login_count += 1
        if user.failed_login_count >= 5:
            user.locked_until = utcnow() + timedelta(minutes=15); user.failed_login_count = 0
        audit(db, "LOGIN_FAILED", user.id, request, metadata={"reason": "invalid_credentials"}); db.commit(); raise generic
    if not user.is_active: raise HTTPException(status_code=403, detail="Account is unavailable")
    if not user.is_verified: raise HTTPException(status_code=403, detail="Email verification required")
    if user.mfa_enabled:
        if not payload.totp_code: raise HTTPException(status_code=401, detail="MFA_REQUIRED")
        secret = decrypt_text(user.mfa_secret_encrypted)
        if not verify_totp(secret, payload.totp_code):
            remaining = consume_recovery_code(payload.totp_code, user.recovery_code_hashes or [])
            if remaining is None:
                audit(db, "LOGIN_FAILED", user.id, request, metadata={"reason": "invalid_mfa"}); db.commit(); raise generic
            user.recovery_code_hashes = remaining
    user.failed_login_count = 0; user.locked_until = None
    tokens = _issue(db, user, request); audit(db, "LOGIN_SUCCEEDED", user.id, request); db.commit()
    return tokens

@router.post("/refresh", response_model=TokenPair)
def refresh(payload: RefreshRequest, request: Request, db: Session = Depends(get_db)):
    try: session_id, secret = parse_refresh_token(payload.refresh_token)
    except ValueError: raise HTTPException(status_code=401, detail="Invalid credentials")
    session = db.get(UserSession, session_id)
    if not session or session.revoked_at or _aware(session.expires_at) <= utcnow() or hash_token(secret) != session.refresh_token_hash:
        if session and not session.revoked_at:
            session.revoked_at = utcnow(); audit(db, "REFRESH_TOKEN_REUSE_DETECTED", session.user_id, request); db.commit()
        raise HTTPException(status_code=401, detail="Invalid credentials")
    user = db.get(User, session.user_id)
    if not user or not user.is_active: raise HTTPException(status_code=401, detail="Invalid credentials")
    refresh_value, digest = new_refresh_token(session.id); session.refresh_token_hash = digest; session.last_seen_at = utcnow()
    access = create_access_token(user.id, session.id, user.role.value); db.commit()
    return TokenPair(access_token=access, refresh_token=refresh_value, expires_in=settings.access_token_minutes * 60)

@router.post("/logout", response_model=Message)
def logout(request: Request, principal: Principal = Depends(require_session), db: Session = Depends(get_db)):
    if principal.session_id:
        db.get(UserSession, principal.session_id).revoked_at = utcnow()
        audit(db, "LOGOUT", principal.user.id, request); db.commit()
    return Message(message="Logged out")

@router.post("/logout-all", response_model=Message)
def logout_all(request: Request, principal: Principal = Depends(require_session), db: Session = Depends(get_db)):
    db.execute(update(UserSession).where(UserSession.user_id == principal.user.id, UserSession.revoked_at.is_(None)).values(revoked_at=utcnow()))
    audit(db, "LOGOUT_ALL", principal.user.id, request); db.commit()
    return Message(message="All sessions revoked")

@router.post("/forgot-password", response_model=RegisterResponse)
def forgot(payload: ForgotPasswordRequest, request: Request, db: Session = Depends(get_db)):
    limit_sensitive(request); raw = None
    user = db.scalar(select(User).where(User.email == payload.email.lower().strip()))
    if user:
        raw, digest = new_one_time_token()
        db.add(OneTimeToken(user_id=user.id, purpose="RESET_PASSWORD", token_hash=digest, expires_at=utcnow() + timedelta(minutes=settings.reset_token_minutes)))
        audit(db, "PASSWORD_RESET_REQUESTED", user.id, request)
        try:
            send_password_reset(user.email, raw)
            db.commit()
        except MailDeliveryError:
            db.rollback()
            raw = None
    return RegisterResponse(message="If the account exists, a reset link has been sent.", verification_token=raw if raw and settings.expose_dev_tokens else None)

@router.post("/reset-password", response_model=Message)
def reset(payload: ResetPasswordRequest, request: Request, db: Session = Depends(get_db)):
    try: validate_password(payload.password)
    except ValueError as exc: raise HTTPException(status_code=422, detail=str(exc))
    token = db.scalar(select(OneTimeToken).where(OneTimeToken.token_hash == hash_token(payload.token), OneTimeToken.purpose == "RESET_PASSWORD"))
    if not token or token.used_at or _aware(token.expires_at) <= utcnow(): raise HTTPException(status_code=400, detail="Invalid or expired reset link")
    user = db.get(User, token.user_id); user.password_hash = hash_password(payload.password); token.used_at = utcnow()
    db.execute(update(UserSession).where(UserSession.user_id == user.id, UserSession.revoked_at.is_(None)).values(revoked_at=utcnow()))
    audit(db, "PASSWORD_RESET", user.id, request); db.commit(); return Message(message="Password reset")

@router.post("/mfa/setup", response_model=MFASetupOut)
def mfa_setup(principal: Principal = Depends(require_session), db: Session = Depends(get_db)):
    if principal.user.mfa_enabled: raise HTTPException(status_code=409, detail="MFA is already enabled")
    secret = new_totp_secret(); principal.user.mfa_secret_encrypted = encrypt_text(secret); db.commit()
    return MFASetupOut(provisioning_uri=pyotp.TOTP(secret).provisioning_uri(name=principal.user.email, issuer_name="GnKAlgo"), secret=secret)

@router.post("/mfa/verify", response_model=MFAEnabledOut)
def mfa_verify(payload: MFAVerifyRequest, request: Request, principal: Principal = Depends(require_session), db: Session = Depends(get_db)):
    if not principal.user.mfa_secret_encrypted or not verify_totp(decrypt_text(principal.user.mfa_secret_encrypted), payload.code): raise HTTPException(status_code=400, detail="Invalid MFA code")
    codes, hashes = new_recovery_codes(); principal.user.mfa_enabled = True; principal.user.recovery_code_hashes = hashes
    audit(db, "MFA_ENABLED", principal.user.id, request); db.commit()
    return MFAEnabledOut(message="MFA enabled. Store recovery codes securely.", recovery_codes=codes)

@router.post("/mfa/disable", response_model=Message)
def mfa_disable(payload: MFADisableRequest, request: Request, principal: Principal = Depends(require_session), db: Session = Depends(get_db)):
    user = principal.user
    if not verify_password(payload.password, user.password_hash): raise HTTPException(status_code=401, detail="Invalid credentials")
    valid = user.mfa_secret_encrypted and verify_totp(decrypt_text(user.mfa_secret_encrypted), payload.code)
    remaining = None if valid else consume_recovery_code(payload.code, user.recovery_code_hashes or [])
    if not valid and remaining is None: raise HTTPException(status_code=401, detail="Invalid credentials")
    user.mfa_enabled = False; user.mfa_secret_encrypted = None; user.recovery_code_hashes = []
    audit(db, "MFA_DISABLED", user.id, request); db.commit(); return Message(message="MFA disabled")
