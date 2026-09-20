import asyncio
from datetime import datetime, timezone

from sqlalchemy import select

from app.market.bus import market_bus
from app.market.contracts import NormalizedQuote
from app.models import Instrument, OrderStatus, TradeExecution, TradingOrder, TradingPosition
from app.trading.engine import PaperTradingEngine
from app.trading.service import order_manager
from tests.conftest import auth, login, register_verified


def instrument(db):
    row = Instrument(exchange="NSE", segment="EQ", symbol="1333", trading_symbol="SBIN-EQ",
                     name="State Bank of India", instrument_type="EQUITY", broker_tokens={"DHAN": "1333"})
    db.add(row)
    db.commit()
    return row


def identity(client):
    register_verified(client, email="trader@example.com")
    token = login(client, email="trader@example.com")["access_token"]
    user_id = client.get("/api/v1/users/me", headers=auth(token)).json()["id"]
    return token, user_id


def quote(row, user_id, ltp=100.0, bid=99.9, ask=100.1):
    value = NormalizedQuote(
        instrument_id=row.id, exchange=row.exchange, segment=row.segment,
        trading_symbol=row.trading_symbol, source="TEST", sequence=1,
        exchange_timestamp=datetime.now(timezone.utc), ltp=ltp, bid=bid, ask=ask,
    )
    asyncio.run(market_bus.publish(user_id, value))
    return value


def payload(row, client_order_id="paper-001", **updates):
    body = {
        "instrument_id": row.id, "client_order_id": client_order_id,
        "side": "BUY", "order_type": "MARKET", "product_type": "INTRADAY",
        "validity": "DAY", "quantity": 5,
    }
    body.update(updates)
    return body


def test_trading_disabled_by_default(client, db):
    row = instrument(db)
    token, user_id = identity(client)
    quote(row, user_id)
    response = client.post("/api/v1/trading/orders", headers=auth(token), json=payload(row))
    assert response.status_code == 403
    assert response.json()["detail"] == "TRADING_DISABLED"


def test_paper_market_order_is_filled_idempotently(client, db, monkeypatch):
    monkeypatch.setattr(order_manager.settings, "trading_mode", "paper")
    row = instrument(db)
    token, user_id = identity(client)
    quote(row, user_id)

    first = client.post("/api/v1/trading/orders", headers=auth(token), json=payload(row))
    second = client.post("/api/v1/trading/orders", headers=auth(token), json=payload(row))
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    assert first.json()["status"] == "FILLED"
    assert db.scalar(select(TradingOrder)).filled_quantity == 5
    assert db.scalar(select(TradeExecution)).quantity == 5
    position = db.scalar(select(TradingPosition))
    assert position.quantity == 5


def test_paper_idempotency_conflict_and_kill_switch(client, db, monkeypatch):
    monkeypatch.setattr(order_manager.settings, "trading_mode", "paper")
    row = instrument(db)
    token, user_id = identity(client)
    quote(row, user_id)
    assert client.post("/api/v1/trading/orders", headers=auth(token), json=payload(row)).status_code == 201
    conflict = client.post("/api/v1/trading/orders", headers=auth(token),
                           json=payload(row, quantity=6))
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == "IDEMPOTENCY_CONFLICT"

    assert client.post("/api/v1/trading/kill-switch", headers=auth(token),
                       json={"halted": True, "reason": "acceptance test"}).status_code == 200
    halted = client.post("/api/v1/trading/orders", headers=auth(token),
                         json=payload(row, client_order_id="paper-002"))
    assert halted.status_code == 409
    assert halted.json()["detail"] == "TRADING_HALTED"


def test_resting_limit_fills_on_market_quote(client, db, monkeypatch):
    monkeypatch.setattr(order_manager.settings, "trading_mode", "paper")
    row = instrument(db)
    token, user_id = identity(client)
    first_quote = quote(row, user_id)
    created = client.post("/api/v1/trading/orders", headers=auth(token), json=payload(
        row, order_type="LIMIT", limit_price=99.0, client_order_id="limit-001"))
    assert created.status_code == 201
    assert created.json()["status"] == "OPEN"

    crossing = first_quote.model_copy(update={"sequence": 2, "ltp": 98.9, "bid": 98.8, "ask": 98.9})
    assert PaperTradingEngine().process_quote(db, user_id, crossing) == 1
    result = client.get(f"/api/v1/trading/orders/{created.json()['id']}", headers=auth(token))
    assert result.json()["status"] == "FILLED"


def test_orders_are_tenant_isolated(client, db, monkeypatch):
    monkeypatch.setattr(order_manager.settings, "trading_mode", "paper")
    row = instrument(db)
    first_token, first_user = identity(client)
    quote(row, first_user)
    created = client.post("/api/v1/trading/orders", headers=auth(first_token), json=payload(row)).json()
    register_verified(client, email="other-trader@example.com")
    second_token = login(client, email="other-trader@example.com")["access_token"]
    assert client.get(f"/api/v1/trading/orders/{created['id']}", headers=auth(second_token)).status_code == 404
    assert client.get("/api/v1/trading/orders", headers=auth(second_token)).json() == []


def test_live_mode_requires_all_production_gates(client, db, monkeypatch):
    monkeypatch.setattr(order_manager.settings, "trading_mode", "live")
    monkeypatch.setattr(order_manager.settings, "trading_live_confirmation", "")
    monkeypatch.setattr(order_manager.settings, "dhan_static_ip_confirmed", False)
    row = instrument(db)
    token, user_id = identity(client)
    quote(row, user_id)
    response = client.post("/api/v1/trading/orders", headers=auth(token), json=payload(row, client_order_id="live-001"))
    assert response.status_code == 403
    assert response.json()["detail"] == "LIVE_TRADING_NOT_CONFIRMED"
