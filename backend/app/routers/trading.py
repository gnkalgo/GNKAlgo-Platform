from fastapi import APIRouter, Depends, HTTPException, Query, Request, Security
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..audit import audit
from ..config import get_settings
from ..database import get_db
from ..dependencies import Principal, get_principal, require_admin, require_session
from ..market.bus import market_bus
from ..models import Instrument, TradeExecution, TradingControl, TradingOrder, TradingPosition, User
from ..schemas import (
    KillSwitchOut, KillSwitchRequest, OrderCreate, OrderModify, TradeExecutionOut,
    TradingOrderOut, TradingPositionOut, TradingStatusOut,
)
from ..trading.engine import TradingError
from ..trading.service import order_manager

router = APIRouter(prefix="/trading", tags=["trading"])
settings = get_settings()


def _read_principal(principal: Principal = Security(get_principal, scopes=["orders:read"])) -> Principal:
    return principal


def _write_principal(principal: Principal = Security(get_principal, scopes=["orders:write"])) -> Principal:
    return principal


def _owned_order(db: Session, user_id: str, order_id: str) -> TradingOrder:
    order = db.scalar(select(TradingOrder).where(
        TradingOrder.id == order_id, TradingOrder.user_id == user_id))
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")
    return order


async def _latest_quote(user_id: str, instrument_id: str):
    quotes = await market_bus.latest(user_id, [instrument_id])
    return quotes[0] if quotes else None


def _trading_error(exc: TradingError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.code)


@router.get("/status", response_model=TradingStatusOut)
def status(principal: Principal = Depends(_read_principal), db: Session = Depends(get_db)):
    controls = db.scalars(select(TradingControl).where(
        TradingControl.scope_key.in_(["GLOBAL", f"USER:{principal.user.id}"]))).all()
    halted = next((item for item in controls if item.is_halted), None)
    return {
        "mode": settings.trading_mode,
        "live_ready": settings.trading_mode == "live"
        and settings.trading_live_confirmation == "ENABLE_DHAN_LIVE_ORDERS"
        and settings.dhan_static_ip_confirmed,
        "halted": halted is not None,
        "halt_reason": halted.reason if halted else None,
        "limits": {
            "max_order_quantity": settings.trading_max_order_quantity,
            "max_order_notional": settings.trading_max_order_notional,
            "max_open_orders": settings.trading_max_open_orders,
            "max_absolute_position": settings.trading_max_absolute_position,
        },
    }


@router.get("/orders", response_model=list[TradingOrderOut])
def orders(limit: int = Query(default=100, ge=1, le=500),
           principal: Principal = Depends(_read_principal), db: Session = Depends(get_db)):
    return db.scalars(select(TradingOrder).where(TradingOrder.user_id == principal.user.id)
                      .order_by(TradingOrder.created_at.desc()).limit(limit)).all()


@router.get("/orders/{order_id}", response_model=TradingOrderOut)
def order(order_id: str, principal: Principal = Depends(_read_principal), db: Session = Depends(get_db)):
    return _owned_order(db, principal.user.id, order_id)


@router.post("/orders", response_model=TradingOrderOut, status_code=201)
async def place_order(payload: OrderCreate, request: Request,
                      principal: Principal = Depends(_write_principal), db: Session = Depends(get_db)):
    instrument = db.scalar(select(Instrument).where(
        Instrument.id == payload.instrument_id, Instrument.is_active.is_(True)))
    if instrument is None:
        raise HTTPException(status_code=404, detail="Instrument not found")
    try:
        result = await order_manager.place(
            db, principal.user.id, instrument, payload.model_dump(),
            await _latest_quote(principal.user.id, instrument.id),
        )
    except TradingError as exc:
        raise _trading_error(exc) from exc
    audit(db, "ORDER_SUBMITTED", principal.user.id, request, "trading_order", result.id,
          {"mode": result.mode, "client_order_id": result.client_order_id, "status": result.status.value})
    db.commit()
    return result


@router.patch("/orders/{order_id}", response_model=TradingOrderOut)
async def modify_order(order_id: str, payload: OrderModify, request: Request,
                       principal: Principal = Depends(_write_principal), db: Session = Depends(get_db)):
    result = _owned_order(db, principal.user.id, order_id)
    try:
        result = await order_manager.modify(
            db, result, payload.quantity, payload.limit_price,
            await _latest_quote(principal.user.id, result.instrument_id),
        )
    except TradingError as exc:
        raise _trading_error(exc) from exc
    audit(db, "ORDER_MODIFIED", principal.user.id, request, "trading_order", result.id,
          {"quantity": result.quantity, "limit_price": result.limit_price, "status": result.status.value})
    db.commit()
    return result


@router.post("/orders/{order_id}/cancel", response_model=TradingOrderOut)
async def cancel_order(order_id: str, request: Request,
                       principal: Principal = Depends(_write_principal), db: Session = Depends(get_db)):
    result = _owned_order(db, principal.user.id, order_id)
    try:
        result = await order_manager.cancel(db, result)
    except TradingError as exc:
        raise _trading_error(exc) from exc
    audit(db, "ORDER_CANCELED", principal.user.id, request, "trading_order", result.id,
          {"mode": result.mode, "status": result.status.value})
    db.commit()
    return result


@router.get("/executions", response_model=list[TradeExecutionOut])
def executions(limit: int = Query(default=100, ge=1, le=500),
               principal: Principal = Depends(_read_principal), db: Session = Depends(get_db)):
    return db.scalars(select(TradeExecution).where(TradeExecution.user_id == principal.user.id)
                      .order_by(TradeExecution.executed_at.desc()).limit(limit)).all()


@router.get("/positions", response_model=list[TradingPositionOut])
def positions(principal: Principal = Depends(_read_principal), db: Session = Depends(get_db)):
    rows = db.scalars(select(TradingPosition).where(TradingPosition.user_id == principal.user.id)
                      .order_by(TradingPosition.updated_at.desc())).all()
    output = []
    for row in rows:
        mark = row.last_price if row.last_price is not None else row.average_price
        unrealized = row.quantity * (mark - row.average_price)
        output.append(TradingPositionOut.model_validate(row).model_copy(update={"unrealized_pnl": unrealized}))
    return output


@router.post("/kill-switch", response_model=KillSwitchOut)
def kill_switch(payload: KillSwitchRequest, request: Request,
                session_principal: Principal = Depends(require_session), db: Session = Depends(get_db)):
    scope_key = f"USER:{session_principal.user.id}"
    control = db.get(TradingControl, scope_key)
    if control is None:
        control = TradingControl(scope_key=scope_key, user_id=session_principal.user.id)
        db.add(control)
    control.is_halted = payload.halted
    control.reason = payload.reason if payload.halted else None
    control.updated_by_user_id = session_principal.user.id
    audit(db, "TRADING_KILL_SWITCH_CHANGED", session_principal.user.id, request, "trading_control", scope_key,
          {"halted": control.is_halted, "reason": control.reason})
    db.commit()
    return KillSwitchOut(halted=control.is_halted, reason=control.reason)


@router.post("/global-kill-switch", response_model=KillSwitchOut)
def global_kill_switch(payload: KillSwitchRequest, request: Request,
                       admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    control = db.get(TradingControl, "GLOBAL")
    if control is None:
        control = TradingControl(scope_key="GLOBAL")
        db.add(control)
    control.is_halted = payload.halted
    control.reason = payload.reason if payload.halted else None
    control.updated_by_user_id = admin.id
    audit(db, "GLOBAL_TRADING_KILL_SWITCH_CHANGED", admin.id, request, "trading_control", "GLOBAL",
          {"halted": control.is_halted, "reason": control.reason})
    db.commit()
    return KillSwitchOut(halted=control.is_halted, reason=control.reason)
