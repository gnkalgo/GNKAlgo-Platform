import asyncio
import time
from collections import defaultdict
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

from ..config import get_settings
from .contracts import NormalizedQuote, SubscriptionCommand


class MarketBus:
    """Redis data plane with an in-process fallback for tests."""

    def __init__(self) -> None:
        self.settings = get_settings()
        self._redis: Any | None = None
        self._redis_retry_at = 0.0
        self._latest: dict[tuple[str, str], NormalizedQuote] = {}
        self._queues: dict[str, set[asyncio.Queue[NormalizedQuote]]] = defaultdict(set)
        self._clients: dict[tuple[str, str], set[str]] = {}
        self._health: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def _parse_time(value: Any) -> datetime | None:
        if not value:
            return None
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))

    def _merge_health(self, user_id: str, fields: dict[str, Any], reconnect_delta: int = 0,
                      decode_error_delta: int = 0, tick_delta: int = 0) -> None:
        health = self._health.setdefault(user_id, {"reconnect_count": 0, "decode_errors": 0, "tick_count": 0})
        health.update(fields)
        health["reconnect_count"] += reconnect_delta
        health["decode_errors"] += decode_error_delta
        health["tick_count"] += tick_delta

    async def redis(self) -> Any | None:
        if not self.settings.redis_url or time.monotonic() < self._redis_retry_at:
            return None
        if self._redis is None:
            try:
                from redis.asyncio import Redis
                self._redis = Redis.from_url(self.settings.redis_url, decode_responses=True)
                await self._redis.ping()
            except Exception:
                if self._redis is not None:
                    await self._redis.aclose()
                self._redis = None
                self._redis_retry_at = time.monotonic() + 5
        return self._redis

    def _key(self, *parts: str) -> str:
        return ":".join((self.settings.market_redis_prefix, *parts))

    async def publish(self, user_id: str, quote: NormalizedQuote) -> None:
        now = self._now()
        latency_ms = max(0.0, (quote.received_at - quote.exchange_timestamp).total_seconds() * 1000)
        health_fields = {
            "state": "connected",
            "last_tick_at": now.isoformat(),
            "last_exchange_timestamp": quote.exchange_timestamp.isoformat(),
            "quote_latency_ms": round(latency_ms, 3),
            "last_error": "",
            "updated_at": now.isoformat(),
        }
        self._merge_health(user_id, health_fields, tick_delta=1)
        redis = await self.redis()
        if redis:
            wire = quote.wire()
            async with redis.pipeline(transaction=False) as pipe:
                pipe.set(self._key("latest", user_id, quote.instrument_id), wire, ex=self.settings.market_quote_ttl_seconds)
                pipe.publish(self._key("quote", user_id, quote.instrument_id), wire)
                pipe.hset(self._key("health", user_id), mapping=health_fields)
                pipe.hincrby(self._key("health", user_id), "tick_count", 1)
                pipe.expire(self._key("health", user_id), self.settings.market_feed_health_ttl_seconds)
                await pipe.execute()
            return
        self._latest[(user_id, quote.instrument_id)] = quote
        for queue in tuple(self._queues[user_id]):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(quote)

    async def latest(self, user_id: str, instrument_ids: list[str]) -> list[NormalizedQuote]:
        redis = await self.redis()
        if redis and instrument_ids:
            keys = [self._key("latest", user_id, item) for item in instrument_ids]
            values = await redis.mget(keys)
            return [NormalizedQuote.from_wire(value) for value in values if value]
        return [self._latest[(user_id, item)] for item in instrument_ids if (user_id, item) in self._latest]

    async def listen(self, user_id: str) -> AsyncIterator[NormalizedQuote]:
        redis = await self.redis()
        if redis:
            pubsub = redis.pubsub(ignore_subscribe_messages=True)
            await pubsub.psubscribe(self._key("quote", user_id, "*"))
            try:
                while True:
                    message = await pubsub.get_message(timeout=1.0)
                    if message and message.get("type") == "pmessage":
                        yield NormalizedQuote.from_wire(message["data"])
                    else:
                        await asyncio.sleep(0.01)
            finally:
                await pubsub.aclose()
        else:
            queue: asyncio.Queue[NormalizedQuote] = asyncio.Queue(maxsize=256)
            self._queues[user_id].add(queue)
            try:
                while True:
                    yield await queue.get()
            finally:
                self._queues[user_id].discard(queue)

    async def replace_subscriptions(self, user_id: str, client_id: str, instrument_ids: set[str], mode: str = "quote") -> None:
        old = self._clients.get((user_id, client_id), set())
        added, removed = instrument_ids - old, old - instrument_ids
        self._clients[(user_id, client_id)] = set(instrument_ids)
        redis = await self.redis()
        if not redis:
            return
        client_key = self._key("client", user_id, client_id)
        async with redis.pipeline(transaction=True) as pipe:
            pipe.delete(client_key)
            if instrument_ids:
                pipe.sadd(client_key, *instrument_ids)
                pipe.expire(client_key, 120)
            for instrument_id in added:
                pipe.sadd(self._key("subscribers", user_id, instrument_id), client_id)
                pipe.expire(self._key("subscribers", user_id, instrument_id), 120)
            for instrument_id in removed:
                pipe.srem(self._key("subscribers", user_id, instrument_id), client_id)
            await pipe.execute()
        for action, ids in (("subscribe", added), ("unsubscribe", removed)):
            if ids:
                command = SubscriptionCommand(action=action, user_id=user_id, instrument_ids=sorted(ids), mode=mode, client_id=client_id)
                await redis.publish(self._key("control"), command.model_dump_json())

    async def disconnect_client(self, user_id: str, client_id: str) -> None:
        await self.replace_subscriptions(user_id, client_id, set())
        self._clients.pop((user_id, client_id), None)

    async def active_subscriptions(self) -> set[tuple[str, str]]:
        redis = await self.redis()
        if redis:
            result: set[tuple[str, str]] = set()
            async for key in redis.scan_iter(match=self._key("subscribers", "*", "*"), count=500):
                if await redis.scard(key):
                    suffix = key.removeprefix(self._key("subscribers") + ":")
                    user_id, instrument_id = suffix.split(":", 1)
                    result.add((user_id, instrument_id))
            return result
        return {(user_id, item) for (user_id, _), ids in self._clients.items() for item in ids}

    async def update_feed_health(self, user_id: str, *, state: str | None = None,
                                 subscription_count: int | None = None, connected_at: datetime | None = None,
                                 disconnected_at: datetime | None = None, last_error: str | None = None,
                                 reconnect_delta: int = 0, decode_error_delta: int = 0) -> None:
        now = self._now()
        fields: dict[str, Any] = {"updated_at": now.isoformat()}
        if state is not None:
            fields["state"] = state
        if subscription_count is not None:
            fields["subscription_count"] = subscription_count
        if connected_at is not None:
            fields["connected_at"] = connected_at.isoformat()
        if disconnected_at is not None:
            fields["disconnected_at"] = disconnected_at.isoformat()
        if last_error is not None:
            fields["last_error"] = last_error
        self._merge_health(user_id, fields, reconnect_delta, decode_error_delta)
        redis = await self.redis()
        if redis:
            key = self._key("health", user_id)
            async with redis.pipeline(transaction=False) as pipe:
                pipe.hset(key, mapping=fields)
                if reconnect_delta:
                    pipe.hincrby(key, "reconnect_count", reconnect_delta)
                if decode_error_delta:
                    pipe.hincrby(key, "decode_errors", decode_error_delta)
                pipe.expire(key, self.settings.market_feed_health_ttl_seconds)
                await pipe.execute()

    async def feed_health(self, user_id: str, subscription_count: int) -> dict[str, Any]:
        redis = await self.redis()
        raw = await redis.hgetall(self._key("health", user_id)) if redis else self._health.get(user_id, {})
        raw = dict(raw or {})
        state = raw.get("state") or ("disabled" if self.settings.market_feed_provider == "disabled" else "idle")
        last_tick = self._parse_time(raw.get("last_tick_at"))
        connected_at = self._parse_time(raw.get("connected_at"))
        now = self._now()
        freshness_reference = last_tick or connected_at
        stale = bool(subscription_count and state == "connected" and freshness_reference and
                     (now - freshness_reference).total_seconds() > self.settings.market_stale_after_seconds)
        healthy = not subscription_count or (state == "connected" and not stale)
        if self.settings.market_feed_provider == "disabled":
            healthy = True
        return {
            "provider": self.settings.market_feed_provider,
            "state": "degraded" if stale else state,
            "healthy": healthy,
            "stale": stale,
            "subscription_count": subscription_count,
            "connected_at": connected_at,
            "disconnected_at": self._parse_time(raw.get("disconnected_at")),
            "last_tick_at": last_tick,
            "last_exchange_timestamp": self._parse_time(raw.get("last_exchange_timestamp")),
            "quote_latency_ms": float(raw["quote_latency_ms"]) if raw.get("quote_latency_ms") not in {None, ""} else None,
            "tick_count": int(raw.get("tick_count", 0)),
            "reconnect_count": int(raw.get("reconnect_count", 0)),
            "decode_errors": int(raw.get("decode_errors", 0)),
            "last_error": raw.get("last_error") or None,
            "updated_at": self._parse_time(raw.get("updated_at")),
        }

    async def status(self, user_id: str) -> dict:
        subscriptions = await self.active_subscriptions()
        user_subscriptions = {instrument_id for owner, instrument_id in subscriptions if owner == user_id}
        return {"provider": self.settings.market_feed_provider, "redis_connected": await self.redis() is not None,
                "active_clients": sum(owner == user_id for owner, _ in self._clients),
                "active_subscriptions": len(user_subscriptions),
                "feed": await self.feed_health(user_id, len(user_subscriptions))}

    async def reset(self) -> None:
        self._latest.clear()
        self._queues.clear()
        self._clients.clear()
        self._health.clear()


market_bus = MarketBus()
