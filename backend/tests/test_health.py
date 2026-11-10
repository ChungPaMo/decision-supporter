from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_reports_db_unavailable_without_leaking_details(monkeypatch):
    """DB 에 닿지 않으면 503 + degraded. 접속 정보·예외 문구는 응답에 없어야 한다. (DB 불필요)"""
    class UnavailableEngine:
        def connect(self):
            raise RuntimeError("secret_pw: simulated DB failure")

    monkeypatch.setattr("app.api.health.get_engine", UnavailableEngine)
    r = client.get("/health")
    assert r.status_code == 503
    assert r.json()["status"] == "degraded"
    assert r.json()["database"] == {"status": "unavailable"}
    assert "secret_pw" not in r.text


def test_health_ok_with_real_db(test_engine, monkeypatch):
    """전용 PostgreSQL 컨테이너가 떠 있을 때만 실행."""
    assert test_engine.url.database == "chungpamo_test"
    monkeypatch.setattr("app.api.health.get_engine", lambda: test_engine)
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["database"]["status"] == "ok"
    assert body["database"]["server_version"].startswith("16")
