import os
os.environ.update({"ENVIRONMENT": "test", "DATABASE_URL": "sqlite://", "JWT_SECRET": "test-jwt-secret-that-is-long-and-random", "API_KEY_PEPPER": "test-api-pepper-that-is-long-and-random", "EXPOSE_DEV_TOKENS": "true"})

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app.database import Base, get_db
from app.main import app
from app.models import User
from app.rate_limit import limiter

engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
TestingSession = sessionmaker(bind=engine, expire_on_commit=False)

def override_db():
    db = TestingSession()
    try: yield db
    finally: db.close()

app.dependency_overrides[get_db] = override_db

@pytest.fixture(autouse=True)
def clean_db():
    Base.metadata.drop_all(engine); Base.metadata.create_all(engine); limiter.events.clear()
    yield

@pytest.fixture
def client(): return TestClient(app)

@pytest.fixture
def db():
    session = TestingSession()
    try: yield session
    finally: session.close()

def register_verified(client, email="user@example.com", password="StrongPass!123"):
    response = client.post("/api/v1/auth/register", json={"email": email, "password": password, "password_confirmation": password})
    assert response.status_code == 201
    token = response.json()["verification_token"]
    assert client.post("/api/v1/auth/verify-email", json={"token": token}).status_code == 200
    return email, password

def login(client, email="user@example.com", password="StrongPass!123", totp_code=None):
    body = {"email": email, "password": password}
    if totp_code: body["totp_code"] = totp_code
    response = client.post("/api/v1/auth/login", json=body)
    assert response.status_code == 200, response.text
    return response.json()

def auth(token): return {"Authorization": f"Bearer {token}"}
