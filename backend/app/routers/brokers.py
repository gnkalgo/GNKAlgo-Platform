import secrets
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Cookie, Depends, HTTPException, Query, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from ..audit import audit
from ..brokers.adapters import ADAPTERS, BrokerError
from ..config import get_settings
from ..database import get_db
from ..dependencies import Principal, get_principal
from ..models import BrokerConnection, BrokerName, BrokerStatus, OAuthState
from ..schemas import BrokerConnectOut, BrokerOut, Message
from ..security import decrypt_json, encrypt_json, hash_token, utcnow

router = APIRouter(prefix="/brokers", tags=["brokers"])
settings = get_settings()

def _aware(value): return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

def _owned(db: Session, connection_id: str, principal: Principal) -> BrokerConnection:
    connection = db.get(BrokerConnection, connection_id)
    if not connection or connection.user_id != principal.user.id: raise HTTPException(status_code=404, detail="Broker connection not found")
    return connection

def _scope(principal: Principal, required: str) -> None:
    if principal.api_key_id and required not in (principal.scopes or set()):
        raise HTTPException(status_code=403, detail="Insufficient API key scope")

@router.get("", response_model=list[BrokerOut])
def list_connections(principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    _scope(principal, "broker:read")
    return db.scalars(select(BrokerConnection).where(BrokerConnection.user_id == principal.user.id).order_by(BrokerConnection.created_at.desc())).all()

@router.post("/{broker}/connect", response_model=BrokerConnectOut)
async def connect(broker: BrokerName, response: Response, request: Request, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    _scope(principal, "broker:write")
    raw_state = secrets.token_urlsafe(32)
    try: url, context = await ADAPTERS[broker].authorization_url(raw_state)
    except BrokerError as exc: raise HTTPException(status_code=503, detail=exc.code)
    state = OAuthState(user_id=principal.user.id, broker=broker, state_hash=hash_token(raw_state), context_encrypted=encrypt_json(context), expires_at=utcnow() + timedelta(minutes=10))
    db.add(state); audit(db, "BROKER_CONNECT_STARTED", principal.user.id, request, "broker", broker.value); db.commit()
    response.set_cookie("gnk_oauth_state", raw_state, httponly=True, secure=settings.cookie_secure, samesite="lax", max_age=600, path="/api/v1/brokers")
    return BrokerConnectOut(authorization_url=url)

@router.get("/{broker}/callback")
async def callback(broker: BrokerName, request: Request, state: str | None = Query(default=None), code: str | None = Query(default=None), auth_code: str | None = Query(default=None), tokenId: str | None = Query(default=None), gnk_oauth_state: str | None = Cookie(default=None), db: Session = Depends(get_db)):
    raw_state = state or gnk_oauth_state
    auth_code_value = code or auth_code or tokenId
    if not raw_state or not auth_code_value: raise HTTPException(status_code=400, detail="Invalid broker callback")
    saved = db.scalar(select(OAuthState).where(OAuthState.state_hash == hash_token(raw_state), OAuthState.broker == broker))
    if not saved or saved.used_at or _aware(saved.expires_at) <= utcnow(): raise HTTPException(status_code=400, detail="Invalid or expired broker state")
    saved.used_at = utcnow()
    try:
        credentials = await ADAPTERS[broker].exchange(auth_code_value, decrypt_json(saved.context_encrypted or encrypt_json({})))
        await ADAPTERS[broker].test(credentials)
    except BrokerError as exc:
        audit(db, "BROKER_CONNECTION_FAILED", saved.user_id, request, "broker", broker.value, {"error_code": exc.code}); db.commit()
        raise HTTPException(status_code=400, detail="Broker authentication failed")
    existing = db.scalar(select(BrokerConnection).where(BrokerConnection.user_id == saved.user_id, BrokerConnection.broker == broker))
    if not existing:
        existing = BrokerConnection(user_id=saved.user_id, broker=broker, encrypted_credentials=encrypt_json(credentials)); db.add(existing)
    existing.encrypted_credentials = encrypt_json(credentials)
    existing.broker_client_id = credentials.get("client_id")
    existing.status = BrokerStatus.CONNECTED; existing.last_connected_at = utcnow(); existing.last_checked_at = utcnow(); existing.error_code = None
    if credentials.get("expires_at"):
        try: existing.token_expires_at = datetime.fromisoformat(credentials["expires_at"].replace("Z", "+00:00"))
        except ValueError: existing.token_expires_at = None
    db.flush(); audit(db, "BROKER_CONNECTED", saved.user_id, request, "broker_connection", existing.id, {"broker": broker.value}); db.commit()
    response = RedirectResponse(f"{settings.frontend_url}/settings/brokers?connected={broker.value.lower()}", status_code=302)
    response.delete_cookie("gnk_oauth_state", path="/api/v1/brokers")
    return response

@router.get("/{connection_id}", response_model=BrokerOut)
def get_connection(connection_id: str, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    _scope(principal, "broker:read")
    return _owned(db, connection_id, principal)

@router.post("/{connection_id}/test", response_model=BrokerOut)
async def test_connection(connection_id: str, request: Request, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    _scope(principal, "broker:read")
    connection = _owned(db, connection_id, principal)
    if connection.status == BrokerStatus.DISCONNECTED: raise HTTPException(status_code=409, detail="Broker is disconnected")
    try:
        await ADAPTERS[connection.broker].test(decrypt_json(connection.encrypted_credentials))
        connection.status = BrokerStatus.CONNECTED; connection.error_code = None
    except BrokerError as exc:
        connection.status = BrokerStatus.REAUTH_REQUIRED; connection.error_code = exc.code
    connection.last_checked_at = utcnow(); audit(db, "BROKER_CONNECTION_TESTED", principal.user.id, request, "broker_connection", connection.id, {"status": connection.status.value}); db.commit()
    return connection

@router.post("/{connection_id}/reconnect", response_model=BrokerConnectOut)
async def reconnect(connection_id: str, response: Response, request: Request, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    _scope(principal, "broker:write")
    connection = _owned(db, connection_id, principal)
    result = await connect(connection.broker, response, request, principal, db)
    audit(db, "BROKER_RECONNECTED", principal.user.id, request, "broker_connection", connection.id); db.commit()
    return result

@router.delete("/{connection_id}", response_model=Message)
def disconnect(connection_id: str, request: Request, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    _scope(principal, "broker:write")
    connection = _owned(db, connection_id, principal)
    connection.status = BrokerStatus.DISCONNECTED; connection.encrypted_credentials = encrypt_json({}); connection.token_expires_at = None
    audit(db, "BROKER_DISCONNECTED", principal.user.id, request, "broker_connection", connection.id, {"broker": connection.broker.value}); db.commit()
    return Message(message="Broker disconnected")
