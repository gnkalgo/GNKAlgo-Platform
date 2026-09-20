import asyncio

from sqlalchemy import func, select

from app.models import (
    BrokerConnection, BrokerName, BrokerStatus, Instrument, OrderSide, OrderStatus,
    OrderType, OrderValidity, ProductType, TradeExecution, TradingOrder, TradingPosition, User,
)
from app.trading.dhan import DhanOrderAdapter, map_dhan_status
from app.trading.reconcile import DhanOrderReconciler


def test_dhan_order_status_mapping():
    assert map_dhan_status("TRANSIT") == OrderStatus.PENDING
    assert map_dhan_status("PART_TRADED") == OrderStatus.PARTIALLY_FILLED
    assert map_dhan_status("TRADED") == OrderStatus.FILLED
    assert map_dhan_status("unexpected") == OrderStatus.UNKNOWN


def test_dhan_place_uses_v2_contract(monkeypatch):
    captured = {}
    adapter = DhanOrderAdapter()

    async def request(method, path, credentials, client_id, **kwargs):
        captured.update(method=method, path=path, client_id=client_id, body=kwargs["json"])
        return {"orderId": "broker-1", "orderStatus": "TRANSIT"}

    monkeypatch.setattr(adapter, "_request", request)
    instrument = Instrument(id="instrument-1", exchange="NSE", segment="EQ", symbol="1333",
                            trading_symbol="SBIN-EQ", instrument_type="EQUITY", broker_tokens={"DHAN": "1333"})
    order = TradingOrder(
        client_order_id="correlation-1", side=OrderSide.BUY, order_type=OrderType.LIMIT,
        product_type=ProductType.INTRADAY, validity=OrderValidity.DAY, quantity=10,
        limit_price=800.25, trigger_price=None,
    )
    response = asyncio.run(adapter.place({"access_token": "secret"}, "client-1", instrument, order))
    assert response["orderId"] == "broker-1"
    assert captured["method"] == "POST" and captured["path"] == "/orders"
    assert captured["body"] == {
        "dhanClientId": "client-1", "correlationId": "correlation-1",
        "transactionType": "BUY", "exchangeSegment": "NSE_EQ",
        "productType": "INTRADAY", "orderType": "LIMIT", "validity": "DAY",
        "securityId": "1333", "quantity": 10, "disclosedQuantity": 0,
        "price": 800.25, "triggerPrice": 0, "afterMarketOrder": False,
    }


def test_reconciliation_imports_each_broker_execution_once(db):
    user = User(email="reconcile@example.com", password_hash="unused", is_verified=True)
    instrument = Instrument(exchange="NSE", segment="EQ", symbol="1333", trading_symbol="SBIN-EQ",
                            instrument_type="EQUITY", broker_tokens={"DHAN": "1333"})
    db.add_all([user, instrument])
    db.flush()
    connection = BrokerConnection(user_id=user.id, broker=BrokerName.DHAN, broker_client_id="client-1",
                                  encrypted_credentials="not-used", status=BrokerStatus.CONNECTED)
    order = TradingOrder(
        user_id=user.id, instrument_id=instrument.id, client_order_id="reconcile-1",
        broker_order_id="broker-1", mode="LIVE", side=OrderSide.BUY, order_type=OrderType.LIMIT,
        product_type=ProductType.INTRADAY, validity=OrderValidity.DAY, quantity=10,
        filled_quantity=0, limit_price=800, status=OrderStatus.OPEN, version=1,
    )
    db.add_all([connection, order])
    db.commit()
    broker_orders = [{"orderId": "broker-1", "correlationId": "reconcile-1", "orderStatus": "TRADED"}]
    trades = [{"orderId": "broker-1", "exchangeTradeId": "trade-1", "tradedQuantity": 10,
               "tradedPrice": 799.5, "exchangeTime": "2026-09-20 10:00:00"}]

    DhanOrderReconciler._apply(db, connection, broker_orders, trades)
    db.commit()
    DhanOrderReconciler._apply(db, connection, broker_orders, trades)
    db.commit()

    assert db.scalar(select(func.count()).select_from(TradeExecution)) == 1
    assert db.scalar(select(TradingOrder)).status == OrderStatus.FILLED
    position = db.scalar(select(TradingPosition))
    assert position.quantity == 10 and position.average_price == 799.5
