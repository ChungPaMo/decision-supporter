"""실제 PostgreSQL 경로만 검증한다. 쓰기/DDL은 chungpamo_test로 한정한다."""
import os
from pathlib import Path
import subprocess
import sys

from fastapi.testclient import TestClient
from sqlalchemy import text

from app import db
from app.api import batches
from app.config import Settings
from app.main import app
from tests.test_sql_repository import _clean

ROOT = Path(__file__).resolve().parents[2]


def test_empty_test_db_migrates_down_to_0001_and_up_sequentially(test_engine):
    with test_engine.connect() as c:
        assert c.execute(text("SELECT current_database()")).scalar_one() == "chungpamo_test"
    _clean(test_engine)
    env = {**os.environ, "DATABASE_URL": test_engine.url.render_as_string(hide_password=False)}
    for direction, revision in (("downgrade", "0001"), ("upgrade", "0002"), ("upgrade", "0003"), ("upgrade", "0004"), ("upgrade", "0005")):
        result = subprocess.run([sys.executable, "-m", "alembic", direction, revision],
                                cwd=ROOT / "backend", env=env, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        with test_engine.connect() as c:
            assert c.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == revision
            assert c.execute(text("SELECT count(*) FROM v_forecast_input_monthly_demand")).scalar_one() == 0


def test_api_real_postgres_upload_revalidate_approve_and_query(test_engine, monkeypatch):
    """서비스 override 없이 FastAPI → SQL 저장소 → 승인 트리거를 통과한다."""
    _clean(test_engine)
    settings = Settings(_env_file=None, persistence="postgres",
                        database_url=test_engine.url.render_as_string(hide_password=False))
    monkeypatch.setattr(batches, "get_settings", lambda: settings)
    monkeypatch.setattr(db, "get_engine", lambda: test_engine)
    assert not app.dependency_overrides
    try:
        with TestClient(app) as client:
            for entity, sample, path in (
                ("product", "sample_products.csv", "/products"),
                ("material", "sample_materials.csv", "/materials"),
                ("bom", "sample_bom.csv", "/boms"),
                ("inventory", "sample_inventory.csv", "/inventory"),
                ("monthly_demand", "sample_demand_hold.csv", "/demand/monthly"),
            ):
                response = client.post("/batches", files={"file": (sample, (ROOT / "contracts/examples" / sample).read_bytes())},
                                       data={"entity": entity, "source_type": "SYNTHETIC", "source_note": "통합 시험 합성 입력"})
                assert response.status_code == 201, response.json()
                batch = response.json()
                batch_path = f"/batches/{batch['batch_id']}"
                assert client.get(path).json()["total"] == 0
                if entity == "monthly_demand":
                    assert batch["status"] == "MAPPING_HOLD"
                    blocked = client.post(batch_path + "/approve", json={"approved_by": "REVIEWER-01", "expected_result_digest": batch["result_digest"]})
                    assert blocked.status_code == 409
                    response = client.post(batch_path + "/revalidate", json={"expected_version": batch["version"], "column_overrides": {"demand_qty": "주문수량"}})
                    assert response.status_code == 200, response.json()
                    batch = response.json()
                assert batch["status"] == "READY_FOR_APPROVAL"
                response = client.post(batch_path + "/approve", json={"approved_by": "REVIEWER-01", "expected_result_digest": batch["result_digest"]})
                assert response.status_code == 200, response.json()
                assert response.json()["status"] == "APPROVED"
                listed = client.get(path)
                assert listed.status_code == 200 and listed.json()["total"] > 0
                assert client.get(batch_path).json()["status"] == "APPROVED"
    finally:
        _clean(test_engine)
