import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.market.bus import market_bus
from app.market.contracts import NormalizedQuote
from app.models import (
    Instrument, MarketCandle, Strategy, StrategyRun, StrategyRunStatus,
    StrategySignal, StrategySignalStatus, StrategyStatus, StrategyVersion, TradingControl, TradingOrder, User,
)
from app.strategies.evaluator import evaluate_sma_cross
from app.strategies.service import StrategyError, strategy_executor
from app.strategies.worker import StrategyWorker
from tests.conftest import auth, login, register_verified


def setup_strategy(client, db, monkeypatch):
    monkeypatch.setattr(strategy_executor.settings, "strategy_engine_enabled", True)
    monkeypatch.setattr(strategy_executor.settings, "trading_mode", "paper")
    register_verified(client, email="strategy@example.com")
    token = login(client, email="strategy@example.com")["access_token"]
    user_id = client.get("/api/v1/users/me", headers=auth(token)).json()["id"]
    instrument = Instrument(exchange="NSE", segment="EQ", symbol="1333", trading_symbol="SBIN-EQ",
                            instrument_type="EQUITY", broker_tokens={"DHAN": "1333"})
    db.add(instrument)
    db.commit()
    body = {"name": "SMA cross", "instrument_id": instrument.id, "execution_mode": "PAPER",
            "timeframe_seconds": 60, "fast_period": 2, "slow_period": 3, "quantity": 2}
    response = client.post("/api/v1/strategies", headers=auth(token), json=body)
    assert response.status_code == 201, response.text
    return token, user_id, instrument, response.json()


def add_candles(db, user_id, instrument, prices):
    current = datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(minutes=1)
    result = []
    for index, close in enumerate(prices):
        candle = MarketCandle(user_id=user_id, instrument_id=instrument.id, interval_seconds=60,
                              start_at=current - timedelta(minutes=len(prices) - index - 1),
                              open=close, high=close, low=close, close=close, volume=1,
                              source="TEST", is_complete=True)
        db.add(candle)
        result.append(candle)
    db.commit()
    return result


def publish_quote(user_id, instrument, price=12):
    quote = NormalizedQuote(instrument_id=instrument.id, exchange=instrument.exchange,
                            segment=instrument.segment, trading_symbol=instrument.trading_symbol,
                            source="TEST", sequence=1, exchange_timestamp=datetime.now(timezone.utc),
                            ltp=price, bid=price - 0.05, ask=price + 0.05)
    asyncio.run(market_bus.publish(user_id, quote))


def test_sma_cross_is_close_only_and_deterministic():
    assert evaluate_sma_cross([10, 9, 8], 2, 3) is None
    assert evaluate_sma_cross([10, 9, 8, 12], 2, 3).action.value == "BUY"
    assert evaluate_sma_cross([8, 9, 10, 6], 2, 3).action.value == "SELL"
    assert evaluate_sma_cross([8, 9, 10, 11], 2, 3).action is None


def test_strategy_crud_versioning_activation_and_preview(client, db, monkeypatch):
    token, user_id, instrument, created = setup_strategy(client, db, monkeypatch)
    strategy_id = created["id"]
    assert created["status"] == "DRAFT"
    assert client.patch(f"/api/v1/strategies/{strategy_id}", headers=auth(token),
                        json={"quantity": 3}).json()["version"] == 2
    versions = client.get(f"/api/v1/strategies/{strategy_id}/versions", headers=auth(token)).json()
    assert [item["version"] for item in versions] == [2, 1]
    add_candles(db, user_id, instrument, [10, 9, 8, 12])
    preview = client.get(f"/api/v1/strategies/{strategy_id}/preview", headers=auth(token)).json()
    assert len(preview) == 1 and preview[0]["action"] == "BUY"
    assert db.scalar(select(func.count()).select_from(TradingOrder)) == 0
    active = client.post(f"/api/v1/strategies/{strategy_id}/activate", headers=auth(token), json={})
    assert active.status_code == 200 and active.json()["status"] == "ACTIVE"
    assert client.patch(f"/api/v1/strategies/{strategy_id}", headers=auth(token), json={"quantity": 2}).status_code == 409
    assert client.post(f"/api/v1/strategies/{strategy_id}/pause", headers=auth(token)).json()["status"] == "PAUSED"


def test_signal_routes_to_one_paper_order_and_does_not_replay(client, db, monkeypatch):
    token, user_id, instrument, created = setup_strategy(client, db, monkeypatch)
    strategy_id = created["id"]
    client.post(f"/api/v1/strategies/{strategy_id}/activate", headers=auth(token), json={})
    candles = add_candles(db, user_id, instrument, [10, 9, 8, 12])
    publish_quote(user_id, instrument)
    strategy = db.get(Strategy, strategy_id)
    first = asyncio.run(strategy_executor.evaluate(db, strategy, candles[-1]))
    second = asyncio.run(strategy_executor.evaluate(db, strategy, candles[-1]))
    assert first.id == second.id and first.status == StrategyRunStatus.SUCCEEDED
    assert db.scalar(select(func.count()).select_from(StrategySignal)) == 1
    assert db.scalar(select(func.count()).select_from(TradingOrder)) == 1
    signal = db.scalar(select(StrategySignal))
    assert signal.status == StrategySignalStatus.ORDER_SUBMITTED
    assert signal.order_id == db.scalar(select(TradingOrder)).id


def test_live_strategy_requires_separate_gates(client, db, monkeypatch):
    token, _, instrument, _ = setup_strategy(client, db, monkeypatch)
    response = client.post("/api/v1/strategies", headers=auth(token), json={
        "name": "Live cross", "instrument_id": instrument.id, "execution_mode": "LIVE",
        "fast_period": 2, "slow_period": 3,
    })
    strategy_id = response.json()["id"]
    denied = client.post(f"/api/v1/strategies/{strategy_id}/activate", headers=auth(token), json={})
    assert denied.status_code == 422 and denied.json()["detail"] == "STRATEGY_TRADING_MODE_MISMATCH"
    monkeypatch.setattr(strategy_executor.settings, "trading_mode", "live")
    denied = client.post(f"/api/v1/strategies/{strategy_id}/activate", headers=auth(token), json={})
    assert denied.status_code == 403 and denied.json()["detail"] == "MFA_REQUIRED_FOR_LIVE_STRATEGY"


def test_strategy_is_tenant_isolated(client, db, monkeypatch):
    token, _, _, created = setup_strategy(client, db, monkeypatch)
    register_verified(client, email="other-strategy@example.com")
    second = login(client, email="other-strategy@example.com")["access_token"]
    assert client.get(f"/api/v1/strategies/{created['id']}", headers=auth(second)).status_code == 404
    assert client.get("/api/v1/strategies", headers=auth(second)).json() == []


def test_worker_pauses_incomplete_run_without_replaying(client, db, monkeypatch):
    token, user_id, instrument, created = setup_strategy(client, db, monkeypatch)
    strategy_id = created["id"]
    client.post(f"/api/v1/strategies/{strategy_id}/activate", headers=auth(token), json={})
    candle = add_candles(db, user_id, instrument, [10, 9, 8, 12])[-1]
    db.add(StrategyRun(strategy_id=strategy_id, user_id=user_id, strategy_version=1,
                       candle_start_at=candle.start_at, status=StrategyRunStatus.STARTED,
                       diagnostics_json={}))
    db.commit()
    from tests.conftest import TestingSession
    monkeypatch.setattr("app.strategies.worker.SessionLocal", TestingSession)
    asyncio.run(StrategyWorker().run_once())
    assert db.get(Strategy, strategy_id).status == StrategyStatus.ERROR
    assert db.scalar(select(func.count()).select_from(TradingOrder)) == 0
    denied = client.post(f"/api/v1/strategies/{strategy_id}/activate", headers=auth(token), json={})
    assert denied.status_code == 409


def test_kill_switch_rejects_strategy_order_and_records_signal(client, db, monkeypatch):
    token, user_id, instrument, created = setup_strategy(client, db, monkeypatch)
    client.post(f"/api/v1/strategies/{created['id']}/activate", headers=auth(token), json={})
    candle = add_candles(db, user_id, instrument, [10, 9, 8, 12])[-1]
    publish_quote(user_id, instrument)
    db.add(TradingControl(scope_key="GLOBAL", is_halted=True, reason="test"))
    db.commit()
    run = asyncio.run(strategy_executor.evaluate(db, db.get(Strategy, created["id"]), candle))
    assert run.status == StrategyRunStatus.FAILED
    assert run.diagnostics_json["reason"] == "TRADING_HALTED"
    assert db.scalar(select(StrategySignal)).status == StrategySignalStatus.REJECTED
    assert db.scalar(select(func.count()).select_from(TradingOrder)) == 0


def test_missing_quote_skips_signal_without_order(client, db, monkeypatch):
    token, user_id, instrument, created = setup_strategy(client, db, monkeypatch)
    client.post(f"/api/v1/strategies/{created['id']}/activate", headers=auth(token), json={})
    candle = add_candles(db, user_id, instrument, [10, 9, 8, 12])[-1]
    run = asyncio.run(strategy_executor.evaluate(db, db.get(Strategy, created["id"]), candle))
    assert run.status == StrategyRunStatus.SKIPPED
    assert run.diagnostics_json["reason"] == "LIVE_QUOTE_REQUIRED"
    assert db.scalar(select(func.count()).select_from(TradingOrder)) == 0


def test_live_activation_needs_all_independent_confirmations(client, db, monkeypatch):
    token, user_id, instrument, _ = setup_strategy(client, db, monkeypatch)
    created = client.post("/api/v1/strategies", headers=auth(token), json={
        "name": "Live confirmed", "instrument_id": instrument.id,
        "execution_mode": "LIVE", "fast_period": 2, "slow_period": 3,
    }).json()
    db.get(User, user_id).mfa_enabled = True
    db.commit()
    monkeypatch.setattr(strategy_executor.settings, "trading_mode", "live")
    url = f"/api/v1/strategies/{created['id']}/activate"
    denied = client.post(url, headers=auth(token), json={"confirmation": "ENABLE_LIVE_STRATEGY"})
    assert denied.json()["detail"] == "LIVE_STRATEGIES_NOT_CONFIRMED"
    monkeypatch.setattr(strategy_executor.settings, "strategy_live_confirmation", "ENABLE_LIVE_STRATEGIES")
    denied = client.post(url, headers=auth(token), json={})
    assert denied.json()["detail"] == "LIVE_STRATEGY_ACTIVATION_NOT_CONFIRMED"
    denied = client.post(url, headers=auth(token), json={"confirmation": "ENABLE_LIVE_STRATEGY"})
    assert denied.json()["detail"] == "LIVE_TRADING_NOT_CONFIRMED"
    monkeypatch.setattr(strategy_executor.settings, "trading_live_confirmation", "ENABLE_DHAN_LIVE_ORDERS")
    monkeypatch.setattr(strategy_executor.settings, "dhan_static_ip_confirmed", True)
    accepted = client.post(url, headers=auth(token), json={"confirmation": "ENABLE_LIVE_STRATEGY"})
    assert accepted.status_code == 200 and accepted.json()["status"] == "ACTIVE"


def test_candle_gap_blocks_signal_and_daily_cap_blocks_followup(client, db, monkeypatch):
    token, user_id, instrument, created = setup_strategy(client, db, monkeypatch)
    client.post(f"/api/v1/strategies/{created['id']}/activate", headers=auth(token), json={})
    candles = add_candles(db, user_id, instrument, [10, 9, 8, 12])
    candles[0].start_at -= timedelta(minutes=1)
    db.commit()
    publish_quote(user_id, instrument)
    strategy = db.get(Strategy, created["id"])
    run = asyncio.run(strategy_executor.evaluate(db, strategy, candles[-1]))
    assert run.status == StrategyRunStatus.SKIPPED
    assert run.diagnostics_json["reason"] == "CANDLE_GAP"
    assert db.scalar(select(func.count()).select_from(TradingOrder)) == 0

    db.add(StrategySignal(strategy_id=strategy.id, run_id=run.id, user_id=user_id,
                          instrument_id=instrument.id, action="BUY",
                          status=StrategySignalStatus.ORDER_SUBMITTED, quantity=1,
                          reference_price=12, client_order_id="cap-test"))
    db.commit()
    monkeypatch.setattr(strategy_executor.settings, "strategy_max_orders_per_user_per_day", 1)
    with pytest.raises(StrategyError, match="USER_STRATEGY_DAILY_ORDER_LIMIT"):
        strategy_executor._order_count_check(db, strategy)


def test_unresolved_signal_blocks_reactivation(client, db, monkeypatch):
    token, user_id, instrument, created = setup_strategy(client, db, monkeypatch)
    candle = add_candles(db, user_id, instrument, [10, 9, 8, 12])[-1]
    run = StrategyRun(strategy_id=created["id"], user_id=user_id, strategy_version=1,
                      candle_start_at=candle.start_at, status=StrategyRunStatus.FAILED,
                      diagnostics_json={}, finished_at=datetime.now(timezone.utc))
    db.add(run)
    db.flush()
    db.add(StrategySignal(strategy_id=created["id"], run_id=run.id, user_id=user_id,
                          instrument_id=instrument.id, action="BUY",
                          status=StrategySignalStatus.CREATED, quantity=2,
                          reference_price=12, client_order_id="unresolved"))
    db.commit()
    result = client.post(f"/api/v1/strategies/{created['id']}/activate", headers=auth(token), json={})
    assert result.status_code == 409
    assert result.json()["detail"] == "Unresolved signal requires manual reconciliation"
