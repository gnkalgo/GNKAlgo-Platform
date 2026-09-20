import asyncio
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Security, WebSocket, WebSocketDisconnect
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..audit import audit
from ..config import get_settings
from ..database import get_db
from ..dependencies import Principal, get_principal
from ..market.bus import market_bus
from ..models import ApiKey, ApiKeyStatus, Instrument, MarketCandle, MarketWsTicket, User, UserSession
from ..schemas import InstrumentOut, MarketCandleOut, MarketQuoteOut, MarketStatusOut, MarketWsTicketOut
from ..security import hash_token, new_one_time_token, utcnow

router = APIRouter(prefix="/market", tags=["market data"])
settings = get_settings()


def _aware(value):
    return value if value is None or value.tzinfo else value.replace(tzinfo=timezone.utc)


def _market_principal(principal: Principal = Security(get_principal, scopes=["market:read"])) -> Principal:
    return principal


@router.get("/instruments", response_model=list[InstrumentOut])
def instruments(query: str | None = Query(default=None, max_length=100), exchange: str | None = None,
                segment: str | None = None, limit: int = Query(default=50, ge=1, le=100),
                principal: Principal = Depends(_market_principal), db: Session = Depends(get_db)):
    statement = select(Instrument).where(Instrument.is_active.is_(True))
    if query:
        pattern = f"%{query.strip()}%"
        statement = statement.where(or_(Instrument.trading_symbol.ilike(pattern), Instrument.symbol.ilike(pattern), Instrument.name.ilike(pattern)))
    if exchange:
        statement = statement.where(Instrument.exchange == exchange.upper())
    if segment:
        statement = statement.where(Instrument.segment == segment.upper())
    return db.scalars(statement.order_by(Instrument.exchange, Instrument.trading_symbol).limit(limit)).all()


@router.get("/quotes", response_model=list[MarketQuoteOut])
async def quotes(instrument_ids: list[str] = Query(default=[]), principal: Principal = Depends(_market_principal),
                 db: Session = Depends(get_db)):
    ids = list(dict.fromkeys(instrument_ids))
    if not ids or len(ids) > settings.market_max_subscriptions:
        raise HTTPException(status_code=422, detail=f"Request between 1 and {settings.market_max_subscriptions} instruments")
    valid = set(db.scalars(select(Instrument.id).where(Instrument.id.in_(ids), Instrument.is_active.is_(True))).all())
    if valid != set(ids):
        raise HTTPException(status_code=404, detail="One or more instruments were not found")
    return await market_bus.latest(principal.user.id, ids)


@router.get("/candles", response_model=list[MarketCandleOut])
def candles(instrument_id: str, interval_seconds: int, start_at: str | None = None, end_at: str | None = None,
            limit: int = Query(default=500, ge=1, le=5000), principal: Principal = Depends(_market_principal),
            db: Session = Depends(get_db)):
    if interval_seconds not in settings.candle_intervals:
        raise HTTPException(status_code=422, detail="Unsupported candle interval")
    if not db.get(Instrument, instrument_id):
        raise HTTPException(status_code=404, detail="Instrument not found")
    statement = select(MarketCandle).where(MarketCandle.user_id == principal.user.id,
        MarketCandle.instrument_id == instrument_id, MarketCandle.interval_seconds == interval_seconds)
    try:
        if start_at:
            statement = statement.where(MarketCandle.start_at >= datetime.fromisoformat(start_at.replace("Z", "+00:00")))
        if end_at:
            statement = statement.where(MarketCandle.start_at <= datetime.fromisoformat(end_at.replace("Z", "+00:00")))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Invalid candle time range") from exc
    return db.scalars(statement.order_by(MarketCandle.start_at.desc()).limit(limit)).all()[::-1]


@router.get("/status", response_model=MarketStatusOut)
async def status(principal: Principal = Depends(_market_principal)):
    return await market_bus.status(principal.user.id)


@router.post("/ws-ticket", response_model=MarketWsTicketOut, status_code=201)
def websocket_ticket(request: Request, principal: Principal = Depends(_market_principal), db: Session = Depends(get_db)):
    raw, digest = new_one_time_token()
    ticket = MarketWsTicket(token_hash=digest, user_id=principal.user.id, session_id=principal.session_id,
        api_key_id=principal.api_key_id, expires_at=utcnow() + timedelta(seconds=settings.market_ws_ticket_seconds))
    db.add(ticket)
    audit(db, "MARKET_WS_TICKET_CREATED", principal.user.id, request, "market_ws_ticket", ticket.id)
    db.commit()
    return MarketWsTicketOut(ticket=raw, expires_in=settings.market_ws_ticket_seconds)


def _consume_ticket(db: Session, raw: str) -> User | None:
    # The row lock makes consumption atomic under concurrent WebSocket upgrades.
    ticket = db.scalar(select(MarketWsTicket).where(
        MarketWsTicket.token_hash == hash_token(raw)).with_for_update())
    if not ticket or ticket.used_at or _aware(ticket.expires_at) <= utcnow():
        return None
    user = db.get(User, ticket.user_id)
    if not user or not user.is_active:
        return None
    if ticket.session_id:
        session = db.get(UserSession, ticket.session_id)
        if not session or session.revoked_at or _aware(session.expires_at) <= utcnow():
            return None
    elif ticket.api_key_id:
        key = db.get(ApiKey, ticket.api_key_id)
        if not key or key.status != ApiKeyStatus.ACTIVE or "market:read" not in key.scopes:
            return None
    else:
        return None
    ticket.used_at = utcnow()
    db.commit()
    return user


async def _send_quotes(websocket: WebSocket, user_id: str, subscriptions: set[str]) -> None:
    async for quote in market_bus.listen(user_id):
        if quote.instrument_id in subscriptions:
            await websocket.send_json({"type": "quote", "data": quote.model_dump(mode="json")})


@router.websocket("/stream")
async def stream(websocket: WebSocket, ticket: str = Query(min_length=20), db: Session = Depends(get_db)):
    origin = websocket.headers.get("origin")
    if origin and origin not in settings.cors_origins:
        await websocket.close(code=1008, reason="Origin is not allowed")
        return
    user = _consume_ticket(db, ticket)
    if user is None:
        await websocket.close(code=1008, reason="Invalid or expired ticket")
        return
    await websocket.accept()
    client_id = secrets.token_urlsafe(12)
    subscriptions: set[str] = set()
    sender = asyncio.create_task(_send_quotes(websocket, user.id, subscriptions))
    await websocket.send_json({"type": "ready", "client_id": client_id, "max_subscriptions": settings.market_max_subscriptions})
    try:
        while True:
            message = await websocket.receive_json()
            action = message.get("action")
            if action == "ping":
                await market_bus.replace_subscriptions(user.id, client_id, subscriptions)
                await websocket.send_json({"type": "pong"})
                continue
            if action not in {"subscribe", "unsubscribe"} or not isinstance(message.get("instrument_ids"), list):
                await websocket.send_json({"type": "error", "code": "INVALID_COMMAND"})
                continue
            mode = message.get("mode", "quote")
            if mode not in {"ltp", "quote", "depth"}:
                await websocket.send_json({"type": "error", "code": "INVALID_MODE"})
                continue
            requested = {str(item) for item in message["instrument_ids"]}
            next_subscriptions = subscriptions | requested if action == "subscribe" else subscriptions - requested
            if len(next_subscriptions) > settings.market_max_subscriptions:
                await websocket.send_json({"type": "error", "code": "SUBSCRIPTION_LIMIT"})
                continue
            valid = set(db.scalars(select(Instrument.id).where(Instrument.id.in_(next_subscriptions), Instrument.is_active.is_(True))).all()) if next_subscriptions else set()
            if valid != next_subscriptions:
                await websocket.send_json({"type": "error", "code": "INSTRUMENT_NOT_FOUND"})
                continue
            subscriptions.clear()
            subscriptions.update(next_subscriptions)
            await market_bus.replace_subscriptions(user.id, client_id, subscriptions, mode)
            snapshots = await market_bus.latest(user.id, sorted(requested & subscriptions))
            await websocket.send_json({"type": "subscribed", "instrument_ids": sorted(subscriptions),
                "snapshots": [item.model_dump(mode="json") for item in snapshots]})
    except WebSocketDisconnect:
        pass
    finally:
        sender.cancel()
        await asyncio.gather(sender, return_exceptions=True)
        await market_bus.disconnect_client(user.id, client_id)
