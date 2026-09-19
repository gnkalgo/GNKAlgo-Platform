from sqlalchemy import select
from app.models import ApiKey
from .conftest import auth, login, register_verified

def test_api_key_lifecycle_scope_and_no_plaintext(client, db):
    register_verified(client); tokens = login(client); headers = auth(tokens["access_token"])
    created = client.post("/api/v1/api-keys", headers=headers, json={"name": "analytics", "scopes": ["profile:read", "broker:read"]})
    assert created.status_code == 201
    body = created.json(); full_key = body["key"]
    stored = db.scalar(select(ApiKey).where(ApiKey.id == body["id"]))
    assert full_key not in stored.secret_hash and full_key not in repr(stored.__dict__)
    listed = client.get("/api/v1/api-keys", headers=headers).json()
    assert "key" not in listed[0] and "secret_hash" not in listed[0]
    valid = client.get("/api/v1/api-keys/validate/profile", headers={"X-GnK-API-Key": full_key})
    assert valid.status_code == 200
    assert client.get("/api/v1/brokers", headers={"X-GnK-API-Key": full_key}).status_code == 200
    rotated = client.post(f"/api/v1/api-keys/{body['id']}/rotate", headers=headers)
    assert rotated.status_code == 200 and rotated.json()["key"] != full_key
    assert client.get("/api/v1/api-keys/validate/profile", headers={"X-GnK-API-Key": full_key}).status_code == 401
    assert client.post(f"/api/v1/api-keys/{rotated.json()['id']}/revoke", headers=headers).status_code == 200

def test_retail_cannot_assign_trading_or_admin_write_scopes(client):
    register_verified(client); tokens = login(client)
    for scope in ("orders:write", "strategies:write", "admin:write"):
        response = client.post("/api/v1/api-keys", headers=auth(tokens["access_token"]), json={"name": scope, "scopes": [scope]})
        assert response.status_code == 403
