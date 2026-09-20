# Phase 6.2 — Dhan Production Acceptance and Observability

Phase 6.2 hardens the read-only Dhan market-data path for production acceptance. It does not enable trading, and live data remains disabled unless `MARKET_FEED_PROVIDER=dhan` is explicitly configured.

## Delivered controls

- Tenant-scoped feed health at `GET /api/v1/market/status`
- Connection state, subscription count, last tick, exchange timestamp and quote latency
- Reconnect and binary decode-error counters
- Configurable stale-feed detection with a structured error log on the first stale transition
- Authentication/entitlement failures surfaced as `reauth_required` or `error`
- Idempotent compact/detailed Dhan instrument-master importer
- Market UI visibility for health, latency, reconnects and stale state
- No access token, client identifier or raw credential is emitted through health data or logs

Health is stored under tenant-scoped Redis keys with a five-minute default TTL. If Redis is unavailable, the API reports `redis_connected=false`; production monitoring must treat that as degraded because the API and worker cannot share fallback process memory.

## Configuration

```env
MARKET_FEED_PROVIDER=disabled
MARKET_FEED_HEALTH_TTL_SECONDS=300
MARKET_STALE_AFTER_SECONDS=15
DHAN_MARKET_REQUEST_CODE=17
DHAN_MARKET_RECONNECT_MAX_SECONDS=30
```

Keep the provider disabled until the acceptance checklist passes. `17` selects quote packets; `15` selects ticker packets and `21` full packets.

## Import the official Dhan master

Download Dhan's documented detailed instrument master and import it inside the API container:

```bash
curl -fsSLo /tmp/api-scrip-master-detailed.csv \
  https://images.dhan.co/api-data/api-scrip-master-detailed.csv

sudo docker compose cp /tmp/api-scrip-master-detailed.csv \
  api:/tmp/api-scrip-master-detailed.csv

sudo docker compose exec api \
  python -m app.cli import-dhan-instruments /tmp/api-scrip-master-detailed.csv
```

The importer keys updates by Dhan feed segment plus security ID and stores the explicit `exchange_segment`. Rows that cannot be mapped to a supported Dhan v2 live-feed segment are skipped rather than guessed.

Reference: [Dhan instrument-list documentation](https://dhanhq.co/docs/v2/instruments/).

## Acceptance sequence

1. Connect an entitled Dhan account from the Brokers page and confirm its status is `CONNECTED`.
2. Import the current Dhan master before market open.
3. Set `MARKET_FEED_PROVIDER=dhan`, reload, and subscribe to one liquid NSE equity.
4. Confirm `/api/v1/market/status` reports `connected`, a recent `last_tick_at`, non-stale data, and plausible latency.
5. Compare LTP, bid/ask, volume and timestamps with an independent Dhan terminal sample.
6. Interrupt network access briefly and confirm reconnect count increases and subscriptions resume without browser action.
7. Exercise an expired token and confirm the broker becomes `REAUTH_REQUIRED` without a reconnect storm.
8. Use two accounts and confirm each status, subscription and quote stream remains tenant-isolated.
9. Run through a complete market session and record disconnects, decode errors, p95/p99 quote latency, stale intervals and memory use.
10. Keep the provider enabled only if all gates below pass.

## Production gates

- `decode_errors == 0` during the soak
- No cross-tenant quotes, status or subscription counts
- Reconnect and automatic resubscription succeed
- Stale state is raised within `MARKET_STALE_AFTER_SECONDS`
- Authentication and entitlement failures stop retrying and request reauthorization
- Redis, API and market-worker remain healthy for the complete session
- Operations has a log alert for `Dhan feed is stale`, `DHAN_FEED_806` and `DHAN_FEED_807..809`
- Rollback to `MARKET_FEED_PROVIDER=disabled` has been rehearsed

## Operator verification

```bash
sudo docker compose ps
sudo docker compose logs --no-color --tail=200 market-worker
curl -fsS https://api.gnkalgo.com/health
```

Call the authenticated status endpoint from the application or an API client with `market:read`. Healthy active output includes `feed.healthy=true`, `feed.stale=false`, and a recent `feed.last_tick_at`.

## Fail-closed rollback

```bash
sed -i 's/^MARKET_FEED_PROVIDER=.*/MARKET_FEED_PROVIDER=disabled/' .env
sudo ./deploy/oracle/gnkalgo-control.sh reload
sudo ./deploy/oracle/gnkalgo-control.sh status
```

This stops live broker sessions without deleting PostgreSQL or Redis volumes.
