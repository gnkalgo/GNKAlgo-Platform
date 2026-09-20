"""Dhan v2 live order adapter. Placement is never retried automatically."""

from typing import Any

import httpx

from ..market.dhan import dhan_subscription
from ..models import OrderStatus, TradingOrder

DHAN_API_URL = "https://api.dhan.co/v2"


class DhanOrderError(Exception):
    def __init__(self, code: str, ambiguous: bool = False):
        self.code = code
        self.ambiguous = ambiguous
        super().__init__(code)


DHAN_STATUS_MAP = {
    "TRANSIT": OrderStatus.PENDING,
    "PENDING": OrderStatus.OPEN,
    "PART_TRADED": OrderStatus.PARTIALLY_FILLED,
    "TRADED": OrderStatus.FILLED,
    "CANCELLED": OrderStatus.CANCELED,
    "REJECTED": OrderStatus.REJECTED,
    "EXPIRED": OrderStatus.EXPIRED,
}


def map_dhan_status(value: str | None) -> OrderStatus:
    return DHAN_STATUS_MAP.get((value or "").upper(), OrderStatus.UNKNOWN)


class DhanOrderAdapter:
    async def _request(self, method: str, path: str, credentials: dict, client_id: str, **kwargs) -> Any:
        headers = {"access-token": credentials["access_token"], "client-id": client_id,
                   "Content-Type": "application/json", "Accept": "application/json"}
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
                response = await client.request(method, f"{DHAN_API_URL}{path}", headers=headers, **kwargs)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise DhanOrderError("DHAN_ORDER_RESULT_UNKNOWN", ambiguous=True) from exc
        if response.status_code >= 400:
            try:
                payload = response.json()
                code = str(payload.get("errorCode") or payload.get("errorType") or "DHAN_ORDER_REJECTED")
            except ValueError:
                code = "DHAN_ORDER_REJECTED"
            raise DhanOrderError(code)
        if response.status_code == 204 or not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise DhanOrderError("DHAN_INVALID_RESPONSE", ambiguous=method == "POST") from exc

    async def place(self, credentials: dict, client_id: str, instrument, order: TradingOrder) -> dict:
        subscription = dhan_subscription(instrument)
        body = {
            "dhanClientId": client_id,
            "correlationId": order.client_order_id,
            "transactionType": order.side.value,
            "exchangeSegment": subscription.exchange_segment,
            "productType": order.product_type.value,
            "orderType": order.order_type.value,
            "validity": order.validity.value,
            "securityId": subscription.security_id,
            "quantity": order.quantity,
            "disclosedQuantity": 0,
            "price": order.limit_price or 0,
            "triggerPrice": order.trigger_price or 0,
            "afterMarketOrder": False,
        }
        return await self._request("POST", "/orders", credentials, client_id, json=body)

    async def modify(self, credentials: dict, client_id: str, order: TradingOrder) -> dict:
        if not order.broker_order_id:
            raise DhanOrderError("DHAN_ORDER_ID_MISSING")
        body = {
            "dhanClientId": client_id,
            "orderId": order.broker_order_id,
            "orderType": order.order_type.value,
            "quantity": order.quantity,
            "price": order.limit_price or 0,
            "disclosedQuantity": 0,
            "triggerPrice": order.trigger_price or 0,
            "validity": order.validity.value,
        }
        return await self._request("PUT", f"/orders/{order.broker_order_id}", credentials, client_id, json=body)

    async def cancel(self, credentials: dict, client_id: str, order: TradingOrder) -> dict:
        if not order.broker_order_id:
            raise DhanOrderError("DHAN_ORDER_ID_MISSING")
        return await self._request("DELETE", f"/orders/{order.broker_order_id}", credentials, client_id)

    async def order_by_correlation(self, credentials: dict, client_id: str, correlation_id: str) -> dict:
        return await self._request("GET", f"/orders/external/{correlation_id}", credentials, client_id)

    async def order_book(self, credentials: dict, client_id: str) -> list[dict]:
        result = await self._request("GET", "/orders", credentials, client_id)
        return result if isinstance(result, list) else []

    async def trade_book(self, credentials: dict, client_id: str) -> list[dict]:
        result = await self._request("GET", "/trades", credentials, client_id)
        return result if isinstance(result, list) else []


dhan_order_adapter = DhanOrderAdapter()
