"""Dedicated Phase 6 feed worker.

Run with ``python -m app.market.worker``. Production broker SDK callbacks should
normalize through app.market.adapters and publish through the same bus.
"""
import asyncio
import logging
import math
import signal
from collections import defaultdict
from datetime import datetime, timezone

from sqlalchemy import select

from ..config import get_settings
from ..database import SessionLocal
from ..models import Instrument
from .bus import market_bus
from .candles import CandleAggregator
from .contracts import NormalizedQuote

logger = logging.getLogger("gnkalgo.market.worker")


class MarketWorker:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.candles = CandleAggregator()
        self.sequences: dict[tuple[str, str], int] = defaultdict(int)
        self._stopping = asyncio.Event()

    def stop(self) -> None:
        self._stopping.set()

    async def run(self) -> None:
        logger.info("market worker started", extra={"provider": self.settings.market_feed_provider})
        while not self._stopping.is_set():
            if self.settings.market_feed_provider == "disabled":
                await asyncio.sleep(5)
                continue
            if self.settings.market_feed_provider == "broker":
                raise RuntimeError("Broker feed SDK runners must be configured before MARKET_FEED_PROVIDER=broker")
            await self._simulate_once()
            await asyncio.sleep(self.settings.market_simulated_interval_seconds)

    async def _simulate_once(self) -> None:
        subscriptions = await market_bus.active_subscriptions()
        if not subscriptions:
            return
        with SessionLocal() as db:
            for user_id, instrument_id in subscriptions:
                instrument = db.scalar(select(Instrument).where(Instrument.id == instrument_id, Instrument.is_active.is_(True)))
                if instrument is None:
                    continue
                key = (user_id, instrument_id)
                self.sequences[key] += 1
                sequence = self.sequences[key]
                base = 100 + (sum(instrument.trading_symbol.encode()) % 9900)
                price = round(base + math.sin(sequence / 10) * max(0.5, base * 0.001), 2)
                now = datetime.now(timezone.utc)
                quote = NormalizedQuote(instrument_id=instrument.id, exchange=instrument.exchange,
                    segment=instrument.segment, trading_symbol=instrument.trading_symbol, source="SIMULATED",
                    mode="quote", sequence=sequence, exchange_timestamp=now, ltp=price,
                    previous_close=float(base), volume=float(sequence * 10), bid=max(0.01, price - 0.05), ask=price + 0.05)
                await market_bus.publish(user_id, quote)
                self.candles.ingest(db, user_id, quote)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    worker = MarketWorker()
    loop = asyncio.get_running_loop()
    for event in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(event, worker.stop)
        except NotImplementedError:
            pass
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
