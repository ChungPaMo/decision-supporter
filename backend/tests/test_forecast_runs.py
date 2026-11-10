import copy
import json
import os
from pathlib import Path
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app import db
from app.api import forecasts
from app.batches.service import BatchService
from app.batches.sql_repository import SqlBatchRepository
from app.config import Settings
from app.forecast import service as adapter
from app.forecast.runs import ForecastRuns
from app.forecast import runs as runs_module
from app.main import app
from engine.forecast import run
from engine.tools.make_forecast_examples import build_request
from tests.test_sql_repository import _clean

PARAMETERS = {"trained_until": "2016-12-31", "horizon_months": 3, "p50_method": "MA12"}


@pytest.fixture
def environment(test_engine, monkeypatch):
    _clean(test_engine)
    settings = Settings(_env_file=None, persistence="postgres", database_url=test_engine.url.render_as_string(hide_password=False))
    monkeypatch.setattr(forecasts, "get_settings", lambda: settings)
    monkeypatch.setattr(db, "get_engine", lambda: test_engine)
    service = BatchService(SqlBatchRepository(test_engine))
    yield TestClient(app), service, test_engine
    _clean(test_engine)


def approve(service, name, content, entity="monthly_demand"):
    batch = service.create_batch(name, content.encode(), entity=entity, source_type="SYNTHETIC", source_note="forecast integration synthetic")
    assert batch.status == "READY_FOR_APPROVAL", batch.validation_result
    return service.approve(batch.batch_id, approved_by="REVIEWER-01", expected_result_digest=batch.result_digest)


def seed(service):
    rows = build_request()["rows"]
    products = sorted({r["product_id"] for r in rows})
    approve(service, "products.csv", "제품코드,제품명,단위\n" + "".join(f"{p},Synthetic {p},EA\n" for p in products), "product")
    return approve(service, "history.csv", "제품코드,기준월,수요량,단위\n" + "".join(
        f"{r['product_id']},{r['period']},{r['demand_qty']},{r['unit']}\n" for r in rows))


def counts(engine):
    with engine.connect() as c:
        return tuple(c.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
                     for table in ("forecast_run", "forecast_run_input", "demand_forecast"))


def test_run_snapshot_store_retrieve_and_unapproved_exclusion(environment):
    client, service, engine = environment
    batch = seed(service)
    pending = service.create_batch("pending.csv", b"product_id,period,demand_qty,unit\n0001001,2015-02,999,EA\n")
    assert pending.status == "READY_FOR_APPROVAL"
    future = approve(service, "future.csv", "제품코드,기준월,수요량,단위\n0001001,2017-01,777,EA\n")
    response = client.post("/forecasts", json=PARAMETERS)
    assert response.status_code == 201, response.json()
    body = response.json()
    execution = body["execution"]
    assert not body["reused"] and execution["status"] == "PARTIAL"
    assert execution["result"]["skipped"] and execution["result"]["warnings"]
    assert execution["input_digest"] == execution["result"]["run"]["input_digest"]
    assert execution["quantile_model_version"] == execution["result"]["run"]["quantile_model_version"]
    assert [b["batch_id"] for b in execution["input_batches"]] == [str(batch.batch_id)]
    assert execution["input_batches"][0]["batch_version"] == batch.version
    assert execution["input_batches"][0]["source_sha256"] == batch.source_sha256
    assert execution["input_batches"][0]["result_digest"] == batch.result_digest
    with engine.connect() as c:
        snapshot = c.execute(text("SELECT input_snapshot FROM forecast_run WHERE execution_id=:id"), {"id": execution["execution_id"]}).scalar_one()
        assert all(r["batch_id"] not in (str(pending.batch_id), str(future.batch_id)) for r in snapshot["rows"])
        assert not any(r["product_id"] == "0001001" and r["period"] == "2015-02" for r in snapshot["rows"])
        assert c.execute(text("SELECT count(*) FROM demand_forecast WHERE forecast_run_id=:id"), {"id": execution["execution_id"]}).scalar_one() == len(execution["result"]["forecasts"])
        assert service.get_batch(batch.batch_id).version == batch.version
    reproduced = run(snapshot)
    stored = copy.deepcopy(execution["result"])
    for item in (stored, reproduced):
        item["run"].pop("generated_at")
    assert stored == reproduced
    fetched = client.get(f"/forecasts/{execution['execution_id']}")
    assert fetched.status_code == 200 and fetched.json() == execution
    assert client.get("/forecasts").json()["total"] == 1
    assert client.get("/forecasts/00000000-0000-0000-0000-000000000000").status_code == 404


def test_identical_input_reused_and_p50_methods_can_coexist(environment):
    client, service, engine = environment
    seed(service)
    first = client.post("/forecasts", json=PARAMETERS).json()["execution"]
    duplicate = client.post("/forecasts", json=PARAMETERS)
    assert duplicate.status_code == 200 and duplicate.json()["reused"]
    assert duplicate.json()["execution"]["execution_id"] == first["execution_id"]
    other = client.post("/forecasts", json={**PARAMETERS, "p50_method": "LGBM_L1"})
    assert other.status_code == 201, other.json()
    assert other.json()["execution"]["input_digest"] != first["input_digest"]
    assert counts(engine) == (2, 2, 18)
    assert client.get("/forecasts?limit=1&offset=1").json()["total"] == 2


@pytest.mark.parametrize("parameters,status,code", [
    (PARAMETERS, "DATA_INSUFFICIENT", None),
    ({**PARAMETERS, "trained_until": "2016-12-15"}, "INPUT_INVALID", "TRAINED_UNTIL_NOT_MONTH_END"),
])
def test_no_data_and_invalid_run_are_saved_without_fake_forecasts(environment, parameters, status, code):
    client, _, engine = environment
    response = client.post("/forecasts", json=parameters)
    assert response.status_code == (422 if code else 201)
    execution = response.json()["execution"]
    assert execution["status"] == status and execution["forecast_count"] == 0
    assert execution["result"]["forecasts"] == []
    if code:
        assert execution["result"]["run"] is None and execution["result"]["errors"][0]["code"] == code
    assert counts(engine) == (1, 0, 0)
    assert client.get(f"/forecasts/{execution['execution_id']}").json()["status"] == status


def test_no_training_rows_error_and_exclusion_are_preserved(environment):
    client, service, _ = environment
    seed(service)
    response = client.post("/forecasts", json={**PARAMETERS, "trained_until": "2014-12-31"})
    result = response.json()["execution"]["result"]
    assert result["status"] == "DATA_INSUFFICIENT" and result["errors"][0]["code"] == "NO_TRAINING_ROWS"
    response = client.post("/forecasts", json={**PARAMETERS, "min_history_months": 120})
    result = response.json()["execution"]["result"]
    assert result["status"] == "DATA_INSUFFICIENT" and len(result["skipped"]) == 4


def test_model_failure_history_and_retry(environment, monkeypatch):
    client, service, engine = environment
    seed(service)
    original = adapter._engine_run
    def fail(request):
        raise RuntimeError("sensitive-secret-path")
    monkeypatch.setattr(adapter, "_engine_run", fail)
    first = client.post("/forecasts", json=PARAMETERS)
    second = client.post("/forecasts", json=PARAMETERS)
    assert first.status_code == second.status_code == 500
    assert first.json()["execution"]["status"] == "FAILED" and first.json()["execution"]["result"] is None
    assert first.json()["execution"]["execution_id"] != second.json()["execution"]["execution_id"]
    assert "sensitive-secret-path" not in first.text and counts(engine) == (2, 2, 0)
    monkeypatch.setattr(adapter, "_engine_run", original)
    recovered = client.post("/forecasts", json=PARAMETERS)
    assert recovered.status_code == 201 and recovered.json()["execution"]["status"] == "PARTIAL"


def test_real_db_save_failure_rolls_back_run_inputs_and_rows(environment, monkeypatch):
    client, service, engine = environment
    batch = seed(service)
    original = ForecastRuns._save_rows
    def broken(c, execution_id, rows):
        original(c, execution_id, [{**rows[0], "product_id": "NO_SUCH_PRODUCT"}])
    monkeypatch.setattr(ForecastRuns, "_save_rows", staticmethod(broken))
    response = client.post("/forecasts", json=PARAMETERS)
    assert response.status_code == 500 and response.json()["detail"]["code"] == "PERSISTENCE_ERROR"
    assert counts(engine) == (0, 0, 0)
    assert service.get_batch(batch.batch_id).status == "APPROVED"


def test_input_reference_rejects_unapproved_batch(environment):
    client, service, engine = environment
    batch = seed(service)
    pending = service.create_batch("pending.csv", b"product_id,period,demand_qty,unit\n0001001,2015-02,999,EA\n")
    execution = client.post("/forecasts", json=PARAMETERS).json()["execution"]
    with pytest.raises(IntegrityError) as error:
        with engine.begin() as c:
            c.execute(text("INSERT INTO forecast_run_input(execution_id,batch_id,batch_version,source_sha256,result_digest,approved_at) "
                           "VALUES (:id,:batch,:version,:sha,:digest,now())"),
                      {"id": execution["execution_id"], "batch": pending.batch_id, "version": pending.version,
                       "sha": pending.source_sha256, "digest": pending.result_digest})
    assert error.value.orig.sqlstate == "23514"
    assert counts(engine)[1] == 1


def test_simultaneous_same_input_conflicts_then_reuses(environment, monkeypatch):
    client, service, engine = environment
    seed(service)
    entered, release = Event(), Event()
    original = adapter._engine_run
    calls = []
    def held(request):
        calls.append(request)
        entered.set()
        assert release.wait(10)
        return original(request)
    monkeypatch.setattr(adapter, "_engine_run", held)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(client.post, "/forecasts", json=PARAMETERS)
        try:
            assert entered.wait(5)
            second = client.post("/forecasts", json=PARAMETERS)
            assert second.status_code == 409 and second.json()["detail"]["code"] == "FORECAST_IN_PROGRESS"
        finally:
            release.set()
        assert first.result(timeout=10).status_code == 201
    assert client.post("/forecasts", json=PARAMETERS).status_code == 200
    assert len(calls) == 1 and counts(engine) == (1, 1, 9)


def test_client_rows_cannot_override_approved_input(environment):
    client, _, engine = environment
    assert client.post("/forecasts", json={**PARAMETERS, "rows": []}).status_code == 422
    assert counts(engine) == (0, 0, 0)


def test_new_approved_input_and_runtime_version_create_new_runs(environment, monkeypatch):
    client, service, engine = environment
    seed(service)
    first = client.post("/forecasts", json=PARAMETERS).json()["execution"]
    approve(service, "gap.csv", "제품코드,기준월,수요량,단위\n0001001,2015-02,999,EA\n")
    second = client.post("/forecasts", json=PARAMETERS)
    assert second.status_code == 201
    second = second.json()["execution"]
    assert first["input_digest"] != second["input_digest"] and len(second["input_batches"]) == 2
    identity = runs_module.runtime_identity()
    monkeypatch.setattr(runs_module, "runtime_identity", lambda: {**identity, "engine_version": "test-next-version"})
    third = client.post("/forecasts", json=PARAMETERS)
    assert third.status_code == 201
    third = third.json()["execution"]
    assert third["input_digest"] == second["input_digest"] and third["dedup_key"] != second["dedup_key"]
    assert client.get(f"/forecasts/{first['execution_id']}").json() == first
    assert counts(engine) == (3, 5, 27)


def test_missing_model_dependency_is_saved_as_failed(environment, monkeypatch):
    client, service, _ = environment
    seed(service)
    def missing(request):
        raise ImportError("private-library-path")
    monkeypatch.setattr(adapter, "_engine_run", missing)
    response = client.post("/forecasts", json=PARAMETERS)
    assert response.status_code == 503 and response.json()["execution"]["status"] == "FAILED"
    assert response.json()["execution"]["failure"]["code"] == "FORECAST_ENGINE_UNAVAILABLE"
    assert "private-library-path" not in response.text


def test_invalid_model_quantiles_are_not_saved_as_predictions(environment, monkeypatch):
    client, service, engine = environment
    seed(service)
    def invalid(request):
        result = run(request)
        result["forecasts"][0]["p90"] = "NaN"
        return result
    monkeypatch.setattr(adapter, "_engine_run", invalid)
    response = client.post("/forecasts", json=PARAMETERS)
    assert response.status_code == 500 and response.json()["execution"]["status"] == "FAILED"
    assert counts(engine) == (1, 1, 0)


def test_0005_preserves_legacy_results_and_allows_new_run(environment):
    client, service, engine = environment
    seed(service)
    from engine.forecast.service import QUANTILE_MODEL_VERSION
    with engine.begin() as c:
        c.execute(text("INSERT INTO demand_forecast(product_id,target_month,horizon,p50,p80,p90,p50_method,quantile_model_version,trained_until) "
                       "VALUES ('0001001','2017-01',1,1,2,3,'MA12',:model,'2016-12-31')"), {"model": QUANTILE_MODEL_VERSION})
    env = {**os.environ, "DATABASE_URL": engine.url.render_as_string(hide_password=False)}
    for direction, revision in (("downgrade", "0004"), ("upgrade", "0005")):
        result = subprocess.run([sys.executable, "-m", "alembic", direction, revision],
                                cwd=Path(__file__).resolve().parents[1], env=env, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
    with engine.connect() as c:
        row = c.execute(text("SELECT forecast_run_id,p50,p80,p90 FROM demand_forecast")).one()
        assert row[0] is None and tuple(row[1:]) == (1, 2, 3)
    created = client.post("/forecasts", json=PARAMETERS)
    assert created.status_code == 201 and counts(engine) == (1, 1, 10)
    # A populated execution history must not be silently destroyed.
    result = subprocess.run([sys.executable, "-m", "alembic", "downgrade", "0004"],
                            cwd=Path(__file__).resolve().parents[1], env=env, capture_output=True, text=True)
    assert result.returncode != 0 and "cannot downgrade 0005 with forecast history" in result.stderr
    assert client.get(f"/forecasts/{created.json()['execution']['execution_id']}").status_code == 200


@pytest.mark.parametrize("method,path", [("post", "/forecasts"), ("get", "/forecasts"), ("get", "/forecasts/00000000-0000-0000-0000-000000000000")])
def test_official_api_requires_persistence(method, path):
    response = getattr(TestClient(app), method)(path, **({"json": PARAMETERS} if method == "post" else {}))
    assert response.status_code == 501 and response.json()["detail"]["code"] == "PERSISTENCE_DISABLED"
