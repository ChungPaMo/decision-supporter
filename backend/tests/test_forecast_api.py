import copy
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.forecast import service
from engine.forecast import run
from engine.tools.make_forecast_examples import build_request

client = TestClient(app)


@pytest.fixture(scope="module")
def example():
    request = build_request()
    response = client.post("/dev/forecasts", json=request)
    assert response.status_code == 200
    return request, response.json()


def test_real_engine_response_is_lossless(example):
    request, response = example
    response = copy.deepcopy(response)
    expected = run(request)
    for body in (expected, response):
        body["run"].pop("generated_at")
    assert response == expected
    assert response["status"] == "PARTIAL"
    assert response["skipped"] and response["warnings"]
    for row in response["forecasts"]:
        assert Decimal(row["p50"]) <= Decimal(row["p80"]) <= Decimal(row["p90"])


def test_all_products_predicted():
    request = build_request()
    request["rows"] = [r for r in request["rows"] if r["product_id"] != "0001003"]
    response = client.post("/dev/forecasts", json=request)
    assert response.status_code == 200 and response.json()["status"] == "OK"


@pytest.mark.parametrize("change", [
    {"p50_method": "UNKNOWN"}, {"missing_month_policy": "REJECT"}, {"horizon_months": 13},
    {"extra": True},
])
def test_unsupported_input_is_explicit_422(change):
    response = client.post("/dev/forecasts", json={**build_request(), **change})
    assert response.status_code == 422 and response.json()["detail"]["code"] == "REQUEST_INVALID"


@pytest.mark.parametrize("source", ["CLIENT_UPLOAD", "PUBLIC_DEMAND"])
def test_development_api_rejects_non_synthetic_input(source):
    request = build_request()
    request["rows"][0]["source_type"] = source
    assert client.post("/dev/forecasts", json=request).status_code == 422


@pytest.mark.parametrize("change,code", [
    ({"trained_until": "2016-12-15"}, "TRAINED_UNTIL_NOT_MONTH_END"),
    ({"quantiles": ["P50", "P90"]}, "QUANTILES_UNSUPPORTED"),
])
def test_engine_invalid_has_null_run(change, code):
    response = client.post("/dev/forecasts", json={**build_request(), **change})
    body = response.json()
    assert response.status_code == 422 and body["status"] == "INPUT_INVALID" and body["run"] is None
    assert body["errors"][0]["code"] == code


def test_short_history_is_not_transport_error():
    request = build_request()
    request["rows"] = [r for r in request["rows"] if r["product_id"] == "0001003"]
    response = client.post("/dev/forecasts", json=request)
    assert response.status_code == 200 and response.json()["status"] == "DATA_INSUFFICIENT"
    assert response.json()["forecasts"] == [] and response.json()["skipped"]


@pytest.mark.parametrize("exception,status,code", [
    (RuntimeError("sensitive-path-or-secret"), 500, "FORECAST_ENGINE_ERROR"),
    (ImportError("sensitive-path-or-secret"), 503, "FORECAST_ENGINE_UNAVAILABLE"),
])
def test_engine_failure_is_safe(monkeypatch, exception, status, code):
    def fail(request):
        raise exception
    monkeypatch.setattr(service, "_engine_run", fail)
    response = client.post("/dev/forecasts", json=build_request())
    assert response.status_code == status and response.json()["detail"]["code"] == code
    assert "sensitive-path-or-secret" not in response.text


def test_optional_response_metadata_is_preserved(example, monkeypatch):
    request, response = example
    response = copy.deepcopy(response)
    response["run"]["generated_at"] = "2017-01-01T00:00:00Z"
    response["warnings"].append({"code": "FUTURE_WARNING", "detail": "test", "metadata": {"rows": [1, 2]}})
    monkeypatch.setattr(service, "_engine_run", lambda _: response)
    assert client.post("/dev/forecasts", json=request).json() == response
