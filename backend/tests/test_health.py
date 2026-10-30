from fastapi.testclient import TestClient

from app import db
from app.main import app

client = TestClient(app)


def test_health_reports_db_unavailable_without_leaking_details(monkeypatch):
    """DB 에 닿지 않으면 503 + degraded. 접속 정보·예외 문구는 응답에 없어야 한다. (DB 불필요)"""
    from sqlalchemy import create_engine
    bad = create_engine("postgresql+psycopg://u:secret_pw@127.0.0.1:1/x", connect_args={"connect_timeout": 1})
    monkeypatch.setattr("app.api.health.get_engine", lambda: bad)
    r = client.get("/health")
    assert r.status_code == 503
    assert r.json()["status"] == "degraded"
    assert r.json()["database"] == {"status": "unavailable"}
    assert "secret_pw" not in r.text


def test_health_ok_with_real_db(test_engine):
    """전용 PostgreSQL 컨테이너가 떠 있을 때만 실행."""
    db.get_engine.cache_clear()
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["database"]["status"] == "ok"
    assert body["database"]["server_version"].startswith("16")
