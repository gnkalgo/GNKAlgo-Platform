# GnKAlgo Phase 6 — Market Data Engine

Phase 6 adds a read-only, tenant-isolated market-data plane to the existing identity and broker platform. Trading remains disabled.

Production acceptance and observability are specified in [PHASE-6.2.md](PHASE-6.2.md).

## Components

- `app.market.contracts`: versioned normalized quote and subscription contracts
- `app.market.adapters`: Dhan v2, FYERS v3 and Upstox v3 decoded-payload normalizers
- `app.market.bus`: Redis latest-quote cache, pub/sub fan-out and subscription coordination
- `app.market.candles`: tenant-scoped OHLCV aggregation
- `app.market.dhan`: Dhan v2 binary decoder, batched subscriptions and credential-gated reconnecting sessions
- `app.market.worker`: separate feed process; disabled by default, with deterministic simulation and Dhan live modes
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

The status response is tenant-scoped and includes connection state, stale detection, last tick and exchange timestamps, quote latency, reconnects and decode errors.

## Instrument master

Apply migrations and import the broker-independent instrument map:

```bash
alembic upgrade head
python -m app.cli import-instruments market-instruments.example.csv
python -m app.cli import-dhan-instruments api-scrip-master-detailed.csv
```

The importer upserts instruments by exchange, segment and canonical symbol. Provider subscription identifiers live in the `broker_tokens` JSON object and are never accepted directly from a browser.

## Feed modes

- `MARKET_FEED_PROVIDER=disabled`: production-safe default; APIs are available but no ticks are generated.
- `MARKET_FEED_PROVIDER=simulated`: deterministic acceptance feed. Production configuration rejects this value.
- `MARKET_FEED_PROVIDER=dhan`: Dhan v2 live feed. A session starts only for a user who has an active Dhan broker connection and a browser subscription; reconnects automatically resubscribe.
- `MARKET_FEED_PROVIDER=broker`: reserved for future broker runners and still fails closed.

The Dhan runner decodes ticker, quote, full-depth, OI, previous-close and disconnect packets. Subscription requests are limited to 100 instruments per message. Authentication failures mark the broker connection for reauthorization, while access tokens are never logged or published. Decoded Dhan packets pass through `DhanV2Adapter` before tenant-scoped publication.

### Enable Dhan after acceptance

1. Confirm the Dhan account has live market-data entitlement and reconnect the account from the Brokers page so a current access token is stored.
2. Import instruments with a `DHAN` security ID in `broker_tokens`; exchange/segment are mapped to Dhan's v2 segment codes. For unusual instruments, use `{"DHAN":{"security_id":"...","exchange_segment":"BSE_FNO"}}`.
3. Leave `DHAN_MARKET_REQUEST_CODE=17` for quote mode, or use `15` for ticker / `21` for full depth.
4. Set `MARKET_FEED_PROVIDER=dhan`, reload the stack, and watch `market-worker` logs. Keep the value `disabled` until this production gate is approved.

Protocol references: [Dhan live market feed v2](https://dhanhq.co/docs/v2/live-market-feed/) and the [official Dhan Python client](https://github.com/dhan-oss/DhanHQ-py/blob/main/src/dhanhq/marketfeed.py).

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

Tests cover authorization, instrument search, tenant isolation, single-use WebSocket tickets, subscription commands, candle isolation, all three normalizers, Dhan binary packet fixtures, depth decoding, segment mapping and 100-instrument request batching.

## Production gate

Do not set `MARKET_FEED_PROVIDER=dhan` until a real entitled account passes packet-level smoke tests, reconnect/resubscribe tests, a full-session soak, quote-latency monitoring and operational alerting. The implementation enforces one Dhan feed connection per active user, below Dhan's five-connection account limit, and keeps production disabled by default. The API deliberately fails closed instead of presenting simulated prices as live data.
