import math
import logging
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..audit import audit
from ..config import get_settings
from ..market.bus import market_bus
from ..models import (
    Instrument, MarketCandle, OrderSide, OrderType, OrderValidity, StrategyStatus, User,
    Strategy, StrategyRun, StrategyRunStatus, StrategySignal, StrategySignalStatus,
    TradeExecution, TradingOrder,
)
from ..trading.engine import OPEN_ORDER_STATUSES, TradingError
from ..trading.service import order_manager
from .evaluator import evaluate_sma_cross

logger = logging.getLogger("gnkalgo.strategies.service")


class StrategyError(Exception):
    def __init__(self, code: str, status_code: int = 409):
        self.code = code
        self.status_code = status_code
        super().__init__(code)


def strategy_definition(strategy: Strategy) -> dict:
    return {
        "kind": strategy.kind, "instrument_id": strategy.instrument_id,
        "execution_mode": strategy.execution_mode, "timeframe_seconds": strategy.timeframe_seconds,
        "fast_period": strategy.fast_period, "slow_period": strategy.slow_period,
        "quantity": strategy.quantity, "product_type": strategy.product_type.value,
        "order_type": strategy.order_type.value, "limit_offset_bps": strategy.limit_offset_bps,
        "max_orders_per_day": strategy.max_orders_per_day,
    }


def _day_start_utc() -> datetime:
    india = ZoneInfo("Asia/Kolkata")
    now = datetime.now(india)
    return datetime.combine(now.date(), time.min, tzinfo=india).astimezone(timezone.utc)


def _client_order_id(strategy: Strategy, candle: MarketCandle) -> str:
    candle_start = candle.start_at if candle.start_at.tzinfo else candle.start_at.replace(tzinfo=timezone.utc)
    return f"s{strategy.id.replace('-', '')[:10]}{int(candle_start.timestamp())}"


def _limit_price(strategy: Strategy, instrument: Instrument, side: OrderSide, reference: float) -> float:
    direction = -1 if side == OrderSide.BUY else 1
    raw = reference * (1 + direction * strategy.limit_offset_bps / 10_000)
    tick = instrument.tick_size or 0.05
    ticks = math.floor(raw / tick) if side == OrderSide.BUY else math.ceil(raw / tick)
    return round(max(tick, ticks * tick), 8)


class StrategyExecutor:
    def __init__(self) -> None:
        self.settings = get_settings()

    def activation_check(self, strategy: Strategy, *, mfa_enabled: bool, confirmation: str | None) -> None:
        if not self.settings.strategy_engine_enabled:
            raise StrategyError("STRATEGY_ENGINE_DISABLED", 403)
        if strategy.execution_mode.lower() != self.settings.trading_mode:
            raise StrategyError("STRATEGY_TRADING_MODE_MISMATCH", 422)
        if strategy.execution_mode == "LIVE":
            if not mfa_enabled:
                raise StrategyError("MFA_REQUIRED_FOR_LIVE_STRATEGY", 403)
            if self.settings.strategy_live_confirmation != "ENABLE_LIVE_STRATEGIES":
                raise StrategyError("LIVE_STRATEGIES_NOT_CONFIRMED", 403)
            if confirmation != "ENABLE_LIVE_STRATEGY":
                raise StrategyError("LIVE_STRATEGY_ACTIVATION_NOT_CONFIRMED", 403)
            try:
                order_manager._live_gate()
            except TradingError as exc:
                raise StrategyError(exc.code, exc.status_code) from exc

    @staticmethod
    def _strategy_exposure(db: Session, strategy: Strategy) -> int:
        executions = db.execute(select(TradeExecution.side, TradeExecution.quantity).join(
            TradingOrder, TradingOrder.id == TradeExecution.order_id).join(
            StrategySignal, StrategySignal.order_id == TradingOrder.id).where(
                StrategySignal.strategy_id == strategy.id,
            )).all()
        return sum(quantity if side == OrderSide.BUY else -quantity for side, quantity in executions)

    @staticmethod
    def _has_open_order(db: Session, strategy: Strategy) -> bool:
        return db.scalar(select(func.count()).select_from(TradingOrder).join(
            StrategySignal, StrategySignal.order_id == TradingOrder.id).where(
                StrategySignal.strategy_id == strategy.id,
                TradingOrder.status.in_(OPEN_ORDER_STATUSES),
            )) > 0

    def _order_count_check(self, db: Session, strategy: Strategy) -> None:
        base = select(func.count()).select_from(StrategySignal).where(
            StrategySignal.user_id == strategy.user_id,
            StrategySignal.status == StrategySignalStatus.ORDER_SUBMITTED,
            StrategySignal.generated_at >= _day_start_utc(),
        )
        user_count = db.scalar(base) or 0
        strategy_count = db.scalar(base.where(StrategySignal.strategy_id == strategy.id)) or 0
        if user_count >= self.settings.strategy_max_orders_per_user_per_day:
            raise StrategyError("USER_STRATEGY_DAILY_ORDER_LIMIT")
        if strategy_count >= strategy.max_orders_per_day:
            raise StrategyError("STRATEGY_DAILY_ORDER_LIMIT")
        recent = db.scalar(select(func.count()).select_from(StrategySignal).where(
            StrategySignal.user_id == strategy.user_id,
            StrategySignal.status.in_([
                StrategySignalStatus.CREATED, StrategySignalStatus.ORDER_SUBMITTED,
                StrategySignalStatus.REJECTED,
            ]),
            StrategySignal.generated_at >= datetime.now(timezone.utc) - timedelta(minutes=1),
        )) or 0
        if recent >= self.settings.strategy_max_order_attempts_per_minute:
            raise StrategyError("STRATEGY_ORDER_RATE_LIMIT")

    async def evaluate(self, db: Session, strategy: Strategy, candle: MarketCandle) -> StrategyRun:
        existing = db.scalar(select(StrategyRun).where(
            StrategyRun.strategy_id == strategy.id,
            StrategyRun.candle_start_at == candle.start_at,
        ))
        if existing:
            return existing
        run = StrategyRun(strategy_id=strategy.id, user_id=strategy.user_id,
                          strategy_version=strategy.version, candle_start_at=candle.start_at,
                          status=StrategyRunStatus.STARTED, diagnostics_json={})
        db.add(run)
        db.flush()
        strategy.last_evaluated_at = datetime.now(timezone.utc)
        # Persist the unique candle claim before any broker side effect. A crash
        # leaves STARTED for manual reconciliation, never an automatic duplicate.
        db.commit()
        try:
            if not self.settings.strategy_engine_enabled:
                return self._finish(db, run, StrategyRunStatus.SKIPPED, {"reason": "STRATEGY_ENGINE_DISABLED"})
            if strategy.status.value != "ACTIVE":
                return self._finish(db, run, StrategyRunStatus.SKIPPED, {"reason": "STRATEGY_NOT_ACTIVE"})
            if strategy.execution_mode.lower() != self.settings.trading_mode:
                return self._finish(db, run, StrategyRunStatus.SKIPPED, {"reason": "TRADING_MODE_MISMATCH"})
            user = db.get(User, strategy.user_id)
            if not user or not user.is_active:
                strategy.status = StrategyStatus.ERROR
                strategy.last_error = "STRATEGY_USER_INACTIVE"
                return self._finish(db, run, StrategyRunStatus.FAILED, {"reason": strategy.last_error})
            if strategy.execution_mode == "LIVE" and self.settings.strategy_live_confirmation != "ENABLE_LIVE_STRATEGIES":
                return self._finish(db, run, StrategyRunStatus.SKIPPED, {"reason": "LIVE_STRATEGIES_NOT_CONFIRMED"})
            if strategy.execution_mode == "LIVE":
                if not user.mfa_enabled:
                    strategy.status = StrategyStatus.ERROR
                    strategy.last_error = "LIVE_STRATEGY_USER_NOT_ELIGIBLE"
                    return self._finish(db, run, StrategyRunStatus.FAILED, {"reason": strategy.last_error})
                try:
                    order_manager._live_gate()
                except TradingError as exc:
                    strategy.status = StrategyStatus.ERROR
                    strategy.last_error = exc.code
                    return self._finish(db, run, StrategyRunStatus.FAILED, {"reason": exc.code})
            candle_time = candle.start_at if candle.start_at.tzinfo else candle.start_at.replace(tzinfo=timezone.utc)
            closed_at = candle_time + timedelta(seconds=strategy.timeframe_seconds)
            lag = (datetime.now(timezone.utc) - closed_at).total_seconds()
            if lag < 0:
                return self._finish(db, run, StrategyRunStatus.SKIPPED, {"reason": "CANDLE_NOT_CLOSED"})
            if lag > self.settings.strategy_max_candle_lag_seconds:
                return self._finish(db, run, StrategyRunStatus.SKIPPED, {"reason": "STALE_CANDLE", "lag_seconds": lag})
            candles = db.scalars(select(MarketCandle).where(
                MarketCandle.user_id == strategy.user_id,
                MarketCandle.instrument_id == strategy.instrument_id,
                MarketCandle.interval_seconds == strategy.timeframe_seconds,
                MarketCandle.is_complete.is_(True),
                MarketCandle.start_at <= candle.start_at,
            ).order_by(MarketCandle.start_at.desc()).limit(strategy.slow_period + 1)).all()[::-1]
            if len(candles) >= strategy.slow_period + 1 and any(
                ((right.start_at if right.start_at.tzinfo else right.start_at.replace(tzinfo=timezone.utc))
                 - (left.start_at if left.start_at.tzinfo else left.start_at.replace(tzinfo=timezone.utc))).total_seconds()
                != strategy.timeframe_seconds
                for left, right in zip(candles, candles[1:])
            ):
                return self._finish(db, run, StrategyRunStatus.SKIPPED, {"reason": "CANDLE_GAP"})
            decision = evaluate_sma_cross([item.close for item in candles], strategy.fast_period, strategy.slow_period)
            if decision is None:
                return self._finish(db, run, StrategyRunStatus.NO_SIGNAL, {"reason": "INSUFFICIENT_CANDLES", "count": len(candles)})
            diagnostics = {
                "fast_average": decision.fast_average, "slow_average": decision.slow_average,
                "previous_fast_average": decision.previous_fast_average,
                "previous_slow_average": decision.previous_slow_average,
            }
            if decision.action is None:
                return self._finish(db, run, StrategyRunStatus.NO_SIGNAL, diagnostics)
            return await self._route(db, strategy, candle, run, decision.action, diagnostics)
        except StrategyError as exc:
            return self._finish(db, run, StrategyRunStatus.SKIPPED, {"reason": exc.code})
        except TradingError as exc:
            return self._finish(db, run, StrategyRunStatus.SKIPPED, {"reason": exc.code})
        except Exception as exc:
            logger.exception("strategy evaluation failed", extra={"strategy_id": strategy.id, "run_id": run.id})
            db.rollback()
            strategy = db.get(Strategy, strategy.id)
            run = db.get(StrategyRun, run.id)
            strategy.status = StrategyStatus.ERROR
            strategy.last_error = type(exc).__name__
            return self._finish(db, run, StrategyRunStatus.FAILED, {"reason": "STRATEGY_EVALUATION_FAILED"})

    async def _route(self, db: Session, strategy: Strategy, candle: MarketCandle, run: StrategyRun,
                     side: OrderSide, diagnostics: dict) -> StrategyRun:
        self._order_count_check(db, strategy)
        if self._has_open_order(db, strategy):
            return self._suppressed(db, strategy, candle, run, side, "OPEN_STRATEGY_ORDER", diagnostics)
        exposure = self._strategy_exposure(db, strategy)
        if side == OrderSide.BUY and exposure != 0:
            return self._suppressed(db, strategy, candle, run, side, "STRATEGY_ALREADY_EXPOSED", diagnostics)
        if side == OrderSide.SELL and exposure <= 0:
            return self._suppressed(db, strategy, candle, run, side, "NO_STRATEGY_LONG_POSITION", diagnostics)
        quantity = strategy.quantity if side == OrderSide.BUY else min(strategy.quantity, exposure)
        quotes = await market_bus.latest(strategy.user_id, [strategy.instrument_id])
        quote = quotes[0] if quotes else None
        order_manager.paper._quote_check(quote)
        instrument = db.get(Instrument, strategy.instrument_id)
        if instrument is None or not instrument.is_active:
            raise StrategyError("INSTRUMENT_NOT_AVAILABLE")
        client_order_id = _client_order_id(strategy, candle)
        signal = StrategySignal(
            strategy_id=strategy.id, run_id=run.id, user_id=strategy.user_id,
            instrument_id=strategy.instrument_id, action=side,
            status=StrategySignalStatus.CREATED, quantity=quantity,
            reference_price=quote.ltp, client_order_id=client_order_id,
        )
        db.add(signal)
        db.flush()
        values = {
            "client_order_id": client_order_id, "side": side, "order_type": strategy.order_type,
            "product_type": strategy.product_type, "validity": OrderValidity.DAY,
            "quantity": quantity, "limit_price": None, "trigger_price": None,
        }
        if strategy.order_type == OrderType.LIMIT:
            values["limit_price"] = _limit_price(strategy, instrument, side, quote.ltp)
        try:
            order = await order_manager.place(db, strategy.user_id, instrument, values, quote)
            signal.order_id = order.id
            signal.status = StrategySignalStatus.ORDER_SUBMITTED
            run.status = StrategyRunStatus.SUCCEEDED
            run.diagnostics_json = {**diagnostics, "order_status": order.status.value}
            run.finished_at = datetime.now(timezone.utc)
            strategy.last_error = None
            audit(db, "STRATEGY_ORDER_SUBMITTED", strategy.user_id, target_type="strategy", target_id=strategy.id,
                  metadata={"signal_id": signal.id, "order_id": order.id, "action": side.value})
            db.commit()
            return run
        except TradingError as exc:
            signal.status = StrategySignalStatus.REJECTED
            signal.reason = exc.code
            run.status = StrategyRunStatus.FAILED
            run.diagnostics_json = {**diagnostics, "reason": exc.code}
            run.finished_at = datetime.now(timezone.utc)
            strategy.last_error = exc.code
            if strategy.execution_mode == "LIVE":
                strategy.status = StrategyStatus.ERROR
            audit(db, "STRATEGY_ORDER_REJECTED", strategy.user_id, target_type="strategy", target_id=strategy.id,
                  metadata={"signal_id": signal.id, "reason": exc.code})
            db.commit()
            return run

    @staticmethod
    def _suppressed(db: Session, strategy: Strategy, candle: MarketCandle, run: StrategyRun,
                    side: OrderSide, reason: str, diagnostics: dict) -> StrategyRun:
        db.add(StrategySignal(
            strategy_id=strategy.id, run_id=run.id, user_id=strategy.user_id,
            instrument_id=strategy.instrument_id, action=side,
            status=StrategySignalStatus.SUPPRESSED, quantity=strategy.quantity,
            reference_price=candle.close, client_order_id=_client_order_id(strategy, candle), reason=reason,
        ))
        return StrategyExecutor._finish(db, run, StrategyRunStatus.SKIPPED, {**diagnostics, "reason": reason})

    @staticmethod
    def _finish(db: Session, run: StrategyRun, status: StrategyRunStatus, diagnostics: dict) -> StrategyRun:
        run.status = status
        run.diagnostics_json = diagnostics
        run.finished_at = datetime.now(timezone.utc)
        db.commit()
        return run


strategy_executor = StrategyExecutor()
