# GnKAlgo — Phase 1 + Phase 2

Production-oriented identity, broker connection, and scoped API-credential foundation for GnKAlgo. The attached GnKAlgo artwork is used as the application brand asset.

## What is implemented

### Phase 1 — identity and access

- Email/password registration, duplicate protection, strong-password policy, SMTP verification, expiring single-use verification tokens
- Login with generic credential failures, account state checks, five-attempt temporary lockout, application and Nginx throttling
- Google Authenticator-compatible TOTP setup/verification, encrypted seed storage, one-time recovery codes, secure disable flow
- Short-lived JWT access tokens bound to server-side sessions
- Random refresh tokens stored only as SHA-256 digests, rotation on every use, reuse detection and session revocation
- Session listing, individual revocation, logout, logout-all, password reset, password change
- `ADMIN` and `RETAIL` backend-enforced RBAC; admin user enable/disable and role management
- Sanitized audit events for identity, session, MFA, password, broker, API-key, and admin actions

### Phase 2 — brokers and GnKAlgo API keys

- Modular Dhan, FYERS v3, and Upstox OAuth/auth-code adapters
- Short-lived, single-use OAuth state, bound to the initiating user and browser; Dhan's callback uses the same-site HttpOnly state cookie because its documented consent callback returns `tokenId` rather than OAuth `state`
- Encrypted-at-rest broker credentials with Fernet; tokens are never returned by broker response schemas
- Per-user broker ownership on every read/write endpoint, connection test, reconnect, status, and credential-destroying disconnect
- API keys in `gnk_live_<public-id>.<secret>` format; only a keyed SHA-256 digest is persisted
- One-time secret display, scope allow-listing, optional expiration, last-use tracking, rotation, revocation, and deletion after revocation
- API-key authentication enters the same user authorization layer, while API-key management, sessions, MFA, and admin operations require an interactive JWT session
- Retail callers cannot assign `orders:write`, `strategies:write`, or admin scopes. No order, strategy, risk, or trading-execution endpoint exists in Phase 2.

## Repository

```text
backend/                 FastAPI, SQLAlchemy, Alembic, security and tests
frontend/                Next.js App Router UI
nginx/                   reverse proxy and edge login throttling
docker-compose.yml       PostgreSQL, Redis, API, web and Nginx
```

## Local backend

Requires Python 3.12+.

```bash
cd backend
python -m venv .venv
.venv/Scripts/activate        # Windows
pip install -r requirements-dev.txt
copy .env.example .env        # then replace development values
alembic upgrade head
uvicorn app.main:app --reload
```

For local-only testing, `DATABASE_URL=sqlite:///./gnkalgo.db` is supported. Set `EXPOSE_DEV_TOKENS=true` only in development/test to receive verification/reset tokens in responses. It is rejected as an operating practice for production; tokens are delivered through SMTP.

Create the first administrator after migrating:

```bash
python -m app.cli create-admin admin@gnkalgo.com
```

## Local frontend

```bash
cd frontend
copy .env.example .env.local
npm ci
npm run dev
```

The web app runs at `http://localhost:3000`; the API defaults to `http://localhost:8000/api/v1`.

## Production configuration

Copy `.env.example` to `.env` and replace every placeholder. Generate a Fernet key without committing it:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Required production values include strong `JWT_SECRET`, `API_KEY_PEPPER`, `FIELD_ENCRYPTION_KEY`, `POSTGRES_PASSWORD`, SMTP settings, exact CORS/frontend origins, and broker application credentials. `COOKIE_SECURE=true` is mandatory in production. The production stack places Caddy in front of internal Nginx; Caddy obtains and renews public certificates after the DNS names resolve to the VM and ports 80/443 are reachable. PostgreSQL, Redis, FastAPI, Next.js, and Nginx are not published directly.

Register these exact callback URLs with providers (adjust hostname only if your deployment differs):

- `https://api.gnkalgo.com/api/v1/brokers/dhan/callback`
- `https://api.gnkalgo.com/api/v1/brokers/fyers/callback`
- `https://api.gnkalgo.com/api/v1/brokers/upstox/callback`

The legacy `http://gnkalgo.com:5000/dhan/callback` is intentionally not hard-coded. Change a registered callback only after updating the provider console and matching environment variable.

Start the stack:

```bash
chmod +x deploy/oracle/prepare-env.sh
./deploy/oracle/prepare-env.sh
docker compose config
docker compose up --build -d
docker compose exec api python -m app.cli create-admin admin@gnkalgo.com
```

The API container runs `alembic upgrade head` before startup.

## API map

- Auth: `/api/v1/auth/register`, `verify-email`, `login`, `refresh`, `logout`, `logout-all`, `forgot-password`, `reset-password`, `mfa/setup`, `mfa/verify`, `mfa/disable`
- User/session: `/api/v1/users/me`, `/users/me/security`, `/users/me/password`, `/sessions`, `/sessions/{id}/revoke`
- Brokers: `/api/v1/brokers`, `/{broker}/connect`, `/{broker}/callback`, `/{id}`, `/{id}/test`, `/{id}/reconnect`
- API keys: `/api/v1/api-keys`, `/{id}`, `/{id}/rotate`, `/{id}/revoke`, and scoped validation example `/validate/profile`
- Admin: `/api/v1/admin/users`, `/admin/users/{id}`, `/admin/audit`

FastAPI OpenAPI is available at `/docs` outside production.

## Tests and verification

```bash
cd backend && python -m pytest
cd frontend && npm run typecheck && npm run build
```

Coverage includes registration/verification/login, generic failures, refresh rotation/reuse revocation, MFA, password reset, admin RBAC, session ownership, broker callback/state/encryption/ownership, API-key hashing/scopes/rotation/revocation, and unauthenticated denials.

## Rollback

1. Stop accepting traffic and back up PostgreSQL.
2. Deploy the prior application images.
3. Only if Phase 1/2 data may be discarded, run `alembic downgrade base`. This drops the seven new tables and is destructive; a database restore is the preferred rollback.
4. Restore the previous Nginx configuration and broker callback registrations together.

Never downgrade the database while the Phase 1/2 application is still serving traffic.
