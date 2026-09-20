"""Dhan order/trade-book reconciliation worker for Phase 7.2."""

import asyncio
import logging
import signal
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select

from ..config import get_settings
from ..database import SessionLocal
from ..models import BrokerConnection, BrokerName, BrokerStatus, OrderStatus, TradingOrder
from ..security import decrypt_json
from .dhan import DhanOrderError, dhan_order_adapter, map_dhan_status
from .engine import OPEN_ORDER_STATUSES, TradingError, apply_execution

logger = logging.getLogger("gnkalgo.trading.reconcile")
TERMINAL = {OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED, OrderStatus.EXPIRED}


def _trade_time(value) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    text = str(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo("Asia/Kolkata"))
    return parsed.astimezone(timezone.utc)


class DhanOrderReconciler:
    def __init__(self) -> None:
        self.settings = get_settings()
        self._stopping = asyncio.Event()

    def stop(self) -> None:
        self._stopping.set()

    async def run(self) -> None:
        logger.info("order reconciler started", extra={"mode": self.settings.trading_mode})
        while not self._stopping.is_set():
            if self.settings.trading_mode == "live":
                await self.reconcile_once()
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self.settings.dhan_order_reconcile_seconds)
            except TimeoutError:
                pass

    async def reconcile_once(self) -> None:
        with SessionLocal() as db:
            connections = db.scalars(select(BrokerConnection).where(
                BrokerConnection.broker == BrokerName.DHAN,
                BrokerConnection.status == BrokerStatus.CONNECTED,
            )).all()
            for connection in connections:
                try:
                    credentials = decrypt_json(connection.encrypted_credentials)
                    client_id = str(credentials.get("client_id") or connection.broker_client_id or "")
                    if not client_id or not credentials.get("access_token"):
                        logger.error("Dhan credentials incomplete", extra={"connection_id": connection.id})
                        continue
                    broker_orders, trades = await asyncio.gather(
                        dhan_order_adapter.order_book(credentials, client_id),
                        dhan_order_adapter.trade_book(credentials, client_id),
                    )
                    self._apply(db, connection, broker_orders, trades)
                    db.commit()
                except DhanOrderError as exc:
                    db.rollback()
                    logger.warning("Dhan reconciliation request failed", extra={
                        "connection_id": connection.id, "error_code": exc.code,
                    })
                except Exception:
                    db.rollback()
                    logger.exception("Dhan reconciliation failed", extra={"connection_id": connection.id})

    @staticmethod
    def _apply(db, connection: BrokerConnection, broker_orders: list[dict], trades: list[dict]) -> None:
        local_orders = db.scalars(select(TradingOrder).where(
            TradingOrder.user_id == connection.user_id,
            TradingOrder.mode == "LIVE",
            TradingOrder.status.in_(OPEN_ORDER_STATUSES),
        ).with_for_update()).all()
        by_broker = {order.broker_order_id: order for order in local_orders if order.broker_order_id}
        by_correlation = {order.client_order_id: order for order in local_orders}

        for item in broker_orders:
            broker_id = str(item.get("orderId") or "")
            correlation = str(item.get("correlationId") or "")
            order = by_broker.get(broker_id) or by_correlation.get(correlation)
            if order is None:
                continue
            if broker_id and not order.broker_order_id:
                order.broker_order_id = broker_id
                by_broker[broker_id] = order

        for item in trades:
            broker_id = str(item.get("orderId") or "")
            order = by_broker.get(broker_id)
            execution_id = str(item.get("exchangeTradeId") or "")
            if order is None or not execution_id:
                continue
            try:
                apply_execution(
                    db, order, int(item.get("tradedQuantity") or 0), float(item.get("tradedPrice") or 0),
                    source="DHAN_RECONCILIATION", broker_execution_id=execution_id,
                    executed_at=_trade_time(item.get("exchangeTime")),
                )
            except TradingError as exc:
                order.status = OrderStatus.UNKNOWN
                order.rejection_reason = f"RECONCILIATION_{exc.code}"
                logger.error("Invalid broker execution", extra={"order_id": order.id, "error_code": exc.code})

        for item in broker_orders:
            broker_id = str(item.get("orderId") or "")
            correlation = str(item.get("correlationId") or "")
            order = by_broker.get(broker_id) or by_correlation.get(correlation)
            if order is None:
                continue
            broker_status = map_dhan_status(item.get("orderStatus"))
            if broker_status == OrderStatus.FILLED and order.filled_quantity != order.quantity:
                order.status = OrderStatus.UNKNOWN
                order.rejection_reason = "TRADE_BOOK_INCOMPLETE"
            else:
                order.status = broker_status
                reason = item.get("omsErrorDescription") or item.get("orderStatusDescription")
                if broker_status == OrderStatus.REJECTED and reason:
                    order.rejection_reason = str(reason)[:120]
                if broker_status in TERMINAL:
                    order.completed_at = order.completed_at or datetime.now(timezone.utc)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    worker = DhanOrderReconciler()
    loop = asyncio.get_running_loop()
    for event in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(event, worker.stop)
        except NotImplementedError:
            pass
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
