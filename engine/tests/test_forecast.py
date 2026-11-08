"""예측 모듈 테스트. 실행: 저장소 루트에서 `python -m pytest engine/tests`."""

from __future__ import annotations

import copy
import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from engine.forecast import run
from engine.tools.make_forecast_examples import build_request

ROOT = Path(__file__).resolve().parents[2]


def _strip_time(res: dict) -> dict:
    res = copy.deepcopy(res)
    if res.get("run"):
        res["run"].pop("generated_at")
    return res


@pytest.fixture(scope="module")
def example():
    req = build_request()
    return req, run(req)


def _rows(pid, start_year, values, start_month=1):
    out, y, m = [], start_year, start_month
    for v in values:
        out.append({"product_id": pid, "period": f"{y:04d}-{m:02d}", "demand_qty": str(v), "unit": "EA",
                    "source_type": "SYNTHETIC", "batch_id": "B1"})
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def _req(rows, **kw):
    base = {"contract_version": "forecast-input-0.1-draft", "request_id": "T", "trained_until": "2016-12-31",
            "horizon_months": 3, "rows": rows}
    base.update(kw)
    return base


# ---------- 정상 흐름 ----------

def test_example_status_and_shape(example):
    _, res = example
    assert res["status"] == "PARTIAL"
    assert [s["product_id"] for s in res["skipped"]] == ["0001003"]
    assert res["skipped"][0]["reason_code"] == "INSUFFICIENT_HISTORY"
    assert {(f["product_id"], f["target_month"], f["horizon"]) for f in res["forecasts"]} == {
        (p, t, h) for p in ("0001001", "0001004", "0001005")
        for t, h in (("2017-01", 1), ("2017-02", 2), ("2017-03", 3))}
    codes = {w["code"] for w in res["warnings"]}
    assert {"ZERO_FILLED_MONTH", "SMALL_TRAINING_SET"} <= codes
    zf = next(w for w in res["warnings"] if w["code"] == "ZERO_FILLED_MONTH")
    assert zf == {**zf, "product_id": "0001001", "periods": ["2015-02"]}


def test_ma12_is_exact_mean_and_flat(example):
    _, res = example
    p50 = {f["p50"] for f in res["forecasts"] if f["product_id"] == "0001001"}
    assert p50 == {"400"}


def test_quantiles_ordered_and_decimal_strings(example):
    _, res = example
    for f in res["forecasts"]:
        p50, p80, p90 = (Decimal(f[k]) for k in ("p50", "p80", "p90"))
        assert Decimal(0) <= p50 <= p80 <= p90
        for k in ("p50", "p80", "p90"):
            assert isinstance(f[k], str) and "e" not in f[k].lower()
            assert -Decimal(f[k]).as_tuple().exponent <= 4


def test_rows_match_backend_forecast_row(example):
    """백엔드 계약 ForecastRow(app/contracts.py) 로 그대로 검증되는지."""
    sys.path.insert(0, str(ROOT / "backend"))
    try:
        from app.contracts import ForecastRow
    except ImportError as e:   # 백엔드 의존성이 없는 환경
        pytest.skip(f"backend contracts import 불가: {e}")
    finally:
        sys.path.pop(0)
    _, res = example
    for f in res["forecasts"]:
        ForecastRow.model_validate(f)


def test_deterministic(example):
    req, res = example
    assert _strip_time(run(copy.deepcopy(req))) == _strip_time(res)


def test_run_id_ignores_request_id(example):
    req, res = example
    other = run({**req, "request_id": "OTHER"})
    assert other["run"]["run_id"] == res["run"]["run_id"]


def test_lgbm_l1_p50(example):
    req, _ = example
    res = run({**req, "p50_method": "LGBM_L1"})
    assert res["status"] == "PARTIAL"
    assert {f["p50_method"] for f in res["forecasts"]} == {"LGBM_L1"}
    p50 = [f["p50"] for f in res["forecasts"] if f["product_id"] == "0001001"]
    assert p50 != ["400"] * 3   # 모델 예측이라 MA12 와 다름


def test_example_files_in_sync(example):
    """contracts/examples/engine/forecast_*.json 이 생성기 출력과 같은지 (generated_at 제외)."""
    req, res = example
    folder = ROOT / "contracts" / "examples" / "engine"
    assert json.loads((folder / "forecast_request.json").read_text(encoding="utf-8")) == req
    saved = json.loads((folder / "forecast_response.json").read_text(encoding="utf-8"))
    assert _strip_time(saved) == _strip_time(res)


def test_year_boundary_and_horizon():
    rows = _rows("A", 2015, [10 + i % 4 for i in range(23)]) + _rows("B", 2015, [20 + i % 3 for i in range(23)])
    res = run(_req(rows, trained_until="2016-11-30", horizon_months=3))
    got = sorted({(f["target_month"], f["horizon"]) for f in res["forecasts"]})
    assert got == [("2016-12", 1), ("2017-01", 2), ("2017-02", 3)]


def test_horizon_beyond_validated_warns():
    rows = _rows("A", 2014, [10 + i % 5 for i in range(36)])
    res = run(_req(rows, horizon_months=6))
    assert res["status"] == "OK"
    assert "HORIZON_BEYOND_VALIDATED" in {w["code"] for w in res["warnings"]}
    assert max(f["horizon"] for f in res["forecasts"]) == 6


# ---------- 제외·경고 ----------

def test_skips_all_zero_and_mixed_unit():
    rows = _rows("A", 2014, [10 + i % 3 for i in range(36)]) + _rows("Z", 2014, [0] * 36)
    mixed = _rows("M", 2014, [5] * 36)
    mixed[3]["unit"] = "BOX"
    res = run(_req(rows + mixed))
    reasons = {s["product_id"]: s["reason_code"] for s in res["skipped"]}
    assert reasons == {"Z": "ALL_ZERO", "M": "UNIT_MIXED"}
    assert res["status"] == "PARTIAL"


def test_zero_recent_demand_forecasts_zero():
    rows = _rows("A", 2014, [10 + i % 3 for i in range(36)]) + _rows("Q", 2014, [5] * 24 + [0] * 12)
    res = run(_req(rows))
    q = [f for f in res["forecasts"] if f["product_id"] == "Q"]
    assert q and all(f["p50"] == f["p80"] == f["p90"] == "0" for f in q)
    assert any(w["code"] == "ZERO_RECENT_DEMAND" and w["product_id"] == "Q" for w in res["warnings"])


def test_all_skipped_is_data_insufficient():
    res = run(_req(_rows("A", 2016, [5] * 6, start_month=7)))
    assert res["status"] == "DATA_INSUFFICIENT"
    assert res["forecasts"] == []


def test_no_training_rows():
    res = run(_req(_rows("A", 2016, [5] * 12)))   # 정확히 12개월: 목표 월 실적이 있는 학습 행 없음
    assert res["status"] == "DATA_INSUFFICIENT"
    assert res["errors"][0]["code"] == "NO_TRAINING_ROWS"


# ---------- 입력 오류 ----------

@pytest.mark.parametrize("mutate, code", [
    (lambda r: r.update(trained_until="2016-12-15"), "TRAINED_UNTIL_NOT_MONTH_END"),
    (lambda r: r["rows"].append({**r["rows"][0]}), "DUPLICATE_PERIOD"),
    (lambda r: r["rows"].append({**r["rows"][0], "period": "2017-01"}), "ROW_AFTER_TRAINED_UNTIL"),
    (lambda r: r.update(p50_method="LGBM_Q50"), "SCHEMA_INVALID"),
    (lambda r: r.update(quantiles=["P50", "P90"]), "QUANTILES_UNSUPPORTED"),
    (lambda r: r["rows"][0].update(demand_qty=12.5), "SCHEMA_INVALID"),          # JSON 숫자 거부
    (lambda r: r["rows"][0].update(demand_qty="1e3"), "SCHEMA_INVALID"),         # 지수 표기 거부
    (lambda r: r["rows"][0].update(demand_qty="1.23456"), "SCHEMA_INVALID"),     # 소수 5자리 거부
    (lambda r: r.update(horizon_months=13), "SCHEMA_INVALID"),
    (lambda r: r.update(extra_field=1), "SCHEMA_INVALID"),
])
def test_input_invalid(mutate, code):
    req = _req(_rows("A", 2014, [10] * 36))
    mutate(req)
    res = run(req)
    assert res["status"] == "INPUT_INVALID"
    assert code in {e["code"] for e in res["errors"]}
    assert res["forecasts"] == [] and res["run"] is None
