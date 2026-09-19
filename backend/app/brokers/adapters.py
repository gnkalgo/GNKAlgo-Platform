import hashlib
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from urllib.parse import urlencode
import httpx
from ..config import get_settings
from ..models import BrokerName

settings = get_settings()

class BrokerError(Exception):
    def __init__(self, code: str): self.code = code

class BrokerAdapter(ABC):
    name: BrokerName
    @abstractmethod
    async def authorization_url(self, state: str) -> tuple[str, dict]: ...
    @abstractmethod
    async def exchange(self, code: str, context: dict) -> dict: ...
    @abstractmethod
    async def test(self, credentials: dict) -> bool: ...

    async def _request(self, method: str, url: str, **kwargs) -> dict:
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
                response = await client.request(method, url, **kwargs)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise BrokerError("BROKER_AUTH_FAILED") from exc

class DhanAdapter(BrokerAdapter):
    name = BrokerName.DHAN
    async def authorization_url(self, state: str) -> tuple[str, dict]:
        if not settings.dhan_app_id or not settings.dhan_app_secret: raise BrokerError("BROKER_NOT_CONFIGURED")
        data = await self._request("POST", "https://auth.dhan.co/app/generate-consent", params={"client_id": settings.dhan_app_id}, headers={"app_id": settings.dhan_app_id, "app_secret": settings.dhan_app_secret})
        consent = data.get("consentAppId")
        if not consent: raise BrokerError("BROKER_AUTH_FAILED")
        return f"https://auth.dhan.co/login/consentApp-login?{urlencode({'consentAppId': consent})}", {"consent_id": consent}
    async def exchange(self, code: str, context: dict) -> dict:
        data = await self._request("GET", "https://auth.dhan.co/app/consumeApp-consent", params={"tokenId": code}, headers={"app_id": settings.dhan_app_id or "", "app_secret": settings.dhan_app_secret or ""})
        if not data.get("accessToken"): raise BrokerError("BROKER_AUTH_FAILED")
        return {"access_token": data["accessToken"], "client_id": data.get("dhanClientId"), "expires_at": data.get("expiryTime")}
    async def test(self, credentials: dict) -> bool:
        await self._request("GET", "https://api.dhan.co/v2/profile", headers={"access-token": credentials["access_token"]})
        return True

class FyersAdapter(BrokerAdapter):
    name = BrokerName.FYERS
    async def authorization_url(self, state: str) -> tuple[str, dict]:
        if not settings.fyers_client_id or not settings.fyers_client_secret: raise BrokerError("BROKER_NOT_CONFIGURED")
        query = urlencode({"client_id": settings.fyers_client_id, "redirect_uri": settings.fyers_redirect_uri, "response_type": "code", "state": state})
        return f"https://api-t1.fyers.in/api/v3/generate-authcode?{query}", {}
    async def exchange(self, code: str, context: dict) -> dict:
        app_hash = hashlib.sha256(f"{settings.fyers_client_id}:{settings.fyers_client_secret}".encode()).hexdigest()
        data = await self._request("POST", "https://api-t1.fyers.in/api/v3/validate-authcode", json={"grant_type": "authorization_code", "appIdHash": app_hash, "code": code})
        if not data.get("access_token"): raise BrokerError("BROKER_AUTH_FAILED")
        return {"access_token": data["access_token"], "refresh_token": data.get("refresh_token"), "client_id": settings.fyers_client_id}
    async def test(self, credentials: dict) -> bool:
        await self._request("GET", "https://api-t1.fyers.in/api/v3/profile", headers={"Authorization": f"{settings.fyers_client_id}:{credentials['access_token']}"})
        return True

class UpstoxAdapter(BrokerAdapter):
    name = BrokerName.UPSTOX
    async def authorization_url(self, state: str) -> tuple[str, dict]:
        if not settings.upstox_client_id or not settings.upstox_client_secret: raise BrokerError("BROKER_NOT_CONFIGURED")
        query = urlencode({"client_id": settings.upstox_client_id, "redirect_uri": settings.upstox_redirect_uri, "response_type": "code", "state": state})
        return f"https://api.upstox.com/v2/login/authorization/dialog?{query}", {}
    async def exchange(self, code: str, context: dict) -> dict:
        data = await self._request("POST", "https://api.upstox.com/v2/login/authorization/token", data={"code": code, "client_id": settings.upstox_client_id, "client_secret": settings.upstox_client_secret, "redirect_uri": settings.upstox_redirect_uri, "grant_type": "authorization_code"}, headers={"accept": "application/json"})
        if not data.get("access_token"): raise BrokerError("BROKER_AUTH_FAILED")
        return {"access_token": data["access_token"], "client_id": data.get("user_id") or settings.upstox_client_id}
    async def test(self, credentials: dict) -> bool:
        await self._request("GET", "https://api.upstox.com/v2/user/profile", headers={"Authorization": f"Bearer {credentials['access_token']}", "Accept": "application/json"})
        return True

ADAPTERS = {BrokerName.DHAN: DhanAdapter(), BrokerName.FYERS: FyersAdapter(), BrokerName.UPSTOX: UpstoxAdapter()}
