# Phase 7.2 — Dhan Live Order Execution and Reconciliation

Phase 7 is deliberately split into two acceptance gates. Phase 7.1 supplies a
deterministic paper-trading path. Phase 7.2 reuses the same order, risk,
execution, position and audit contracts for Dhan live execution. Live orders
are disabled by default and cannot be enabled accidentally by a single flag.

The Dhan adapter follows the official [Dhan v2 order API](https://dhanhq.co/docs/v2/orders/):
placement, modification and cancellation use the v2 order endpoints;
`correlationId` is the local idempotency key; order and trade books are the
reconciliation authority. The live adapter never automatically retries a POST
whose result is unknown.

## Delivered controls

- Tenant-owned orders, executions, positions and user kill switches
- MARKET and LIMIT orders, DAY and IOC validity, and CNC/INTRADAY/MARGIN/MTF products
- Quantity, notional, open-order and absolute-position limits before submission
- Fresh quote requirement for live risk valuation
- Idempotent `client_order_id` values (maximum 30 characters)
- Dhan status mapping for TRANSIT, PENDING, PART_TRADED, TRADED, CANCELLED, REJECTED and EXPIRED
- A dedicated `order-reconciler` service that imports broker executions once by `exchangeTradeId`
- Ambiguous results remain `UNKNOWN` until order-book reconciliation; placement is not retried
- Interactive per-user kill switch and sanitized order audit events
- Explicit static-IP and live-trading confirmations validated at startup in production

## Phase 7.1 paper acceptance

Deploy with a live or simulated market feed and paper execution only:

```dotenv
TRADING_MODE=paper
TRADING_LIVE_CONFIRMATION=
DHAN_STATIC_IP_CONFIRMED=false
```

Acceptance requires all of the following:

1. A MARKET order fills from the latest tenant-owned quote and creates one execution and one position update.
2. Repeating the same `client_order_id` returns the original order; changing its immutable fields returns `IDEMPOTENCY_CONFLICT`.
3. A non-marketable LIMIT order rests, then fills when a subsequent quote crosses it.
4. Quantity, notional, open-order and position limits reject before submission.
5. Stale or missing quotes reject MARKET orders.
6. Cancel, modify, position/P&L, tenant isolation and the user kill switch pass.
7. `python -m pytest` passes and the paper system completes the agreed soak period without unexplained order-state divergence.

Do not proceed to live mode when any paper acceptance item is unresolved.

## Oracle VM deployment

From `/opt/gnkalgo`, back up PostgreSQL and the `.env` file, then deploy the
schema and services while trading is still disabled:

```bash
git pull --ff-only origin main
cp .env ".env.backup.$(date +%Y%m%d%H%M%S)"
sed -i 's/^TRADING_MODE=.*/TRADING_MODE=disabled/' .env
docker compose config --quiet
./deploy/oracle/gnkalgo-control.sh reload
docker compose exec api alembic current
docker compose exec api alembic heads
docker compose ps
curl -fsS https://api.gnkalgo.com/health
```

The migration head must be `0003_phase_7_order_management`. Confirm the API,
market worker and order reconciler remain healthy. The reconciler intentionally
sleeps while trading is disabled or in paper mode.

## Live enablement gate

Before live enablement, the operator must complete all external controls:

- Dhan order APIs are enabled for the account.
- The Oracle VM's stable egress IP is whitelisted with Dhan. Dhan documents a
  static-IP requirement for order placement, modification and cancellation.
- Each live user has a connected, unexpired Dhan credential.
- Production risk limits have been reviewed independently.
- The Phase 7.1 acceptance report and database backup are retained.
- The operator has tested the kill switch and has a supervised canary plan.

Only then set all three gates together:

```dotenv
TRADING_MODE=live
TRADING_LIVE_CONFIRMATION=ENABLE_DHAN_LIVE_ORDERS
DHAN_STATIC_IP_CONFIRMED=true
```

Validate and recreate the application services:

```bash
docker compose config --quiet
./deploy/oracle/gnkalgo-control.sh reload
docker compose ps
docker compose logs --tail 200 api order-reconciler
curl -fsS https://api.gnkalgo.com/health
```

The health response must report `"phase":"7.2"`, `"trading_enabled":true`
and `"trading_mode":"live"`. Use a separately approved minimum-size canary;
verify its local order, Dhan order, execution, quantity and average price all
agree before increasing exposure.

## API surface

- `GET /api/v1/trading/status`
- `GET|POST /api/v1/trading/orders`
- `GET|PATCH /api/v1/trading/orders/{id}`
- `POST /api/v1/trading/orders/{id}/cancel`
- `GET /api/v1/trading/executions`
- `GET /api/v1/trading/positions`
- `POST /api/v1/trading/kill-switch` (interactive session only)
- `POST /api/v1/trading/global-kill-switch` (administrator session only)

API keys require `orders:read` or `orders:write`. The existing policy prevents
retail users from self-issuing an `orders:write` API key.

## Emergency stop and rollback

Use the UI kill switch first when available. For a system-wide stop, disable
new submissions and recreate the API/reconciler:

```bash
sed -i 's/^TRADING_MODE=.*/TRADING_MODE=disabled/' .env
sed -i 's/^TRADING_LIVE_CONFIRMATION=.*/TRADING_LIVE_CONFIRMATION=/' .env
sed -i 's/^DHAN_STATIC_IP_CONFIRMED=.*/DHAN_STATIC_IP_CONFIRMED=false/' .env
./deploy/oracle/gnkalgo-control.sh reload
```

Disabling GnKAlgo does not cancel orders already accepted by Dhan. Operators
must inspect the Dhan order book and cancel or manage remaining broker orders
through an approved channel. Preserve the Phase 7 tables for investigation;
do not downgrade the database during an incident.

## Verification

```bash
cd backend && python -m pytest
cd ../frontend && npm run typecheck && npm run build
cd .. && docker compose config --quiet
```

Tests cover the default-disabled gate, paper fills, idempotency conflict,
resting limit fills, tenant isolation, kill switch, live confirmation gate,
Dhan state mapping and the exact v2 placement payload.
