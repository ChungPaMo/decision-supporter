"""배치 API 테스트 (DB 없음). 서비스에는 InMemoryBatchRepository 를 주입한다 — 실제 저장이 아니다."""
import json

import pytest
from fastapi.testclient import TestClient

from app.api.batches import get_service
from app.batches.service import BatchService
from app.main import app
from tests.fakes import InMemoryBatchRepository

HDR = "제품코드,제품명,기준월,수요량,단위\n"
GOOD = (HDR + "FG001,히터 A,2026-01,100,EA\nFG002,히터 B,2026-01,50,EA\n").encode()
HOLD = "제품코드,기준월,수량,주문수량,단위\nFG001,2026-01,1,2,EA\n".encode()


@pytest.fixture
def repo():
    return InMemoryBatchRepository(products={"FG001": "히터 A", "FG002": "히터 B"})


@pytest.fixture
def client(repo):
    svc = BatchService(repo)
    app.dependency_overrides[get_service] = lambda: svc
    yield TestClient(app)
    app.dependency_overrides.clear()


def upload(client, data=GOOD, name="d.csv", **form):
    return client.post("/batches", files={"file": (name, data)}, data=form)


def err(r):
    return r.json()["detail"]["code"]


# ---------------------------------------------------------------- 저장 비활성(기본 설정)
@pytest.mark.parametrize("method,path", [
    ("post", "/batches"), ("get", "/batches"), ("get", "/batches/00000000-0000-0000-0000-000000000000"),
    ("post", "/batches/00000000-0000-0000-0000-000000000000/revalidate"), ("post", "/batches/00000000-0000-0000-0000-000000000000/approve"),
    ("post", "/batches/00000000-0000-0000-0000-000000000000/reject"), ("get", "/demand/monthly"), ("get", "/products"),
])
def test_persistence_disabled_by_default_returns_501_not_fake_success(method, path):
    app.dependency_overrides.clear()
    c = TestClient(app)
    kwargs = {"files": {"file": ("d.csv", GOOD)}} if path == "/batches" and method == "post" else {"json": {}} if method == "post" else {}
    r = getattr(c, method)(path, **kwargs)
    assert r.status_code == 501 and err(r) == "PERSISTENCE_DISABLED"


def test_preview_still_works_without_persistence():
    app.dependency_overrides.clear()
    assert TestClient(app).post("/ingest/preview", files={"file": ("d.csv", GOOD)}).status_code == 200


# ---------------------------------------------------------------- 전체 흐름
def test_full_flow_upload_review_fix_approve_query(client):
    r = upload(client, HOLD, created_by="UPLOADER-1")
    assert r.status_code == 201
    b = r.json()
    assert b["status"] == "MAPPING_HOLD" and b["version"] == 1 and b["created_by"] == "UPLOADER-1"
    assert b["approval_blockers"][0]["code"] == "APPROVAL_BLOCKED_HOLD"
    assert b["validation"]["sheets"][0]["holds"][0]["candidates"] == ["수량", "주문수량"]
    bid = b["batch_id"]

    r = client.post(f"/batches/{bid}/revalidate", json={"expected_version": 1, "column_overrides": {"demand_qty": "주문수량"}})
    assert r.status_code == 200
    b = r.json()
    assert b["status"] == "READY_FOR_APPROVAL" and b["version"] == 2 and b["approval_blockers"] == []
    assert b["settings"]["column_overrides"] == {"demand_qty": "주문수량"}
    assert client.get("/demand/monthly").json()["total"] == 0                          # 승인 전: 조회 결과 없음

    r = client.post(f"/batches/{bid}/approve", json={"approved_by": "REVIEWER-01", "expected_result_digest": b["result_digest"]})
    assert r.status_code == 200
    a = r.json()
    assert a["status"] == "APPROVED" and a["approved_by"] == "REVIEWER-01" and a["approved_record_count"] == 1

    d = client.get("/demand/monthly", params={"product_id": "FG001"}).json()
    assert d["total"] == 1 and d["items"][0] == {"product_id": "FG001", "period": "2026-01", "demand_qty": "2", "unit": "EA",
                                                 "source_type": "CLIENT_UPLOAD", "batch_id": bid}
    assert client.get(f"/batches/{bid}").json()["status"] == "APPROVED"
    assert client.get("/batches", params={"status": "APPROVED"}).json()["total"] == 1


def test_error_codes_are_stable_and_shaped(client):
    bad = upload(client, (HDR + "FG001,A,2026-01,x,EA\n").encode()).json()
    assert bad["status"] == "HAS_ERRORS"
    i = bad["validation"]["sheets"][0]["issues"][0]
    assert i["code"] == "QTY_INVALID" and i["location"]["cell_ref"] == "D2" and i["severity"] == "ERROR"
    r = client.post(f"/batches/{bad['batch_id']}/approve", json={"approved_by": "REVIEWER-01", "expected_result_digest": bad["result_digest"]})
    assert r.status_code == 409 and r.json() == {"detail": {"code": "APPROVAL_BLOCKED_ERRORS", "message": r.json()["detail"]["message"],
                                                            "details": {"status": "HAS_ERRORS"}}}


def test_http_status_and_codes(client):
    ok = upload(client).json()
    assert err(upload(client, name="copy.csv")) == "DUPLICATE_UPLOAD"
    assert upload(client, name="copy.csv").status_code == 409
    assert err(upload(client, b"x", name="a.txt")) == "UNSUPPORTED_TYPE"
    assert upload(client, b"x", name="a.txt").status_code == 415
    assert err(upload(client, (HDR + "FG001,A,2026-02,1,EA\n").encode(), column_overrides="[1]")) == "BAD_COLUMN_OVERRIDES"
    assert err(upload(client, (HDR + "FG001,A,2026-02,1,EA\n").encode(), source_type="PUBLIC_DEMAND")) == "SOURCE_NOTE_REQUIRED"
    assert err(upload(client, (HDR + "FG001,A,2026-02,1,EA\n").encode(), entity="orders")) == "UNSUPPORTED_ENTITY"
    assert err(client.get("/batches/00000000-0000-0000-0000-000000000000")) == "BATCH_NOT_FOUND"
    r = client.get("/batches/not-a-uuid")
    assert r.status_code == 422 and err(r) == "REQUEST_INVALID" and r.json()["detail"]["details"][0]["loc"] == ["path", "batch_id"]
    r = client.post(f"/batches/{ok['batch_id']}/approve", json={"approved_by": "REVIEWER-01"})
    assert r.status_code == 422 and err(r) == "REQUEST_INVALID"
    r = client.post(f"/batches/{ok['batch_id']}/approve", json={"approved_by": "홍길동", "expected_result_digest": ok["result_digest"]})
    assert r.status_code == 422 and err(r) == "ACTOR_REQUIRED"
    r = client.post(f"/batches/{ok['batch_id']}/approve", json={"approved_by": "REVIEWER-01", "expected_result_digest": "0" * 64})
    assert r.status_code == 409 and err(r) == "RESULT_CHANGED"
    r = client.post(f"/batches/{ok['batch_id']}/revalidate", json={"expected_version": 9})
    assert r.status_code == 409 and err(r) == "VERSION_CONFLICT"


def test_duplicate_approval_and_changes_after_approval(client):
    b = upload(client).json()
    body = {"approved_by": "REVIEWER-01", "expected_result_digest": b["result_digest"]}
    assert client.post(f"/batches/{b['batch_id']}/approve", json=body).status_code == 200
    r = client.post(f"/batches/{b['batch_id']}/approve", json=body)
    assert r.status_code == 409 and err(r) == "ALREADY_APPROVED" and r.json()["detail"]["details"]["approved_by"] == "REVIEWER-01"
    r = client.post(f"/batches/{b['batch_id']}/revalidate", json={"expected_version": 2})
    assert r.status_code == 409 and err(r) == "BATCH_FINALIZED"
    r = client.post(f"/batches/{b['batch_id']}/reject", json={"rejected_by": "REVIEWER-01", "reason": "늦음"})
    assert r.status_code == 409
    assert client.get("/demand/monthly").json()["total"] == 2


def test_reject_flow(client):
    b = upload(client).json()
    r = client.post(f"/batches/{b['batch_id']}/reject", json={"rejected_by": "REVIEWER-01", "reason": "다른 파일"})
    assert r.status_code == 200 and r.json()["status"] == "REJECTED" and r.json()["reject_reason"] == "다른 파일"
    assert client.get("/demand/monthly").json()["total"] == 0
    assert upload(client, name="again.csv").status_code == 201                       # 반려 후 같은 파일 재업로드 허용


def test_storage_failure_returns_500_with_unchanged_state(client, repo):
    b = upload(client).json()
    repo.fail_insert_after = 1
    r = client.post(f"/batches/{b['batch_id']}/approve", json={"approved_by": "REVIEWER-01", "expected_result_digest": b["result_digest"]})
    assert r.status_code == 500 and err(r) == "PERSISTENCE_ERROR"
    assert client.get(f"/batches/{b['batch_id']}").json()["status"] == "READY_FOR_APPROVAL"
    assert client.get("/demand/monthly").json()["total"] == 0


def test_db_unavailable_returns_503(client, repo):
    repo.unavailable = True
    r = client.get("/batches")
    assert r.status_code == 503 and err(r) == "DB_UNAVAILABLE"


# ---------------------------------------------------------------- OpenAPI 계약 (화면 담당용)
def test_openapi_exposes_codes_statuses_and_paths():
    spec = TestClient(app).get("/openapi.json").json()
    schemas = spec["components"]["schemas"]
    assert set(schemas["BatchStatus"]["enum"]) == {"MAPPING_HOLD", "HAS_ERRORS", "READY_FOR_APPROVAL", "APPROVED", "REJECTED"}
    codes = set(schemas["ErrorCode"]["enum"])
    assert {"PERSISTENCE_DISABLED", "ALREADY_APPROVED", "APPROVAL_BLOCKED_HOLD", "APPROVAL_BLOCKED_ERRORS", "RESULT_CHANGED",
            "KEYS_ALREADY_APPROVED", "ACTOR_REQUIRED", "VERSION_CONFLICT", "DUPLICATE_UPLOAD"} <= codes
    paths = spec["paths"]
    for p in ("/ingest/preview", "/batches", "/batches/{batch_id}", "/batches/{batch_id}/revalidate", "/batches/{batch_id}/approve",
              "/batches/{batch_id}/reject", "/demand/monthly", "/products"):
        assert p in paths
    assert "409" in paths["/batches/{batch_id}/approve"]["post"]["responses"]
    assert schemas["BatchSummary"]["properties"]["result_digest"]["description"]


def test_error_code_enum_covers_every_file_error_code():
    from app.errors import ErrorCode
    from app.ingest import parsing
    src = open(parsing.__file__, encoding="utf-8").read()
    import re
    used = set(re.findall(r'FileError\("([A-Z_]+)"', src))
    assert used and used <= {c.value for c in ErrorCode}
