# Phase 8 — Strategy Engine and Signal-to-Order Pipeline

Phase 8 adds a constrained, versioned `SMA_CROSS` strategy. It evaluates only
completed, tenant-owned candles, records one durable run per strategy/candle,
and sends accepted signals through the existing Phase 7 order manager. It does
not execute user-supplied Python, JavaScript, expressions, or arbitrary SQL.

## Safety boundaries

- `STRATEGY_ENGINE_ENABLED=false` by default. `TRADING_MODE=disabled` also
  prevents strategy order routing.
- Strategy creation only creates a `DRAFT`; an interactive session must activate
  it. Live activation additionally requires MFA, the exact per-request
  `ENABLE_LIVE_STRATEGY` confirmation, `TRADING_MODE=live` and the independent
  `STRATEGY_LIVE_CONFIRMATION=ENABLE_LIVE_STRATEGIES` environment gate.
- Existing Dhan order and static-IP gates still apply. Activating a strategy
  never changes trading mode or enables manual/live broker APIs.
- Completed candles must be recent and contiguous. A stale quote, missing
  history, feed gap, inactive instrument, open strategy order, exceeded daily
  limit, or kill switch prevents submission.
- Each candle run is claimed durably before any broker side effect. The order
  uses a stable strategy/candle correlation ID. An incomplete `STARTED` run
  or an unresolved `CREATED` signal after a crash moves the strategy to
  `ERROR` for manual order-book reconciliation. Reactivation is blocked until
  those records are resolved; the worker never replays them automatically.
- The strategy only opens a long position and sells up to its own filled
  exposure; it does not short or assume manually placed positions belong to it.
- Strategy versions, runs, diagnostics, signals, order links and sanitized
  audit events remain queryable. `preview` is read-only and never places orders.
- Run exactly one strategy-worker replica. The unique candle claim is a second
  replay guard, not a substitute for operational singleton control.

## Configuration

Paper acceptance:

```dotenv
TRADING_MODE=paper
STRATEGY_ENGINE_ENABLED=true
STRATEGY_LIVE_CONFIRMATION=
STRATEGY_POLL_SECONDS=2
STRATEGY_MAX_ORDERS_PER_USER_PER_DAY=20
STRATEGY_MAX_ORDER_ATTEMPTS_PER_MINUTE=8
STRATEGY_MAX_CANDLE_LAG_SECONDS=180
```

Keep the live strategy confirmation blank until independent broker, compliance,
and production acceptance reviews are complete. [SEBI's retail-algo circular](https://www.sebi.gov.in/legal/circulars/feb-2025/safer-participation-of-retail-investors-in-algorithmic-trading_91614.html)
and [Dhan's order API](https://dhanhq.co/docs/v2/orders/) should be checked
against your broker agreement and current operating requirements before any
automated live order. This code does not itself certify regulatory approval.

Live rollout additionally needs the existing Phase 7 live gates, connected
unexpired Dhan credentials, whitelisted static egress IP, MFA, an approved
canary strategy, and:

```dotenv
STRATEGY_LIVE_CONFIRMATION=ENABLE_LIVE_STRATEGIES
```

Do not enable live strategies merely because manual Dhan orders are accepted.
To stop new automated orders immediately, set `STRATEGY_ENGINE_ENABLED=false`
and recreate the strategy worker/API, or pause the strategy through the API.
Disabling the engine does not cancel previously accepted broker orders; inspect
and manage those separately through the approved Dhan channel.

## API

- `GET|POST /api/v1/strategies`
- `GET|PATCH /api/v1/strategies/{id}`
- `POST /api/v1/strategies/{id}/activate` and `/pause`
- `GET /api/v1/strategies/{id}/versions`, `/runs`, `/signals`, `/preview`

Reads require `strategies:read` for API keys; writes require
`strategies:write`, which existing policy does not allow retail users to
self-issue. Activation and pause require an interactive session. Every query
is scoped to the authenticated tenant.

## Oracle VM deployment and acceptance

1. Back up PostgreSQL and `.env`; deploy the code with `STRATEGY_ENGINE_ENABLED=false`.
2. Run `docker compose exec api alembic current` and `alembic heads`; both must
   report `0004_phase_8_strategy_engine` after the API starts.
3. Check `docker compose ps` and `docker compose logs --tail 200 strategy-worker`.
4. In paper mode, enable the engine and reload. Create a strategy, inspect its
   version/preview, activate it, and observe one run and one signal/order on a
   controlled closed-candle cross.
5. Replay the same candle, restart the worker, remove a candle, stale the quote,
   hit the daily order cap, and activate the kill switch. No duplicate or unsafe
   orders should result. Verify pause and restart recovery procedures.
6. Compare all paper executions and positions against the Phase 7 order ledger.
   Keep live strategy automation disabled until independent acceptance is signed.

```bash
docker compose config --quiet
./deploy/oracle/gnkalgo-control.sh reload
docker compose exec api alembic heads
docker compose ps
docker compose logs --tail 200 strategy-worker api market-worker order-reconciler
```

Local verification: `cd backend && python -m pytest`, then
`cd ../frontend && npm run typecheck && npm run build`.
