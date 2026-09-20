from datetime import datetime, timedelta, timezone

from fastapi.websockets import WebSocketDisconnect

from app.market.adapters import DhanV2Adapter, FyersV3Adapter, UpstoxV3Adapter
from app.market.bus import market_bus
from app.market.candles import CandleAggregator
from app.market.contracts import NormalizedQuote
from app.models import Instrument, MarketCandle, User
from tests.conftest import auth, login, register_verified


def instrument(db, symbol="SBIN"):
    row = Instrument(exchange="NSE", segment="EQ", symbol=symbol, trading_symbol=f"{symbol}-EQ",
        name="State Bank of India", instrument_type="EQUITY", broker_tokens={"DHAN": "1333"})
    db.add(row); db.commit(); return row


def user_token(client, email="market@example.com"):
    register_verified(client, email=email)
    return login(client, email=email)["access_token"]


def quote(row, sequence=1, source="DHAN"):
    return NormalizedQuote(instrument_id=row.id, exchange=row.exchange, segment=row.segment,
        trading_symbol=row.trading_symbol, source=source, sequence=sequence,
        exchange_timestamp=datetime.now(timezone.utc), ltp=812.25, previous_close=800,
        volume=1000, bid=812.2, ask=812.3)


def test_instrument_search_and_authentication(client, db):
    row = instrument(db)
    assert client.get("/api/v1/market/instruments?query=SBIN").status_code == 401
    response = client.get("/api/v1/market/instruments?query=state", headers=auth(user_token(client)))
    assert response.status_code == 200
    assert response.json()[0]["id"] == row.id


def test_latest_quotes_are_tenant_scoped(client, db):
    import asyncio
    row = instrument(db)
    first = user_token(client, "first@example.com")
    second = user_token(client, "second@example.com")
    first_user = client.get("/api/v1/users/me", headers=auth(first)).json()["id"]
    asyncio.run(market_bus.publish(first_user, quote(row)))
    first_response = client.get(f"/api/v1/market/quotes?instrument_ids={row.id}", headers=auth(first))
    second_response = client.get(f"/api/v1/market/quotes?instrument_ids={row.id}", headers=auth(second))
    assert first_response.json()[0]["ltp"] == 812.25
    assert second_response.json() == []


def test_market_health_is_tenant_scoped_and_reports_live_ticks(client, db, monkeypatch):
    import asyncio
    row = instrument(db)
    first = user_token(client, "health-first@example.com")
    second = user_token(client, "health-second@example.com")
    first_user = client.get("/api/v1/users/me", headers=auth(first)).json()["id"]
    monkeypatch.setattr(market_bus.settings, "market_feed_provider", "dhan")
    asyncio.run(market_bus.replace_subscriptions(first_user, "health-client", {row.id}))
    asyncio.run(market_bus.update_feed_health(first_user, state="connected", reconnect_delta=2))
    asyncio.run(market_bus.publish(first_user, quote(row)))

    first_status = client.get("/api/v1/market/status", headers=auth(first)).json()
    second_status = client.get("/api/v1/market/status", headers=auth(second)).json()

    assert first_status["active_subscriptions"] == 1
    assert first_status["feed"]["state"] == "connected"
    assert first_status["feed"]["healthy"] is True
    assert first_status["feed"]["last_tick_at"]
    assert first_status["feed"]["reconnect_count"] == 2
    assert second_status["active_subscriptions"] == 0
    assert second_status["feed"]["state"] == "idle"


def test_market_health_marks_an_active_silent_feed_stale(monkeypatch):
    import asyncio
    monkeypatch.setattr(market_bus.settings, "market_feed_provider", "dhan")
    monkeypatch.setattr(market_bus.settings, "market_stale_after_seconds", 5.0)
    market_bus._health["stale-user"] = {
        "state": "connected",
        "connected_at": (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat(),
        "reconnect_count": 0,
        "decode_errors": 0,
    }
    health = asyncio.run(market_bus.feed_health("stale-user", 1))
    assert health["state"] == "degraded"
    assert health["stale"] is True
    assert health["healthy"] is False


def test_market_api_key_scope_is_enforced(client, db):
    instrument(db)
    token = user_token(client)
    denied_key = client.post("/api/v1/api-keys", headers=auth(token),
        json={"name": "profile only", "scopes": ["profile:read"]}).json()["key"]
    allowed_key = client.post("/api/v1/api-keys", headers=auth(token),
        json={"name": "market reader", "scopes": ["market:read"]}).json()["key"]
    assert client.get("/api/v1/market/instruments", headers={"X-GnK-API-Key": denied_key}).status_code == 403
    assert client.get("/api/v1/market/instruments", headers={"X-GnK-API-Key": allowed_key}).status_code == 200


def test_websocket_ticket_is_single_use(client, db):
    row = instrument(db)
    token = user_token(client)
    ticket = client.post("/api/v1/market/ws-ticket", headers=auth(token)).json()["ticket"]
    with client.websocket_connect(f"/api/v1/market/stream?ticket={ticket}") as websocket:
        assert websocket.receive_json()["type"] == "ready"
        websocket.send_json({"action": "subscribe", "instrument_ids": [row.id], "mode": "quote"})
        message = websocket.receive_json()
        assert message["type"] == "subscribed"
        assert message["instrument_ids"] == [row.id]
    try:
        with client.websocket_connect(f"/api/v1/market/stream?ticket={ticket}") as websocket:
            websocket.receive_json()
        assert False, "a consumed ticket must not reconnect"
    except WebSocketDisconnect as exc:
        assert exc.code == 1008


def test_websocket_ticket_honors_session_revocation(client, db):
    token = user_token(client)
    ticket = client.post("/api/v1/market/ws-ticket", headers=auth(token)).json()["ticket"]
    assert client.post("/api/v1/auth/logout", headers=auth(token)).status_code == 200
    try:
        with client.websocket_connect(f"/api/v1/market/stream?ticket={ticket}") as websocket:
            websocket.receive_json()
        assert False, "a ticket from a revoked session must not connect"
    except WebSocketDisconnect as exc:
        assert exc.code == 1008


def test_candles_are_aggregated_and_tenant_scoped(db):
    row = instrument(db)
    user = User(email="candle@example.com", password_hash="not-used", is_verified=True)
    db.add(user); db.commit()
    aggregator = CandleAggregator()
    first = quote(row)
    aggregator.ingest(db, user.id, first)
    later = first.model_copy(update={"sequence": 2, "ltp": 815.0, "volume": 1015.0})
    aggregator.ingest(db, user.id, later)
    candles = db.query(MarketCandle).filter_by(user_id=user.id, instrument_id=row.id).all()
    assert candles
    assert all(item.high == 815.0 and item.close == 815.0 and item.volume == 15.0 for item in candles)
    assert db.query(MarketCandle).filter(MarketCandle.user_id != user.id).count() == 0


def test_broker_adapters_normalize_current_payload_shapes(db):
    row = instrument(db)
    fyers = FyersV3Adapter().normalize(row, {"ltp": 100, "last_traded_time": 1_700_000_000,
        "prev_close_price": 98, "vol_traded_today": 500}, 1)
    upstox = UpstoxV3Adapter().normalize(row, {"ltpc": {"ltp": 101, "ltt": "1700000000000", "cp": 99}}, 2)
    dhan = DhanV2Adapter().normalize(row, {"LTP": 102, "LTT": 1_700_000_002, "volume": 600}, 3)
    assert (fyers.source, upstox.source, dhan.source) == ("FYERS", "UPSTOX", "DHAN")
    assert (fyers.ltp, upstox.ltp, dhan.ltp) == (100, 101, 102)
