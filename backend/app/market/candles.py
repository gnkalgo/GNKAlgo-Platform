from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import MarketCandle
from .contracts import NormalizedQuote


class CandleAggregator:
    """Produces tenant-scoped candles from cumulative-volume quote events."""

    def __init__(self) -> None:
        self.intervals = get_settings().candle_intervals
        self._last_volume: dict[tuple[str, str], float] = {}

    @staticmethod
    def bucket(timestamp: datetime, interval: int) -> datetime:
        epoch = int(timestamp.timestamp())
        return datetime.fromtimestamp(epoch - epoch % interval, tz=timezone.utc)

    def ingest(self, db: Session, user_id: str, quote: NormalizedQuote) -> None:
        key = (user_id, quote.instrument_id)
        cumulative = quote.volume or 0
        previous = self._last_volume.get(key, cumulative)
        volume_delta = max(0.0, cumulative - previous) if cumulative >= previous else 0.0
        self._last_volume[key] = cumulative
        for interval in self.intervals:
            start = self.bucket(quote.exchange_timestamp, interval)
            candle = db.scalar(select(MarketCandle).where(
                MarketCandle.user_id == user_id, MarketCandle.instrument_id == quote.instrument_id,
                MarketCandle.interval_seconds == interval, MarketCandle.start_at == start,
            ))
            if candle is None:
                db.query(MarketCandle).filter(
                    MarketCandle.user_id == user_id, MarketCandle.instrument_id == quote.instrument_id,
                    MarketCandle.interval_seconds == interval, MarketCandle.is_complete.is_(False),
                    MarketCandle.start_at < start,
                ).update({MarketCandle.is_complete: True}, synchronize_session=False)
                candle = MarketCandle(user_id=user_id, instrument_id=quote.instrument_id, interval_seconds=interval,
                    start_at=start, open=quote.ltp, high=quote.ltp, low=quote.ltp, close=quote.ltp,
                    volume=volume_delta, open_interest=quote.open_interest, source=quote.source)
                db.add(candle)
            else:
                candle.high = max(candle.high, quote.ltp)
                candle.low = min(candle.low, quote.ltp)
                candle.close = quote.ltp
                candle.volume += volume_delta
                candle.open_interest = quote.open_interest
                candle.source = quote.source
        db.commit()
