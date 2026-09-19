from sqlalchemy import select
from app.brokers.adapters import ADAPTERS
from app.models import BrokerConnection, BrokerName
from app.security import decrypt_json
from .conftest import auth, login, register_verified

class FakeAdapter:
    async def authorization_url(self, state): return f"https://broker.example/authorize?state={state}", {"nonce": "safe"}
    async def exchange(self, code, context): return {"access_token": "broker-secret-token", "client_id": "client-1"}
    async def test(self, credentials): return credentials.get("access_token") == "broker-secret-token"

def test_broker_callback_encryption_and_ownership(client, db, monkeypatch):
    monkeypatch.setitem(ADAPTERS, BrokerName.UPSTOX, FakeAdapter())
    register_verified(client, "one@example.com"); one = login(client, "one@example.com")
    start = client.post("/api/v1/brokers/UPSTOX/connect", headers=auth(one["access_token"]))
    assert start.status_code == 200
    state = start.json()["authorization_url"].split("state=")[1]
    callback = client.get(f"/api/v1/brokers/UPSTOX/callback?code=code-1&state={state}", follow_redirects=False)
    assert callback.status_code == 302
    connection = db.scalar(select(BrokerConnection))
    assert "broker-secret-token" not in connection.encrypted_credentials
    assert decrypt_json(connection.encrypted_credentials)["access_token"] == "broker-secret-token"
    register_verified(client, "two@example.com"); two = login(client, "two@example.com")
    assert client.get(f"/api/v1/brokers/{connection.id}", headers=auth(two["access_token"])).status_code == 404
    assert client.delete(f"/api/v1/brokers/{connection.id}", headers=auth(two["access_token"])).status_code == 404
    assert client.delete(f"/api/v1/brokers/{connection.id}", headers=auth(one["access_token"])).status_code == 200
    db.refresh(connection); assert decrypt_json(connection.encrypted_credentials) == {}

def test_callback_state_is_single_use(client, monkeypatch):
    monkeypatch.setitem(ADAPTERS, BrokerName.FYERS, FakeAdapter())
    register_verified(client); tokens = login(client)
    start = client.post("/api/v1/brokers/FYERS/connect", headers=auth(tokens["access_token"]))
    state = start.json()["authorization_url"].split("state=")[1]
    url = f"/api/v1/brokers/FYERS/callback?auth_code=x&state={state}"
    assert client.get(url, follow_redirects=False).status_code == 302
    assert client.get(url, follow_redirects=False).status_code == 400

def test_broker_endpoints_require_authentication(client):
    assert client.get("/api/v1/brokers").status_code == 401
