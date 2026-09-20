"""Deterministic paper execution, risk checks, positions and order state transitions."""

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..market.contracts import NormalizedQuote
from ..models import (
    Instrument,
    OrderSide,
    OrderStatus,
    OrderType,
    ProductType,
    TradeExecution,
    TradingControl,
    TradingOrder,
    TradingPosition,
)

OPEN_ORDER_STATUSES = {OrderStatus.PENDING, OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED,
                       OrderStatus.CANCEL_PENDING, OrderStatus.UNKNOWN}


class TradingError(Exception):
    def __init__(self, code: str, status_code: int = 409):
        self.code = code
        self.status_code = status_code
        super().__init__(code)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def is_crossing(order: TradingOrder, quote: NormalizedQuote) -> bool:
    if order.order_type == OrderType.MARKET:
        return True
    reference = (quote.ask or quote.ltp) if order.side == OrderSide.BUY else (quote.bid or quote.ltp)
    return bool(order.limit_price is not None and
                (order.limit_price >= reference if order.side == OrderSide.BUY else order.limit_price <= reference))


def _fill_price(order: TradingOrder, quote: NormalizedQuote, slippage_bps: float) -> float:
    reference = (quote.ask or quote.ltp) if order.side == OrderSide.BUY else (quote.bid or quote.ltp)
    if order.order_type == OrderType.LIMIT:
        return min(order.limit_price or reference, reference) if order.side == OrderSide.BUY else max(order.limit_price or reference, reference)
    multiplier = 1 + slippage_bps / 10_000 if order.side == OrderSide.BUY else 1 - slippage_bps / 10_000
    return round(reference * multiplier, 8)


def _position(db: Session, order: TradingOrder) -> TradingPosition:
    position = db.scalar(select(TradingPosition).where(
        TradingPosition.user_id == order.user_id,
        TradingPosition.instrument_id == order.instrument_id,
        TradingPosition.mode == order.mode,
        TradingPosition.product_type == order.product_type,
    ).with_for_update())
    if position is None:
        position = TradingPosition(user_id=order.user_id, instrument_id=order.instrument_id,
                                   mode=order.mode, product_type=order.product_type,
                                   quantity=0, average_price=0, realized_pnl=0)
        db.add(position)
    return position


def apply_execution(db: Session, order: TradingOrder, quantity: int, price: float, *, source: str,
                    broker_execution_id: str | None = None, executed_at: datetime | None = None) -> TradeExecution:
    if broker_execution_id:
        existing = db.scalar(select(TradeExecution).where(
            TradeExecution.user_id == order.user_id,
            TradeExecution.broker_execution_id == broker_execution_id,
        ))
        if existing:
            return existing
    if quantity <= 0 or order.filled_quantity + quantity > order.quantity:
        raise TradingError("INVALID_EXECUTION_QUANTITY")
    execution = TradeExecution(
        order_id=order.id, user_id=order.user_id, instrument_id=order.instrument_id,
        broker_execution_id=broker_execution_id, mode=order.mode, side=order.side,
        quantity=quantity, price=price, source=source, executed_at=executed_at or _now(),
    )
    db.add(execution)
    previous_filled = order.filled_quantity
    order.filled_quantity += quantity
    order.average_fill_price = (
        ((order.average_fill_price or 0) * previous_filled + price * quantity) / order.filled_quantity
    )
    order.status = OrderStatus.FILLED if order.filled_quantity == order.quantity else OrderStatus.PARTIALLY_FILLED
    if order.status == OrderStatus.FILLED:
        order.completed_at = executed_at or _now()

    position = _position(db, order)
    old_quantity = position.quantity
    signed_fill = quantity if order.side == OrderSide.BUY else -quantity
    new_quantity = old_quantity + signed_fill
    if old_quantity == 0 or old_quantity * signed_fill > 0:
        total_cost = position.average_price * abs(old_quantity) + price * abs(signed_fill)
        position.average_price = total_cost / abs(new_quantity)
    else:
        closing = min(abs(old_quantity), abs(signed_fill))
        direction = 1 if old_quantity > 0 else -1
        position.realized_pnl += closing * (price - position.average_price) * direction
        if new_quantity == 0:
            position.average_price = 0
        elif old_quantity * new_quantity < 0:
            position.average_price = price
    position.quantity = new_quantity
    position.last_price = price
    return execution


class PaperTradingEngine:
    def __init__(self) -> None:
        self.settings = get_settings()

    def _control_check(self, db: Session, user_id: str) -> None:
        controls = db.scalars(select(TradingControl).where(
            TradingControl.scope_key.in_(["GLOBAL", f"USER:{user_id}"]))).all()
        if any(control.is_halted for control in controls):
            raise TradingError("TRADING_HALTED")

    def _quote_check(self, quote: NormalizedQuote | None) -> None:
        if quote is None:
            raise TradingError("LIVE_QUOTE_REQUIRED", 422)
        age = (_now() - _aware(quote.received_at)).total_seconds()
        if quote.stale or age > self.settings.market_stale_after_seconds:
            raise TradingError("STALE_MARKET_DATA")

    def _risk_check(self, db: Session, user_id: str, instrument: Instrument, side: OrderSide,
                    product_type: ProductType, quantity: int, price: float, mode: str = "PAPER") -> None:
        self._control_check(db, user_id)
        if quantity > self.settings.trading_max_order_quantity:
            raise TradingError("MAX_ORDER_QUANTITY_EXCEEDED", 422)
        if quantity * price > self.settings.trading_max_order_notional:
            raise TradingError("MAX_ORDER_NOTIONAL_EXCEEDED", 422)
        open_count = db.scalar(select(func.count()).select_from(TradingOrder).where(
            TradingOrder.user_id == user_id, TradingOrder.status.in_(OPEN_ORDER_STATUSES))) or 0
        if open_count >= self.settings.trading_max_open_orders:
            raise TradingError("MAX_OPEN_ORDERS_EXCEEDED", 422)
        position = db.scalar(select(TradingPosition).where(
            TradingPosition.user_id == user_id, TradingPosition.instrument_id == instrument.id,
            TradingPosition.mode == mode, TradingPosition.product_type == product_type))
        signed = quantity if side == OrderSide.BUY else -quantity
        if abs((position.quantity if position else 0) + signed) > self.settings.trading_max_absolute_position:
            raise TradingError("MAX_POSITION_EXCEEDED", 422)

    @staticmethod
    def _matches(existing: TradingOrder, values: dict[str, Any]) -> bool:
        fields = ("instrument_id", "side", "order_type", "product_type", "validity", "quantity", "limit_price")
        return all(getattr(existing, field) == values[field] for field in fields)

    def place(self, db: Session, user_id: str, instrument: Instrument, values: dict[str, Any],
              quote: NormalizedQuote | None) -> TradingOrder:
        if self.settings.trading_mode != "paper":
            raise TradingError("PAPER_TRADING_DISABLED", 403)
        existing = db.scalar(select(TradingOrder).where(
            TradingOrder.user_id == user_id, TradingOrder.client_order_id == values["client_order_id"]))
        if existing:
            if not self._matches(existing, {**values, "instrument_id": instrument.id}):
                raise TradingError("IDEMPOTENCY_CONFLICT")
            return existing
        if values["order_type"] == OrderType.LIMIT and values.get("limit_price") is None:
            raise TradingError("LIMIT_PRICE_REQUIRED", 422)
        if values["order_type"] == OrderType.MARKET:
            self._quote_check(quote)
        risk_price = quote.ltp if quote else values.get("limit_price")
        if risk_price is None:
            raise TradingError("RISK_PRICE_REQUIRED", 422)
        self._risk_check(db, user_id, instrument, values["side"], values["product_type"],
                         values["quantity"], risk_price)
        order = TradingOrder(
            user_id=user_id, instrument_id=instrument.id, client_order_id=values["client_order_id"],
            mode="PAPER", side=values["side"], order_type=values["order_type"],
            product_type=values["product_type"], validity=values["validity"], quantity=values["quantity"],
            limit_price=values.get("limit_price"), status=OrderStatus.OPEN, submitted_at=_now(),
        )
        db.add(order)
        db.flush()
        if quote and is_crossing(order, quote):
            apply_execution(db, order, order.quantity, _fill_price(order, quote, self.settings.paper_slippage_bps),
                            source="PAPER", broker_execution_id=f"paper-{uuid.uuid4()}")
        elif order.validity.value == "IOC":
            order.status = OrderStatus.CANCELED
            order.completed_at = _now()
        db.commit()
        return order

    def cancel(self, db: Session, order: TradingOrder) -> TradingOrder:
        if order.mode != "PAPER" or order.status not in OPEN_ORDER_STATUSES:
            raise TradingError("ORDER_NOT_CANCELABLE")
        order.status = OrderStatus.CANCELED
        order.completed_at = _now()
        order.version += 1
        db.commit()
        return order

    def modify(self, db: Session, order: TradingOrder, quantity: int, limit_price: float,
               quote: NormalizedQuote | None) -> TradingOrder:
        if order.mode != "PAPER" or order.status not in OPEN_ORDER_STATUSES or order.order_type != OrderType.LIMIT:
            raise TradingError("ORDER_NOT_MODIFIABLE")
        if quantity < max(1, order.filled_quantity):
            raise TradingError("QUANTITY_BELOW_FILLED", 422)
        order.quantity = quantity
        order.limit_price = limit_price
        order.version += 1
        if quote and is_crossing(order, quote):
            apply_execution(db, order, quantity - order.filled_quantity,
                            _fill_price(order, quote, self.settings.paper_slippage_bps),
                            source="PAPER", broker_execution_id=f"paper-{uuid.uuid4()}")
        db.commit()
        return order

    def process_quote(self, db: Session, user_id: str, quote: NormalizedQuote) -> int:
        orders = db.scalars(select(TradingOrder).where(
            TradingOrder.user_id == user_id, TradingOrder.instrument_id == quote.instrument_id,
            TradingOrder.mode == "PAPER", TradingOrder.status.in_([OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED]),
        ).with_for_update()).all()
        filled = 0
        for order in orders:
            if is_crossing(order, quote):
                apply_execution(db, order, order.quantity - order.filled_quantity,
                                _fill_price(order, quote, self.settings.paper_slippage_bps),
                                source="PAPER", broker_execution_id=f"paper-{uuid.uuid4()}")
                filled += 1
        if filled:
            db.commit()
        return filled
