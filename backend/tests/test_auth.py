import pyotp
from sqlalchemy import select
from app.models import AuditLog, Role, User, UserSession
from .conftest import auth, login, register_verified

def test_registration_verification_login_refresh_and_logout(client, db):
    email, password = register_verified(client)
    bad = client.post("/api/v1/auth/login", json={"email": email, "password": "wrong-password"})
    assert bad.status_code == 401 and bad.json()["detail"] == "Invalid credentials"
    tokens = login(client, email, password)
    me = client.get("/api/v1/users/me", headers=auth(tokens["access_token"]))
    assert me.status_code == 200 and me.json()["role"] == "RETAIL"
    rotated = client.post("/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert rotated.status_code == 200 and rotated.json()["refresh_token"] != tokens["refresh_token"]
    reused = client.post("/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert reused.status_code == 401
    assert client.get("/api/v1/users/me", headers=auth(tokens["access_token"])).status_code == 401
    assert db.scalar(select(AuditLog).where(AuditLog.event == "REFRESH_TOKEN_REUSE_DETECTED"))

def test_mfa_setup_login_and_recovery(client):
    email, password = register_verified(client)
    tokens = login(client)
    setup = client.post("/api/v1/auth/mfa/setup", headers=auth(tokens["access_token"]))
    secret = setup.json()["secret"]
    enabled = client.post("/api/v1/auth/mfa/verify", json={"code": pyotp.TOTP(secret).now()}, headers=auth(tokens["access_token"]))
    assert enabled.status_code == 200 and len(enabled.json()["recovery_codes"]) == 8
    assert client.post("/api/v1/auth/login", json={"email": email, "password": password}).json()["detail"] == "MFA_REQUIRED"
    assert client.post("/api/v1/auth/login", json={"email": email, "password": password, "totp_code": "000000"}).status_code == 401
    assert client.post("/api/v1/auth/login", json={"email": email, "password": password, "totp_code": pyotp.TOTP(secret).now()}).status_code == 200

def test_rbac_admin_and_session_ownership(client, db):
    register_verified(client, "admin@example.com")
    admin = db.scalar(select(User).where(User.email == "admin@example.com")); admin.role = Role.ADMIN; db.commit()
    admin_tokens = login(client, "admin@example.com")
    register_verified(client, "retail@example.com")
    retail_tokens = login(client, "retail@example.com")
    assert client.get("/api/v1/admin/users", headers=auth(retail_tokens["access_token"])).status_code == 403
    assert client.get("/api/v1/admin/users", headers=auth(admin_tokens["access_token"])).status_code == 200
    other_session = db.scalar(select(UserSession).where(UserSession.user_id != admin.id))
    assert client.post(f"/api/v1/sessions/{other_session.id}/revoke", headers=auth(admin_tokens["access_token"])).status_code == 404

def test_password_reset_revokes_sessions(client):
    email, password = register_verified(client)
    tokens = login(client)
    forgot = client.post("/api/v1/auth/forgot-password", json={"email": email}).json()
    new_password = "EvenStronger!456"
    assert client.post("/api/v1/auth/reset-password", json={"token": forgot["verification_token"], "password": new_password, "password_confirmation": new_password}).status_code == 200
    assert client.get("/api/v1/users/me", headers=auth(tokens["access_token"])).status_code == 401
    login(client, email, new_password)
