"""Dhan v2 live market-feed protocol and credential-gated session supervisor."""

import asyncio
import hashlib
import json
import logging
import random
import struct
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

from sqlalchemy import select

from ..database import SessionLocal
from ..models import BrokerConnection, BrokerName, BrokerStatus, Instrument
from ..security import decrypt_json
from .bus import market_bus

logger = logging.getLogger("gnkalgo.market.dhan")

DHAN_FEED_URL = "wss://api-feed.dhan.co"
HEADER = struct.Struct("<BHBI")
TICKER = struct.Struct("<BHBIfI")
QUOTE = struct.Struct("<BHBIfHIfIIIffff")
OPEN_INTEREST = struct.Struct("<BHBII")
FULL = struct.Struct("<BHBIfHIfIIIIIIffff100s")
DEPTH_LEVEL = struct.Struct("<IIHHff")
DISCONNECT = struct.Struct("<BHBIH")

SEGMENT_CODES = {
    "IDX_I": 0,
    "NSE_EQ": 1,
    "NSE_FNO": 2,
    "NSE_CURRENCY": 3,
    "BSE_EQ": 4,
    "MCX_COMM": 5,
    "BSE_CURRENCY": 7,
    "BSE_FNO": 8,
}


class DhanProtocolError(ValueError):
    pass


class DhanDisconnectError(ConnectionError):
    def __init__(self, code: int):
        self.code = code
        super().__init__(f"Dhan feed disconnected with code {code}")


@dataclass(frozen=True)
class DhanSubscription:
    instrument_id: str
    exchange_segment: str
    exchange_code: int
    security_id: str
    instrument: Instrument

    @property
    def key(self) -> tuple[int, str]:
        return self.exchange_code, self.security_id


def _derived_segment(instrument: Instrument) -> str:
    exchange = instrument.exchange.upper()
    segment = instrument.segment.upper()
    if exchange in {"IDX", "INDEX"} or segment in {"IDX", "INDEX", "INDICES"}:
        return "IDX_I"
    if exchange in {"NFO"} or (exchange == "NSE" and segment in {"FNO", "FO", "FUT", "OPT", "FUTURES", "OPTIONS"}):
        return "NSE_FNO"
    if exchange == "NSE" and segment in {"CUR", "CDS", "CURRENCY"}:
        return "NSE_CURRENCY"
    if exchange == "NSE" and segment in {"EQ", "EQUITY", "CASH"}:
        return "NSE_EQ"
    if exchange in {"BFO"} or (exchange == "BSE" and segment in {"FNO", "FO", "FUT", "OPT", "FUTURES", "OPTIONS"}):
        return "BSE_FNO"
    if exchange == "BSE" and segment in {"CUR", "CDS", "CURRENCY"}:
        return "BSE_CURRENCY"
    if exchange == "BSE" and segment in {"EQ", "EQUITY", "CASH"}:
        return "BSE_EQ"
    if exchange == "MCX":
        return "MCX_COMM"
    raise DhanProtocolError(f"Unsupported Dhan exchange/segment: {exchange}/{segment}")


def dhan_subscription(instrument: Instrument) -> DhanSubscription:
    token: Any = (instrument.broker_tokens or {}).get("DHAN")
    if isinstance(token, dict):
        security_id = token.get("security_id") or token.get("securityId") or token.get("token")
        segment = str(token.get("exchange_segment") or token.get("exchangeSegment") or _derived_segment(instrument)).upper()
    else:
        security_id = token
        segment = _derived_segment(instrument)
    if security_id is None or str(security_id).strip() == "":
        raise DhanProtocolError(f"Instrument {instrument.id} has no Dhan security ID")
    if segment not in SEGMENT_CODES:
        raise DhanProtocolError(f"Unsupported Dhan exchange segment: {segment}")
    return DhanSubscription(
        instrument_id=instrument.id,
        exchange_segment=segment,
        exchange_code=SEGMENT_CODES[segment],
        security_id=str(security_id),
        instrument=instrument,
    )


def subscription_messages(
    subscriptions: Iterable[DhanSubscription], request_code: int, batch_size: int = 100
) -> list[str]:
    if request_code not in {15, 16, 17, 18, 21, 22}:
        raise DhanProtocolError(f"Unsupported Dhan request code: {request_code}")
    items = sorted(subscriptions, key=lambda item: (item.exchange_code, item.security_id))
    messages = []
    for offset in range(0, len(items), batch_size):
        batch = items[offset : offset + batch_size]
        messages.append(json.dumps({
            "RequestCode": request_code,
            "InstrumentCount": len(batch),
            "InstrumentList": [
                {"ExchangeSegment": item.exchange_segment, "SecurityId": item.security_id} for item in batch
            ],
        }, separators=(",", ":")))
    return messages


def _depth(raw: bytes) -> dict[str, list[dict[str, int | float]]]:
    levels = []
    for offset in range(0, len(raw), DEPTH_LEVEL.size):
        bid_qty, ask_qty, bid_orders, ask_orders, bid, ask = DEPTH_LEVEL.unpack_from(raw, offset)
        levels.append({"bid_quantity": bid_qty, "ask_quantity": ask_qty, "bid_orders": bid_orders,
                       "ask_orders": ask_orders, "bid": bid, "ask": ask})
    return {"levels": levels}


def _decode_frame(frame: bytes) -> dict[str, Any]:
    code, length, exchange, security = HEADER.unpack_from(frame)
    base: dict[str, Any] = {"response_code": code, "message_length": length,
                            "exchange_segment": exchange, "security_id": str(security)}
    if code == 2:
        if len(frame) < TICKER.size:
            raise DhanProtocolError("Truncated Dhan ticker packet")
        values = TICKER.unpack_from(frame)
        base.update({"LTP": values[4], "LTT": values[5]})
    elif code == 4:
        if len(frame) < QUOTE.size:
            raise DhanProtocolError("Truncated Dhan quote packet")
        values = QUOTE.unpack_from(frame)
        base.update({"LTP": values[4], "LTQ": values[5], "LTT": values[6], "average_price": values[7],
                     "volume": values[8], "total_sell_quantity": values[9], "total_buy_quantity": values[10],
                     "open": values[11], "close": values[12], "high": values[13], "low": values[14]})
    elif code == 5:
        if len(frame) < OPEN_INTEREST.size:
            raise DhanProtocolError("Truncated Dhan open-interest packet")
        base["OI"] = OPEN_INTEREST.unpack_from(frame)[4]
    elif code == 6:
        if len(frame) < TICKER.size:
            raise DhanProtocolError("Truncated Dhan previous-close packet")
        values = TICKER.unpack_from(frame)
        base.update({"close": values[4], "previous_open_interest": values[5]})
    elif code == 8:
        if len(frame) < FULL.size:
            raise DhanProtocolError("Truncated Dhan full packet")
        values = FULL.unpack_from(frame)
        depth = _depth(values[18])
        base.update({"LTP": values[4], "LTQ": values[5], "LTT": values[6], "average_price": values[7],
                     "volume": values[8], "total_sell_quantity": values[9], "total_buy_quantity": values[10],
                     "OI": values[11], "oi_day_high": values[12], "oi_day_low": values[13],
                     "open": values[14], "close": values[15], "high": values[16], "low": values[17],
                     "depth": depth, "bid": depth["levels"][0]["bid"], "ask": depth["levels"][0]["ask"]})
    elif code == 50:
        if len(frame) < DISCONNECT.size:
            raise DhanProtocolError("Truncated Dhan disconnect packet")
        base["disconnect_code"] = DISCONNECT.unpack_from(frame)[4]
    return base


def decode_dhan_frames(data: bytes) -> list[dict[str, Any]]:
    """Decode one WebSocket binary message, including concatenated Dhan packets."""
    packets: list[dict[str, Any]] = []
    offset = 0
    while offset < len(data):
        if len(data) - offset < HEADER.size:
            raise DhanProtocolError("Truncated Dhan packet header")
        _, length, _, _ = HEADER.unpack_from(data, offset)
        if length < HEADER.size:
            raise DhanProtocolError(f"Invalid Dhan packet length: {length}")
        end = offset + length
        if end > len(data):
            raise DhanProtocolError("Dhan packet length exceeds WebSocket message")
        packet = _decode_frame(data[offset:end])
        packets.append(packet)
        offset = end
    return packets


PacketCallback = Callable[[str, DhanSubscription, dict[str, Any]], Awaitable[None]]
FatalCallback = Callable[[str, int], Awaitable[None]]
HealthCallback = Callable[[str, str, dict[str, Any]], Awaitable[None]]


class DhanFeedSession:
    def __init__(self, user_id: str, client_id: str, access_token: str, request_code: int,
                 reconnect_max_seconds: float, on_packet: PacketCallback, on_fatal: FatalCallback,
                 on_health: HealthCallback | None = None):
        self.user_id = user_id
        self.client_id = client_id
        self.access_token = access_token
        self.request_code = request_code
        self.reconnect_max_seconds = reconnect_max_seconds
        self.on_packet = on_packet
        self.on_fatal = on_fatal
        self.on_health = on_health
        self._desired: dict[tuple[int, str], DhanSubscription] = {}
        self._partial: dict[tuple[int, str], dict[str, Any]] = {}
        self._changed = asyncio.Event()
        self._stopping = asyncio.Event()

    async def _emit_health(self, state: str, **details: Any) -> None:
        if self.on_health:
            await self.on_health(self.user_id, state, details)

    def replace_subscriptions(self, subscriptions: Iterable[DhanSubscription]) -> None:
        self._desired = {item.key: item for item in subscriptions}
        self._changed.set()

    def stop(self) -> None:
        self._stopping.set()
        self._changed.set()

    async def _send(self, websocket: Any, subscriptions: Iterable[DhanSubscription], request_code: int) -> None:
        for message in subscription_messages(subscriptions, request_code):
            await websocket.send(message)

    async def _connected(self, websocket: Any) -> None:
        active: dict[tuple[int, str], DhanSubscription] = {}
        while not self._stopping.is_set():
            desired = dict(self._desired)
            removed = [active[key] for key in active.keys() - desired.keys()]
            added = [desired[key] for key in desired.keys() - active.keys()]
            if removed:
                await self._send(websocket, removed, self.request_code + 1)
            if added:
                await self._send(websocket, added, self.request_code)
            active = desired
            self._changed.clear()
            receive = asyncio.create_task(websocket.recv())
            changed = asyncio.create_task(self._changed.wait())
            stopped = asyncio.create_task(self._stopping.wait())
            done, pending = await asyncio.wait({receive, changed, stopped}, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            if stopped in done:
                return
            if receive in done:
                message = receive.result()
                if isinstance(message, bytes):
                    for packet in decode_dhan_frames(message):
                        if packet["response_code"] == 50:
                            raise DhanDisconnectError(packet["disconnect_code"])
                        key = (packet["exchange_segment"], packet["security_id"])
                        if packet["response_code"] in {5, 6}:
                            self._partial.setdefault(key, {}).update(packet)
                            continue
                        if packet["response_code"] not in {2, 4, 8}:
                            continue
                        subscription = active.get(key)
                        if subscription:
                            payload = {**self._partial.get(key, {}), **packet}
                            await self.on_packet(self.user_id, subscription, payload)

    async def run(self) -> None:
        delay = 1.0
        while not self._stopping.is_set():
            try:
                await self._emit_health("connecting")
                try:
                    import websockets
                except ImportError as exc:
                    raise RuntimeError("The websockets dependency is required for the Dhan live feed") from exc
                query = urlencode({"version": "2", "token": self.access_token,
                                   "clientId": self.client_id, "authType": "2"})
                async with websockets.connect(f"{DHAN_FEED_URL}?{query}", ping_interval=10, ping_timeout=40,
                                              close_timeout=5, max_size=2**20) as websocket:
                    logger.info("Dhan feed connected", extra={"user_id": self.user_id})
                    await self._emit_health("connected", connected_at=datetime.now(timezone.utc), last_error="")
                    delay = 1.0
                    await self._connected(websocket)
            except asyncio.CancelledError:
                raise
            except DhanDisconnectError as exc:
                if exc.code in {806, 807, 808, 809}:
                    await self.on_fatal(self.user_id, exc.code)
                    return
                await self._emit_health("reconnecting", reconnect_delta=1,
                                        disconnected_at=datetime.now(timezone.utc), last_error=f"DHAN_FEED_{exc.code}")
                logger.warning("Dhan feed rejected session", extra={"user_id": self.user_id, "code": exc.code})
            except Exception as exc:
                await self._emit_health("reconnecting", reconnect_delta=1,
                                        decode_error_delta=int(isinstance(exc, DhanProtocolError)),
                                        disconnected_at=datetime.now(timezone.utc),
                                        last_error=type(exc).__name__)
                logger.warning("Dhan feed connection interrupted", extra={"user_id": self.user_id,
                                                                           "error_type": type(exc).__name__})
            if not self._stopping.is_set():
                await asyncio.sleep(delay + random.uniform(0, min(1.0, delay / 4)))
                delay = min(delay * 2, self.reconnect_max_seconds)


class DhanFeedSupervisor:
    def __init__(self, on_packet: PacketCallback, request_code: int = 17,
                 reconnect_max_seconds: float = 30.0, poll_seconds: float = 2.0):
        self.on_packet = on_packet
        self.request_code = request_code
        self.reconnect_max_seconds = reconnect_max_seconds
        self.poll_seconds = poll_seconds
        self._sessions: dict[str, tuple[str, DhanFeedSession, asyncio.Task]] = {}
        self._subscriptions_by_user: dict[str, set[str]] = {}
        self._stale_alerted: set[str] = set()

    async def _health(self, user_id: str, state: str, details: dict[str, Any]) -> None:
        await market_bus.update_feed_health(user_id, state=state, **details)

    async def _fatal(self, user_id: str, code: int) -> None:
        with SessionLocal() as db:
            connection = db.scalar(select(BrokerConnection).where(
                BrokerConnection.user_id == user_id, BrokerConnection.broker == BrokerName.DHAN))
            if connection:
                connection.status = BrokerStatus.REAUTH_REQUIRED if code in {807, 808, 809} else BrokerStatus.ERROR
                connection.error_code = f"DHAN_FEED_{code}"
                db.commit()
        await market_bus.update_feed_health(
            user_id, state="reauth_required" if code in {807, 808, 809} else "error",
            disconnected_at=datetime.now(timezone.utc), last_error=f"DHAN_FEED_{code}",
        )

    @staticmethod
    def _expired(connection: BrokerConnection) -> bool:
        expires = connection.token_expires_at
        if expires is None:
            return False
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        return expires <= datetime.now(timezone.utc)

    def _load_user(self, user_id: str, instrument_ids: set[str]) -> tuple[str, str, str, list[DhanSubscription]] | None:
        with SessionLocal() as db:
            connection = db.scalar(select(BrokerConnection).where(
                BrokerConnection.user_id == user_id,
                BrokerConnection.broker == BrokerName.DHAN,
                BrokerConnection.status == BrokerStatus.CONNECTED,
            ))
            if connection is None:
                return None
            if self._expired(connection):
                connection.status = BrokerStatus.REAUTH_REQUIRED
                connection.error_code = "DHAN_TOKEN_EXPIRED"
                db.commit()
                return None
            try:
                credentials = decrypt_json(connection.encrypted_credentials)
            except (TypeError, ValueError, json.JSONDecodeError):
                logger.error("Unable to decrypt Dhan credentials", extra={"user_id": user_id})
                return None
            access_token = credentials.get("access_token")
            client_id = credentials.get("client_id") or connection.broker_client_id
            if not access_token or not client_id:
                return None
            rows = db.scalars(select(Instrument).where(
                Instrument.id.in_(instrument_ids), Instrument.is_active.is_(True))).all()
            subscriptions: list[DhanSubscription] = []
            for row in rows:
                try:
                    subscriptions.append(dhan_subscription(row))
                except DhanProtocolError as exc:
                    logger.warning("Skipping instrument for Dhan feed", extra={"instrument_id": row.id,
                                                                               "reason": str(exc)})
            fingerprint = hashlib.sha256(f"{client_id}\0{access_token}".encode()).hexdigest()
            return fingerprint, str(client_id), str(access_token), subscriptions

    async def reconcile(self) -> None:
        grouped: dict[str, set[str]] = defaultdict(set)
        for user_id, instrument_id in await market_bus.active_subscriptions():
            grouped[user_id].add(instrument_id)
        self._subscriptions_by_user = dict(grouped)
        for user_id in set(self._sessions) - set(grouped):
            _, session, task = self._sessions.pop(user_id)
            session.stop()
            task.cancel()
            await market_bus.update_feed_health(user_id, state="idle", subscription_count=0, last_error="")
        for user_id, instrument_ids in grouped.items():
            loaded = self._load_user(user_id, instrument_ids)
            if loaded is None:
                await market_bus.update_feed_health(user_id, state="awaiting_credentials",
                                                    subscription_count=len(instrument_ids))
                existing = self._sessions.pop(user_id, None)
                if existing:
                    existing[1].stop()
                    existing[2].cancel()
                continue
            fingerprint, client_id, access_token, subscriptions = loaded
            current = self._sessions.get(user_id)
            if not subscriptions:
                await market_bus.update_feed_health(user_id, state="instrument_error",
                                                    subscription_count=0, last_error="NO_VALID_DHAN_INSTRUMENTS")
                if current:
                    current[1].stop()
                    current[2].cancel()
                    self._sessions.pop(user_id, None)
                continue
            if current and (current[0] != fingerprint or current[2].done()):
                current[1].stop()
                current[2].cancel()
                self._sessions.pop(user_id, None)
                current = None
            if current is None:
                session = DhanFeedSession(user_id, client_id, access_token, self.request_code,
                    self.reconnect_max_seconds, self.on_packet, self._fatal, self._health)
                task = asyncio.create_task(session.run(), name=f"dhan-feed-{user_id}")
                self._sessions[user_id] = (fingerprint, session, task)
                current = self._sessions[user_id]
            current[1].replace_subscriptions(subscriptions)
            await market_bus.update_feed_health(user_id, subscription_count=len(subscriptions))

    async def _alert_on_stale_feeds(self) -> None:
        for user_id, instrument_ids in self._subscriptions_by_user.items():
            health = await market_bus.feed_health(user_id, len(instrument_ids))
            if health["stale"] and user_id not in self._stale_alerted:
                logger.error("Dhan feed is stale", extra={"user_id": user_id,
                                                          "last_tick_at": health["last_tick_at"]})
                self._stale_alerted.add(user_id)
            elif not health["stale"]:
                self._stale_alerted.discard(user_id)

    async def run(self, stopping: asyncio.Event) -> None:
        try:
            while not stopping.is_set():
                try:
                    await self.reconcile()
                    await self._alert_on_stale_feeds()
                except Exception:
                    logger.exception("Dhan subscription reconciliation failed")
                try:
                    await asyncio.wait_for(stopping.wait(), timeout=self.poll_seconds)
                except TimeoutError:
                    pass
        finally:
            tasks = []
            for _, session, task in self._sessions.values():
                session.stop()
                task.cancel()
                tasks.append(task)
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            self._sessions.clear()
