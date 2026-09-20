"""Mode-gated order orchestration for paper and Dhan live execution."""

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..market.contracts import NormalizedQuote
from ..models import (
    BrokerConnection,
    BrokerName,
    BrokerStatus,
    Instrument,
    OrderStatus,
    OrderType,
    TradingOrder,
)
from ..security import decrypt_json
from .dhan import DhanOrderError, dhan_order_adapter, map_dhan_status
from .engine import OPEN_ORDER_STATUSES, PaperTradingEngine, TradingError


class OrderManager:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.paper = PaperTradingEngine()
        self.dhan = dhan_order_adapter

    def _live_gate(self) -> None:
        if self.settings.trading_mode != "live":
            raise TradingError("LIVE_TRADING_DISABLED", 403)
        if self.settings.trading_live_confirmation != "ENABLE_DHAN_LIVE_ORDERS":
            raise TradingError("LIVE_TRADING_NOT_CONFIRMED", 403)
        if not self.settings.dhan_static_ip_confirmed:
            raise TradingError("DHAN_STATIC_IP_NOT_CONFIRMED", 403)

    @staticmethod
    def _connection(db: Session, user_id: str) -> tuple[BrokerConnection, dict, str]:
        connection = db.scalar(select(BrokerConnection).where(
            BrokerConnection.user_id == user_id, BrokerConnection.broker == BrokerName.DHAN,
            BrokerConnection.status == BrokerStatus.CONNECTED))
        if connection is None:
            raise TradingError("DHAN_CONNECTION_REQUIRED", 422)
        if connection.token_expires_at:
            expires_at = connection.token_expires_at
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if expires_at <= datetime.now(timezone.utc):
                connection.status = BrokerStatus.REAUTH_REQUIRED
                connection.error_code = "TOKEN_EXPIRED"
                db.commit()
                raise TradingError("DHAN_REAUTH_REQUIRED", 422)
        credentials = decrypt_json(connection.encrypted_credentials)
        client_id = credentials.get("client_id") or connection.broker_client_id
        if not credentials.get("access_token") or not client_id:
            raise TradingError("DHAN_CREDENTIALS_INCOMPLETE", 422)
        return connection, credentials, str(client_id)

    async def place(self, db: Session, user_id: str, instrument: Instrument, values: dict[str, Any],
                    quote: NormalizedQuote | None) -> TradingOrder:
        if self.settings.trading_mode == "disabled":
            raise TradingError("TRADING_DISABLED", 403)
        if self.settings.trading_mode == "paper":
            return self.paper.place(db, user_id, instrument, values, quote)
        self._live_gate()
        existing = db.scalar(select(TradingOrder).where(
            TradingOrder.user_id == user_id, TradingOrder.client_order_id == values["client_order_id"]))
        expected = {**values, "instrument_id": instrument.id}
        if existing:
            if not self.paper._matches(existing, expected):
                raise TradingError("IDEMPOTENCY_CONFLICT")
            return existing
        if values["order_type"] == OrderType.LIMIT and values.get("limit_price") is None:
            raise TradingError("LIMIT_PRICE_REQUIRED", 422)
        self.paper._quote_check(quote)
        self.paper._risk_check(db, user_id, instrument, values["side"], values["product_type"],
                               values["quantity"], quote.ltp, mode="LIVE")
        connection, credentials, client_id = self._connection(db, user_id)
        order = TradingOrder(
            user_id=user_id, instrument_id=instrument.id, broker_connection_id=connection.id,
            client_order_id=values["client_order_id"], mode="LIVE", side=values["side"],
            order_type=values["order_type"], product_type=values["product_type"], validity=values["validity"],
            quantity=values["quantity"], limit_price=values.get("limit_price"),
            trigger_price=values.get("trigger_price"), status=OrderStatus.PENDING,
            submitted_at=datetime.now(timezone.utc),
        )
        db.add(order)
        db.commit()
        try:
            response = await self.dhan.place(credentials, client_id, instrument, order)
            order.broker_order_id = str(response.get("orderId")) if response.get("orderId") else None
            order.status = map_dhan_status(response.get("orderStatus"))
            if order.status in {OrderStatus.REJECTED, OrderStatus.CANCELED, OrderStatus.EXPIRED}:
                order.completed_at = datetime.now(timezone.utc)
            db.commit()
            return order
        except DhanOrderError as exc:
            order.status = OrderStatus.UNKNOWN if exc.ambiguous else OrderStatus.REJECTED
            order.rejection_reason = exc.code
            if not exc.ambiguous:
                order.completed_at = datetime.now(timezone.utc)
            db.commit()
            raise TradingError(exc.code, 502) from exc

    async def cancel(self, db: Session, order: TradingOrder) -> TradingOrder:
        if order.status not in OPEN_ORDER_STATUSES:
            raise TradingError("ORDER_NOT_CANCELABLE")
        if order.mode == "PAPER":
            return self.paper.cancel(db, order)
        self._live_gate()
        _, credentials, client_id = self._connection(db, order.user_id)
        prior = order.status
        order.status = OrderStatus.CANCEL_PENDING
        order.version += 1
        db.commit()
        try:
            response = await self.dhan.cancel(credentials, client_id, order)
            order.status = map_dhan_status(response.get("orderStatus") or "CANCELLED")
            if order.status == OrderStatus.CANCELED:
                order.completed_at = datetime.now(timezone.utc)
            db.commit()
            return order
        except DhanOrderError as exc:
            order.status = OrderStatus.UNKNOWN if exc.ambiguous else prior
            order.rejection_reason = exc.code
            db.commit()
            raise TradingError(exc.code, 502) from exc

    async def modify(self, db: Session, order: TradingOrder, quantity: int, limit_price: float,
                     quote: NormalizedQuote | None) -> TradingOrder:
        if order.mode == "PAPER":
            return self.paper.modify(db, order, quantity, limit_price, quote)
        self._live_gate()
        if order.status not in OPEN_ORDER_STATUSES or order.order_type != OrderType.LIMIT:
            raise TradingError("ORDER_NOT_MODIFIABLE")
        if quantity < max(1, order.filled_quantity):
            raise TradingError("QUANTITY_BELOW_FILLED", 422)
        _, credentials, client_id = self._connection(db, order.user_id)
        previous = (order.quantity, order.limit_price)
        order.quantity, order.limit_price = quantity, limit_price
        try:
            response = await self.dhan.modify(credentials, client_id, order)
            order.status = map_dhan_status(response.get("orderStatus"))
            order.version += 1
            db.commit()
            return order
        except DhanOrderError as exc:
            order.quantity, order.limit_price = previous
            order.status = OrderStatus.UNKNOWN if exc.ambiguous else order.status
            order.rejection_reason = exc.code
            db.commit()
            raise TradingError(exc.code, 502) from exc


order_manager = OrderManager()
