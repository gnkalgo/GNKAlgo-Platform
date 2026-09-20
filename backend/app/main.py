from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from .config import get_settings
from .routers import admin, api_keys, auth, brokers, market, sessions, trading, users

settings = get_settings()
app = FastAPI(title="GnKAlgo API", version="0.7.2", docs_url="/docs" if settings.environment != "production" else None, redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_credentials=True, allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"], allow_headers=["Authorization", "Content-Type", "X-GnK-API-Key"])

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if settings.environment == "production": response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response

app.add_middleware(SecurityHeadersMiddleware)
for router in (auth.router, users.router, sessions.router, brokers.router, api_keys.router, market.router, trading.router, admin.router): app.include_router(router, prefix="/api/v1")

@app.get("/health", tags=["system"])
def health(): return {"status": "ok", "phase": "7.2", "trading_enabled": settings.trading_mode != "disabled", "trading_mode": settings.trading_mode, "market_data_enabled": settings.market_feed_provider != "disabled"}
