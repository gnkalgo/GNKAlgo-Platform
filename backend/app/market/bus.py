import asyncio
import time
from collections import defaultdict
from collections.abc import AsyncIterator
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
        redis = await self.redis()
        if redis:
            wire = quote.wire()
            async with redis.pipeline(transaction=False) as pipe:
                pipe.set(self._key("latest", user_id, quote.instrument_id), wire, ex=self.settings.market_quote_ttl_seconds)
                pipe.publish(self._key("quote", user_id, quote.instrument_id), wire)
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

    async def status(self) -> dict:
        subscriptions = await self.active_subscriptions()
        return {"provider": self.settings.market_feed_provider, "redis_connected": await self.redis() is not None,
                "active_clients": len(self._clients), "active_subscriptions": len(subscriptions)}

    async def reset(self) -> None:
        self._latest.clear()
        self._queues.clear()
        self._clients.clear()


market_bus = MarketBus()
