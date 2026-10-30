"""POST /ingest/preview — DB 없이 실행. 이 API 는 DB 를 사용하지 않는다."""
import json

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)
CSV = "제품코드,제품명,기준월,수요량,단위\n0001001,히터,2026-01,10,EA\n".encode()


def post(name="d.csv", data=CSV, **form):
    return client.post("/ingest/preview", files={"file": (name, data)}, data=form)


def test_preview_ok():
    r = post()
    assert r.status_code == 200
    b = r.json()
    assert b["status"] == "READY_FOR_APPROVAL" and b["auto_approve"] is False and b["approval_required"] is True
    assert b["sheets"][0]["records"][0]["product_id"] == "0001001"        # 선행 0 보존
    assert b["sheets"][0]["records"][0]["demand_qty"] == "10"             # 수량은 문자열


def test_preview_reports_error_positions():
    b = post(data="제품코드,기준월,수요량,단위\nFG1,2026-01,x,EA\n".encode()).json()
    issue = b["sheets"][0]["issues"][0]
    assert b["status"] == "HAS_ERRORS" and issue["code"] == "QTY_INVALID"
    assert issue["location"] == {"sheet": "", "row": 2, "col": 3, "cell_ref": "C2"}


def test_file_errors_are_structured():
    r = post(name="d.txt", data=b"x")
    assert r.status_code == 415 and r.json()["detail"]["code"] == "UNSUPPORTED_TYPE"
    r = post(name="d.xlsx", data=b"nope")
    assert r.status_code == 422 and r.json()["detail"]["code"] == "XLSX_CORRUPT"


def test_bad_form_inputs():
    assert post(entity="orders").json()["detail"]["code"] == "UNSUPPORTED_ENTITY"
    assert post(column_overrides="[1]").json()["detail"]["code"] == "BAD_COLUMN_OVERRIDES"
    assert post(column_overrides="not json").status_code == 422


def test_overrides_and_default_unit_via_api():
    data = "제품코드,기준월,수량,주문수량\nFG1,2026-01,1,2\n".encode()
    assert post(data=data, default_unit="EA").json()["status"] == "MAPPING_HOLD"
    b = post(data=data, default_unit="EA", column_overrides=json.dumps({"demand_qty": "주문수량"})).json()
    assert b["status"] == "READY_FOR_APPROVAL" and b["sheets"][0]["records"][0]["demand_qty"] == "2"


def test_openapi_documents_endpoint():
    assert "/ingest/preview" in client.get("/openapi.json").json()["paths"]
