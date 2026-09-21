"""Single-instance Phase 8 strategy worker. A run claim prevents replay after restart."""
import asyncio
import logging
import signal
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ..config import get_settings
from ..database import SessionLocal
from ..market.bus import market_bus
from ..models import MarketCandle, Strategy, StrategyRun, StrategyRunStatus, StrategySignal, StrategySignalStatus, StrategyStatus
from .service import strategy_executor

logger = logging.getLogger("gnkalgo.strategies.worker")


class StrategyWorker:
    def __init__(self) -> None:
        self.settings = get_settings()
        self._stopping = asyncio.Event()
        self.client_id = "strategy-worker"
        self.subscribed_users: set[str] = set()

    def stop(self) -> None:
        self._stopping.set()

    async def run(self) -> None:
        logger.info("strategy worker started", extra={"enabled": self.settings.strategy_engine_enabled})
        try:
            while not self._stopping.is_set():
                if self.settings.strategy_engine_enabled:
                    await self.run_once()
                try:
                    await asyncio.wait_for(self._stopping.wait(), timeout=self.settings.strategy_poll_seconds)
                except TimeoutError:
                    pass
        finally:
            for user_id in self.subscribed_users:
                await market_bus.disconnect_client(user_id, self.client_id)

    async def run_once(self) -> None:
        if not self.settings.strategy_engine_enabled or self.settings.trading_mode == "disabled":
            return
        with SessionLocal() as db:
            strategies = db.scalars(select(Strategy).where(
                Strategy.status == StrategyStatus.ACTIVE,
                Strategy.execution_mode == self.settings.trading_mode.upper(),
            )).all()
            subscriptions: dict[str, set[str]] = defaultdict(set)
            for strategy in strategies:
                subscriptions[strategy.user_id].add(strategy.instrument_id)
            for user_id in self.subscribed_users | set(subscriptions):
                await market_bus.replace_subscriptions(user_id, self.client_id,
                                                       subscriptions.get(user_id, set()))
            self.subscribed_users = set(subscriptions)

            for strategy in strategies:
                stranded = db.scalar(select(StrategyRun).where(
                    StrategyRun.strategy_id == strategy.id,
                    StrategyRun.status == StrategyRunStatus.STARTED,
                ))
                unresolved = db.scalar(select(StrategySignal.id).where(
                    StrategySignal.strategy_id == strategy.id,
                    StrategySignal.status == StrategySignalStatus.CREATED,
                ))
                if stranded or unresolved:
                    strategy.status = StrategyStatus.ERROR
                    strategy.last_error = "INCOMPLETE_SIGNAL_REQUIRES_RECONCILIATION"
                    db.commit()
                    logger.error("strategy paused for incomplete run", extra={"strategy_id": strategy.id})
                    continue
                candle = db.scalar(select(MarketCandle).where(
                    MarketCandle.user_id == strategy.user_id,
                    MarketCandle.instrument_id == strategy.instrument_id,
                    MarketCandle.interval_seconds == strategy.timeframe_seconds,
                    MarketCandle.is_complete.is_(True),
                ).order_by(MarketCandle.start_at.desc()).limit(1))
                if candle is None:
                    continue
                already_run = db.scalar(select(StrategyRun.id).where(
                    StrategyRun.strategy_id == strategy.id,
                    StrategyRun.candle_start_at == candle.start_at,
                ))
                if already_run:
                    continue
                try:
                    run = await strategy_executor.evaluate(db, strategy, candle)
                    logger.info("strategy evaluated", extra={"strategy_id": strategy.id,
                                                       "run_id": run.id, "status": run.status.value})
                except IntegrityError:
                    db.rollback()
                    # Another instance claimed the candle. Never issue a second order.
                    logger.warning("duplicate strategy candle claim", extra={"strategy_id": strategy.id})
                except Exception:
                    db.rollback()
                    logger.exception("strategy worker failed", extra={"strategy_id": strategy.id})


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    worker = StrategyWorker()
    loop = asyncio.get_running_loop()
    for event in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(event, worker.stop)
        except NotImplementedError:
            pass
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
