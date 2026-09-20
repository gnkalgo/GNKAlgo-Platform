from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any

from ..models import Instrument
from .contracts import NormalizedQuote


def _timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if value is None:
        return datetime.now(timezone.utc)
    numeric = float(value)
    if numeric > 10_000_000_000:
        numeric /= 1000
    return datetime.fromtimestamp(numeric, tz=timezone.utc)


def _number(payload: dict, *names: str) -> float | None:
    for name in names:
        value = payload.get(name)
        if value is not None and value != "":
            return float(value)
    return None


class MarketDataAdapter(ABC):
    """Contract for broker SDK/WebSocket callbacks."""

    name: str

    @abstractmethod
    def normalize(self, instrument: Instrument, payload: dict, sequence: int) -> NormalizedQuote:
        raise NotImplementedError


class FyersV3Adapter(MarketDataAdapter):
    name = "FYERS"

    def normalize(self, instrument: Instrument, payload: dict, sequence: int) -> NormalizedQuote:
        return NormalizedQuote(
            instrument_id=instrument.id, exchange=instrument.exchange, segment=instrument.segment,
            trading_symbol=instrument.trading_symbol, source=self.name, mode="quote", sequence=sequence,
            exchange_timestamp=_timestamp(payload.get("last_traded_time")), ltp=float(payload["ltp"]),
            last_quantity=_number(payload, "last_traded_qty"), open=_number(payload, "open_price"),
            high=_number(payload, "high_price"), low=_number(payload, "low_price"),
            previous_close=_number(payload, "prev_close_price"), volume=_number(payload, "vol_traded_today"),
            bid=_number(payload, "bid_price"), ask=_number(payload, "ask_price"),
        )


class UpstoxV3Adapter(MarketDataAdapter):
    name = "UPSTOX"

    def normalize(self, instrument: Instrument, payload: dict, sequence: int) -> NormalizedQuote:
        ltpc = payload.get("ltpc", payload)
        details = payload.get("marketFF", payload.get("fullFeed", payload))
        return NormalizedQuote(
            instrument_id=instrument.id, exchange=instrument.exchange, segment=instrument.segment,
            trading_symbol=instrument.trading_symbol, source=self.name, mode="quote", sequence=sequence,
            exchange_timestamp=_timestamp(ltpc.get("ltt") or payload.get("currentTs")), ltp=float(ltpc["ltp"]),
            last_quantity=_number(ltpc, "ltq"), previous_close=_number(ltpc, "cp"),
            volume=_number(details, "vtt", "volume"), open_interest=_number(details, "oi"),
        )


class DhanV2Adapter(MarketDataAdapter):
    name = "DHAN"

    def normalize(self, instrument: Instrument, payload: dict, sequence: int) -> NormalizedQuote:
        return NormalizedQuote(
            instrument_id=instrument.id, exchange=instrument.exchange, segment=instrument.segment,
            trading_symbol=instrument.trading_symbol, source=self.name,
            mode="depth" if payload.get("depth") else "quote", sequence=sequence,
            exchange_timestamp=_timestamp(payload.get("LTT") or payload.get("last_trade_time")),
            ltp=float(payload.get("LTP", payload.get("ltp"))), last_quantity=_number(payload, "LTQ", "last_quantity"),
            open=_number(payload, "open"), high=_number(payload, "high"), low=_number(payload, "low"),
            previous_close=_number(payload, "close", "previous_close"), volume=_number(payload, "volume"),
            open_interest=_number(payload, "OI", "open_interest"), bid=_number(payload, "bid"),
            ask=_number(payload, "ask"), depth=payload.get("depth"),
        )


ADAPTERS: dict[str, MarketDataAdapter] = {
    "DHAN": DhanV2Adapter(), "FYERS": FyersV3Adapter(), "UPSTOX": UpstoxV3Adapter()
}
