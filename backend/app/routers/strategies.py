from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Security
from sqlalchemy import and_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..audit import audit
from ..config import get_settings
from ..database import get_db
from ..dependencies import Principal, get_principal, require_session
from ..models import (
    Instrument, MarketCandle, OrderStatus, Strategy, StrategyRun, StrategyRunStatus,
    StrategySignal, StrategySignalStatus, StrategyStatus,
    StrategyVersion, TradingOrder,
)
from ..schemas import (
    StrategyActivate, StrategyCreate, StrategyOut, StrategyPreviewOut,
    StrategyRunOut, StrategySignalOut, StrategyUpdate, StrategyVersionOut,
)
from ..strategies.evaluator import evaluate_sma_cross
from ..strategies.service import StrategyError, strategy_definition, strategy_executor

router = APIRouter(prefix="/strategies", tags=["strategies"])
settings = get_settings()


def _read(principal: Principal = Security(get_principal, scopes=["strategies:read"])) -> Principal:
    return principal


def _write(principal: Principal = Security(get_principal, scopes=["strategies:write"])) -> Principal:
    return principal


def _owned(db: Session, user_id: str, strategy_id: str) -> Strategy:
    strategy = db.scalar(select(Strategy).where(
        Strategy.id == strategy_id, Strategy.user_id == user_id))
    if strategy is None:
        raise HTTPException(status_code=404, detail="Strategy not found")
    return strategy


@router.get("", response_model=list[StrategyOut])
def list_strategies(principal: Principal = Depends(_read), db: Session = Depends(get_db)):
    return db.scalars(select(Strategy).where(Strategy.user_id == principal.user.id)
                      .order_by(Strategy.created_at.desc())).all()


@router.post("", response_model=StrategyOut, status_code=201)
def create_strategy(payload: StrategyCreate, request: Request,
                    principal: Principal = Depends(_write), db: Session = Depends(get_db)):
    instrument = db.scalar(select(Instrument).where(
        Instrument.id == payload.instrument_id, Instrument.is_active.is_(True)))
    if instrument is None:
        raise HTTPException(status_code=404, detail="Instrument not found")
    if payload.timeframe_seconds not in settings.candle_intervals:
        raise HTTPException(status_code=422, detail="Unsupported candle interval")
    if not payload.name.strip():
        raise HTTPException(status_code=422, detail="Strategy name required")
    strategy = Strategy(user_id=principal.user.id, instrument_id=instrument.id,
                        name=payload.name.strip(), kind="SMA_CROSS", status=StrategyStatus.DRAFT,
                        **payload.model_dump(exclude={"name", "instrument_id"}))
    db.add(strategy)
    try:
        db.flush()
        db.add(StrategyVersion(strategy_id=strategy.id, user_id=principal.user.id, version=1,
                               definition_json=strategy_definition(strategy), created_by_user_id=principal.user.id))
        audit(db, "STRATEGY_CREATED", principal.user.id, request, "strategy", strategy.id,
              {"mode": strategy.execution_mode, "version": 1})
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Strategy name already exists") from exc
    return strategy


@router.get("/{strategy_id}", response_model=StrategyOut)
def get_strategy(strategy_id: str, principal: Principal = Depends(_read), db: Session = Depends(get_db)):
    return _owned(db, principal.user.id, strategy_id)


@router.patch("/{strategy_id}", response_model=StrategyOut)
def update_strategy(strategy_id: str, payload: StrategyUpdate, request: Request,
                    principal: Principal = Depends(_write), db: Session = Depends(get_db)):
    strategy = _owned(db, principal.user.id, strategy_id)
    if strategy.status not in {StrategyStatus.DRAFT, StrategyStatus.PAUSED, StrategyStatus.ERROR}:
        raise HTTPException(status_code=409, detail="Pause strategy before editing")
    changes = payload.model_dump(exclude_unset=True)
    if any(value is None for value in changes.values()):
        raise HTTPException(status_code=422, detail="Strategy fields cannot be null")
    if "name" in changes:
        changes["name"] = changes["name"].strip()
        if not changes["name"]:
            raise HTTPException(status_code=422, detail="Strategy name required")
    fast = changes.get("fast_period", strategy.fast_period)
    slow = changes.get("slow_period", strategy.slow_period)
    if fast >= slow:
        raise HTTPException(status_code=422, detail="fast_period must be less than slow_period")
    if changes.get("timeframe_seconds", strategy.timeframe_seconds) not in settings.candle_intervals:
        raise HTTPException(status_code=422, detail="Unsupported candle interval")
    for key, value in changes.items():
        setattr(strategy, key, value)
    if changes:
        strategy.version += 1
        strategy.status = StrategyStatus.DRAFT
        strategy.last_error = None
        db.add(StrategyVersion(strategy_id=strategy.id, user_id=principal.user.id,
                               version=strategy.version, definition_json=strategy_definition(strategy),
                               created_by_user_id=principal.user.id))
        audit(db, "STRATEGY_UPDATED", principal.user.id, request, "strategy", strategy.id,
              {"version": strategy.version, "fields": sorted(changes)})
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Strategy name already exists") from exc
    return strategy


@router.post("/{strategy_id}/activate", response_model=StrategyOut)
def activate(strategy_id: str, payload: StrategyActivate, request: Request,
             principal: Principal = Depends(require_session), db: Session = Depends(get_db)):
    strategy = _owned(db, principal.user.id, strategy_id)
    if strategy.status not in {StrategyStatus.DRAFT, StrategyStatus.PAUSED, StrategyStatus.ERROR}:
        raise HTTPException(status_code=409, detail="Strategy cannot be activated")
    stranded = db.scalar(select(StrategyRun.id).where(
        StrategyRun.strategy_id == strategy.id, StrategyRun.status == StrategyRunStatus.STARTED))
    if stranded:
        raise HTTPException(status_code=409, detail="Incomplete run requires manual reconciliation")
    unresolved = db.scalar(select(StrategySignal.id).where(
        StrategySignal.strategy_id == strategy.id,
        StrategySignal.status == StrategySignalStatus.CREATED,
    ))
    if unresolved:
        raise HTTPException(status_code=409, detail="Unresolved signal requires manual reconciliation")
    ambiguous_order = db.scalar(select(TradingOrder.id).join(
        StrategySignal, and_(StrategySignal.user_id == TradingOrder.user_id,
                             StrategySignal.client_order_id == TradingOrder.client_order_id),
    ).where(StrategySignal.strategy_id == strategy.id, TradingOrder.status == OrderStatus.UNKNOWN))
    if ambiguous_order:
        raise HTTPException(status_code=409, detail="Unknown broker order requires reconciliation")
    try:
        strategy_executor.activation_check(strategy, mfa_enabled=principal.user.mfa_enabled,
                                           confirmation=payload.confirmation)
    except StrategyError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.code) from exc
    strategy.status = StrategyStatus.ACTIVE
    strategy.activated_at = datetime.now(timezone.utc)
    strategy.last_error = None
    audit(db, "STRATEGY_ACTIVATED", principal.user.id, request, "strategy", strategy.id,
          {"version": strategy.version, "mode": strategy.execution_mode})
    db.commit()
    return strategy


@router.post("/{strategy_id}/pause", response_model=StrategyOut)
def pause(strategy_id: str, request: Request,
          principal: Principal = Depends(require_session), db: Session = Depends(get_db)):
    strategy = _owned(db, principal.user.id, strategy_id)
    strategy.status = StrategyStatus.PAUSED
    strategy.paused_at = datetime.now(timezone.utc)
    audit(db, "STRATEGY_PAUSED", principal.user.id, request, "strategy", strategy.id)
    db.commit()
    return strategy


@router.get("/{strategy_id}/versions", response_model=list[StrategyVersionOut])
def versions(strategy_id: str, principal: Principal = Depends(_read), db: Session = Depends(get_db)):
    _owned(db, principal.user.id, strategy_id)
    return db.scalars(select(StrategyVersion).where(StrategyVersion.strategy_id == strategy_id)
                      .order_by(StrategyVersion.version.desc())).all()


@router.get("/{strategy_id}/preview", response_model=list[StrategyPreviewOut])
def preview(strategy_id: str, limit: int = Query(default=500, ge=1, le=2000),
            principal: Principal = Depends(_read), db: Session = Depends(get_db)):
    strategy = _owned(db, principal.user.id, strategy_id)
    candles = db.scalars(select(MarketCandle).where(
        MarketCandle.user_id == principal.user.id,
        MarketCandle.instrument_id == strategy.instrument_id,
        MarketCandle.interval_seconds == strategy.timeframe_seconds,
        MarketCandle.is_complete.is_(True),
    ).order_by(MarketCandle.start_at.desc()).limit(limit)).all()[::-1]
    closes = []
    output = []
    for candle in candles:
        closes.append(candle.close)
        decision = evaluate_sma_cross(closes, strategy.fast_period, strategy.slow_period)
        if decision and decision.action:
            output.append(StrategyPreviewOut(
                candle_start_at=candle.start_at, action=decision.action,
                close=candle.close, fast_average=decision.fast_average,
                slow_average=decision.slow_average,
            ))
    return output


@router.get("/{strategy_id}/runs", response_model=list[StrategyRunOut])
def runs(strategy_id: str, limit: int = Query(default=100, ge=1, le=500),
         principal: Principal = Depends(_read), db: Session = Depends(get_db)):
    _owned(db, principal.user.id, strategy_id)
    return db.scalars(select(StrategyRun).where(StrategyRun.strategy_id == strategy_id)
                      .order_by(StrategyRun.started_at.desc()).limit(limit)).all()


@router.get("/{strategy_id}/signals", response_model=list[StrategySignalOut])
def signals(strategy_id: str, limit: int = Query(default=100, ge=1, le=500),
            principal: Principal = Depends(_read), db: Session = Depends(get_db)):
    _owned(db, principal.user.id, strategy_id)
    return db.scalars(select(StrategySignal).where(StrategySignal.strategy_id == strategy_id)
                      .order_by(StrategySignal.generated_at.desc()).limit(limit)).all()
