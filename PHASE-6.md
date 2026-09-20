# GnKAlgo Phase 6 — Market Data Engine

Phase 6 adds a read-only, tenant-isolated market-data plane to the existing identity and broker platform. Trading remains disabled.

## Components

- `app.market.contracts`: versioned normalized quote and subscription contracts
- `app.market.adapters`: Dhan v2, FYERS v3 and Upstox v3 decoded-payload normalizers
- `app.market.bus`: Redis latest-quote cache, pub/sub fan-out and subscription coordination
- `app.market.candles`: tenant-scoped OHLCV aggregation
- `app.market.worker`: separate feed process; disabled by default, with a deterministic simulation mode for acceptance testing
- `app.routers.market`: instrument search, snapshots, candles, status, one-time WebSocket tickets and live stream
- `/market`: authenticated Next.js watchlist

## API

- `GET /api/v1/market/instruments?query=SBIN`
- `GET /api/v1/market/quotes?instrument_ids=<uuid>`
- `GET /api/v1/market/candles?instrument_id=<uuid>&interval_seconds=60`
- `GET /api/v1/market/status`
- `POST /api/v1/market/ws-ticket`
- `WSS /api/v1/market/stream?ticket=<single-use-ticket>`

REST and WebSocket ticket creation accept an interactive JWT or a GnKAlgo API key with `market:read`. WebSocket tickets expire after 30 seconds and are consumed on first use. Redis keys and candle rows include the owning user ID; one user's feed is never fanned out to another user.

## Instrument master

Apply migrations and import the broker-independent instrument map:

```bash
alembic upgrade head
python -m app.cli import-instruments market-instruments.example.csv
```

The importer upserts instruments by exchange, segment and canonical symbol. Provider subscription identifiers live in the `broker_tokens` JSON object and are never accepted directly from a browser.

## Feed modes

- `MARKET_FEED_PROVIDER=disabled`: production-safe default; APIs are available but no ticks are generated.
- `MARKET_FEED_PROVIDER=simulated`: deterministic acceptance feed. Production configuration rejects this value.
- `MARKET_FEED_PROVIDER=broker`: reserved for the live broker SDK runners. The current worker fails closed until those credential-gated runners are configured.

Decoded Dhan/FYERS/Upstox SDK callbacks must pass through their adapter's `normalize()` method and then call `market_bus.publish(user_id, quote)`. Never publish a broker token, account identifier or unnormalized payload.

## Local acceptance run

```bash
docker compose up --build -d
docker compose exec api python -m app.cli import-instruments market-instruments.example.csv
```

For a non-production acceptance environment, set `MARKET_FEED_PROVIDER=simulated`. Open `/market`, search for an imported instrument and subscribe. The browser sends a heartbeat every 25 seconds; stale Redis subscriber leases expire after 120 seconds.

## Verification

```bash
cd backend && python -m pytest
cd frontend && npm run typecheck && npm run build
docker compose config
```

Tests cover authorization, instrument search, tenant isolation, single-use WebSocket tickets, subscription commands, candle isolation and all three normalizers.

## Production gate

Do not set `MARKET_FEED_PROVIDER=broker` until each live runner has recorded-packet contract tests, broker connection-limit enforcement, reconnect/resubscribe behavior, full-session soak results, market-data entitlement approval, and operational alerting. The API deliberately fails closed instead of presenting simulated or stale prices as live data.
