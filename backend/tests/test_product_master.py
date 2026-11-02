"""제품 마스터 업로드·검증·승인 (DB 없음). InMemoryBatchRepository 는 테스트 전용이며 DB 동작을 증명하지 않는다."""
import io

import openpyxl
import pytest
from fastapi.testclient import TestClient

from app.api.batches import get_service
from app.batches.service import BatchService, approval_blockers
from app.errors import ApiError, ErrorCode
from app.ingest.pipeline import run_preview
from app.main import app
from tests.fakes import InMemoryBatchRepository

P_HDR = "제품코드,제품명,단위,제품유형,안전재고\n"
GOOD = (P_HDR + "0001001,히터 A,EA,완제품,10\n0001002,히터 B,개,반제품,0\n").encode()
DEMAND_HDR = "제품코드,제품명,기준월,수요량,단위\n"
DEMAND = (DEMAND_HDR + "0001001,히터 A,2026-01,100,EA\n0001002,히터 B,2026-01,50,EA\n").encode()
APPROVER = "REVIEWER-01"


@pytest.fixture
def repo():
    return InMemoryBatchRepository()


@pytest.fixture
def svc(repo):
    return BatchService(repo)


def upload(svc, data=GOOD, name="p.csv", **kw):
    return svc.create_batch(name, data, entity="product", **kw)


def approve(svc, b, by=APPROVER):
    return svc.approve(b.batch_id, approved_by=by, expected_result_digest=b.result_digest)


def issues(b):
    return [(i["code"], i["location"]["cell_ref"] if i["location"] else None) for i in b.validation_result["sheets"][0]["issues"]]


def products(svc):
    return svc.list_products(None, None, 1000, 0)


def code_of(e):
    return e.value.status, e.value.code


# ---------------------------------------------------------------- 1. 정상 등록
def test_register_products_preserves_codes_and_normalizes_values(svc, repo):
    b = upload(svc)
    assert b.status == "READY_FOR_APPROVAL" and b.entity == "product" and approval_blockers(b) == []
    assert products(svc) == ([], 0)                                    # 승인 전 반영 없음
    a = approve(svc, b)
    assert a.status == "APPROVED" and a.approved_record_count == 2
    items, total = products(svc)
    assert total == 2
    assert [(p.product_id, p.product_name, p.base_unit, p.product_type, str(p.safety_stock_qty)) for p in items] == [
        ("0001001", "히터 A", "EA", "FINISHED", "10"), ("0001002", "히터 B", "EA", "SEMI_FINISHED", "0")]
    assert items[0].batch_id == b.batch_id and items[0].source_type == "CLIENT_UPLOAD"
    assert repo.state.demand == {}


def test_optional_columns_may_be_absent_and_nothing_is_invented(svc):
    b = upload(svc, "품번,품명,UOM\nA-1,볼트,kg\n".encode())
    assert b.status == "READY_FOR_APPROVAL"
    assert ("SAFETY_STOCK_DEFAULTED", None) in issues(b)
    approve(svc, b)
    p = products(svc)[0][0]
    assert (p.product_type, str(p.safety_stock_qty), p.base_unit) == (None, "0", "kg")


def test_blank_safety_stock_warns_and_xlsx_text_code_keeps_leading_zero(svc):
    wb = openpyxl.Workbook()
    ws = wb.active
    for row in [["제품코드", "제품명", "기준단위", "안전재고"], ["0001001", "히터", "EA", None]]:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    b = upload(svc, buf.getvalue(), name="p.xlsx")
    assert b.status == "READY_FOR_APPROVAL" and b.validation_result["sheets"][0]["records"][0]["product_id"] == "0001001"
    assert any(c == "SAFETY_STOCK_DEFAULTED" and ref == "D2" for c, ref in issues(b))


def test_numeric_code_cell_warns_about_lost_zeros(svc):
    wb = openpyxl.Workbook()
    ws = wb.active
    for row in [["제품코드", "제품명", "단위"], [1001, "히터", "EA"]]:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    b = upload(svc, buf.getvalue(), name="p.xlsx")
    assert ("CODE_NUMERIC_CELL", "A2") in issues(b) and b.validation_result["sheets"][0]["records"][0]["review_required"] is True


# ---------------------------------------------------------------- 2·3. 중복·충돌
def test_duplicate_product_id_is_error_even_with_same_values(svc):
    b = upload(svc, (P_HDR + "FG001,히터,EA,완제품,1\nFG001,히터,EA,완제품,1\n").encode())
    assert b.status == "HAS_ERRORS"
    assert {x for x in issues(b) if x[0] == "DUPLICATE_PRODUCT_ID"} == {("DUPLICATE_PRODUCT_ID", "A2"), ("DUPLICATE_PRODUCT_ID", "A3")}
    assert b.validation_result["sheets"][0]["records"] == []


def test_code_name_conflict_in_file_reports_both_directions(svc):
    b = upload(svc, (P_HDR + "FG001,히터 A,EA,,\nFG001,히터 B,EA,,\nFG002,히터 A,EA,,\n").encode())
    codes = {c for c, _ in issues(b)}
    assert b.status == "HAS_ERRORS" and {"CODE_NAME_CONFLICT", "NAME_CODE_CONFLICT", "DUPLICATE_PRODUCT_ID"} <= codes
    assert b.validation_result["sheets"][0]["records"] == []


def test_existing_product_with_different_value_is_conflict_and_not_overwritten(svc):
    approve(svc, upload(svc))
    for row, field, ref in (("0001001,히터 Z,EA,완제품,10", "product_name", "B2"), ("0001001,히터 A,kg,완제품,10", "base_unit", "C2"),
                            ("0001001,히터 A,EA,반제품,10", "product_type", "D2"), ("0001001,히터 A,EA,완제품,11", "safety_stock_qty", "E2")):
        b = upload(svc, (P_HDR + row + "\n").encode(), name=f"{field}.csv")
        assert b.status == "HAS_ERRORS" and ("EXISTING_PRODUCT_CONFLICT", ref) in issues(b), field
    assert products(svc)[0][0].product_name == "히터 A"


def test_new_code_with_registered_name_is_conflict(svc):
    approve(svc, upload(svc))
    b = upload(svc, (P_HDR + "9999,히터  a,EA,,\n").encode(), name="n.csv")          # 공백·대소문자 정규화 후 동일
    assert b.status == "HAS_ERRORS" and ("EXISTING_NAME_CONFLICT", "A2") in issues(b)


def test_same_values_are_skipped_and_all_skipped_batch_cannot_be_approved(svc):
    approve(svc, upload(svc))
    b = upload(svc, (P_HDR + "0001001,히터 A,EA,완제품,10\n").encode(), name="again.csv")
    assert b.status == "READY_FOR_APPROVAL" and ("PRODUCT_ALREADY_REGISTERED", "A2") in issues(b)
    assert [x["code"] for x in approval_blockers(b)] == [ErrorCode.EMPTY_BATCH]
    with pytest.raises(ApiError) as e:
        approve(svc, b)
    assert code_of(e) == (409, ErrorCode.EMPTY_BATCH)
    mixed = upload(svc, (P_HDR + "0001001,히터 A,EA,완제품,10\n0001003,히터 C,EA,,\n").encode(), name="mixed.csv")
    assert approve(svc, mixed).approved_record_count == 1 and products(svc)[1] == 3


def test_leading_zero_variant_of_registered_code_warns_and_is_not_merged(svc):
    approve(svc, upload(svc))
    b = upload(svc, (P_HDR + "1001,새 히터,EA,,\n").encode(), name="v.csv")
    assert b.status == "READY_FOR_APPROVAL" and ("CODE_VARIANT", "A2") in issues(b)


# ---------------------------------------------------------------- 4·5. 값 검사
@pytest.mark.parametrize("row,code,ref", [
    ("FG001,히터,상자,,", "UNIT_UNKNOWN", "C2"),
    ("FG001,히터,,,", "UNIT_MISSING", "C2"),
    ("FG001,히터,EA,,-5", "QTY_NEGATIVE", "E2"),
    ("FG001,히터,EA,,abc", "QTY_INVALID", "E2"),
    ("FG001,히터,EA,,2026-01-01", "QTY_INVALID", "E2"),
    ("FG001,히터,EA,,1e3", "QTY_INVALID", "E2"),
    ("FG001,히터,EA,,１２", "QTY_INVALID", "E2"),
    ("FG001,히터,EA,부품,", "PRODUCT_TYPE_UNKNOWN", "D2"),
    ("FG001,,EA,,", "NAME_MISSING", "B2"),
    ("FG001,#REF!,EA,,", "NAME_INVALID", "B2"),
    ("FG001,히터\x07,EA,,", "NAME_INVALID", "B2"),
    ("FG001,깨진�글자,EA,,", "NAME_INVALID", "B2"),
    (",히터,EA,,", "CODE_MISSING", "A2"),
    ("FG 001,히터,EA,,", "CODE_INVALID", "A2"),
])
def test_invalid_values_with_cell_position(svc, row, code, ref):
    b = upload(svc, (P_HDR + row + "\n").encode())
    assert b.status == "HAS_ERRORS" and (code, ref) in issues(b)
    assert b.validation_result["sheets"][0]["records"] == []


def test_error_location_matches_existing_issue_format(svc):
    b = upload(svc, (P_HDR + "FG001,히터,상자,,\n").encode())
    i = b.validation_result["sheets"][0]["issues"][0]
    assert set(i) == {"severity", "code", "message", "field", "location", "raw_value", "evidence"}
    assert i["location"] == {"sheet": "", "row": 2, "col": 3, "cell_ref": "C2"} and i["field"] == "base_unit"


def test_demand_file_uploaded_as_product_is_flagged(svc):
    b = upload(svc, (DEMAND_HDR + "FG001,히터,2026-01,5,EA\n").encode())
    assert any(c == "LOOKS_LIKE_OTHER_ENTITY" for c, _ in issues(b))


def test_default_unit_does_not_replace_required_unit_column(svc):
    b = upload(svc, "제품코드,제품명\nFG001,히터\n".encode(), default_unit="EA")
    assert b.status == "MAPPING_HOLD" and b.validation_result["sheets"][0]["holds"][0]["field"] == "base_unit"


# ---------------------------------------------------------------- 6·7. 승인 거부, 재검증
def test_error_batch_cannot_be_approved(svc):
    b = upload(svc, (P_HDR + "FG001,히터,상자,,\n").encode())
    with pytest.raises(ApiError) as e:
        approve(svc, b)
    assert code_of(e) == (409, ErrorCode.APPROVAL_BLOCKED_ERRORS) and products(svc) == ([], 0)


def test_hold_batch_cannot_be_approved_until_columns_are_chosen(svc):
    data = "제품코드,제품명,단위명칭\nFG001,히터,EA\n".encode()
    b = upload(svc, data)
    assert b.status == "MAPPING_HOLD"
    with pytest.raises(ApiError) as e:
        approve(svc, b)
    assert code_of(e) == (409, ErrorCode.APPROVAL_BLOCKED_HOLD) and products(svc) == ([], 0)
    r = svc.revalidate(b.batch_id, expected_version=b.version, column_overrides={"base_unit": "단위명칭"}, default_unit=None, sheet=None)
    assert r.status == "READY_FOR_APPROVAL" and approve(svc, r).status == "APPROVED" and products(svc)[1] == 1


def test_overrides_are_checked_against_the_batch_entity(svc):
    b = upload(svc, "제품코드,제품명,단위명칭\nFG001,히터,EA\n".encode())
    with pytest.raises(ApiError) as e:
        svc.revalidate(b.batch_id, expected_version=1, column_overrides={"demand_qty": "A"}, default_unit=None, sheet=None)
    assert code_of(e) == (422, ErrorCode.BAD_COLUMN_OVERRIDES)


def test_digest_and_approver_are_required(svc):
    b = upload(svc)
    with pytest.raises(ApiError) as e:
        svc.approve(b.batch_id, approved_by=APPROVER, expected_result_digest="0" * 64)
    assert code_of(e) == (409, ErrorCode.RESULT_CHANGED)
    with pytest.raises(ApiError) as e:
        svc.approve(b.batch_id, approved_by=None, expected_result_digest=b.result_digest)
    assert code_of(e) == (422, ErrorCode.ACTOR_REQUIRED)
    assert products(svc) == ([], 0)


def test_result_changes_if_another_batch_registers_first(svc):
    """승인 대기 중 다른 배치가 같은 제품을 다르게 등록하면 재검증 결과가 달라져 승인이 멈춘다 (덮어쓰기 없음)."""
    waiting = upload(svc, (P_HDR + "FG001,히터 A,EA,,\n").encode(), name="a.csv")
    approve(svc, upload(svc, (P_HDR + "FG001,다른 이름,EA,,\n").encode(), name="b.csv"))
    with pytest.raises(ApiError) as e:
        approve(svc, waiting)
    assert code_of(e) == (409, ErrorCode.RESULT_CHANGED)
    assert products(svc)[0][0].product_name == "다른 이름"


# ---------------------------------------------------------------- 8. 중복 승인·반영
def test_duplicate_approval_does_not_write_again(svc, repo):
    b = upload(svc)
    first = approve(svc, b)
    with pytest.raises(ApiError) as e:
        approve(svc, b)
    assert code_of(e) == (409, ErrorCode.ALREADY_APPROVED) and products(svc)[1] == 2
    assert svc.get_batch(b.batch_id).version == first.version


def test_same_file_cannot_be_uploaded_twice_but_may_serve_two_entities(svc):
    data = "제품코드,제품명,기준월,수요량,단위\nFG001,히터,2026-01,1,EA\n".encode()
    upload(svc, data)
    with pytest.raises(ApiError) as e:
        upload(svc, data, name="copy.csv")
    assert code_of(e) == (409, ErrorCode.DUPLICATE_UPLOAD)
    assert svc.create_batch("d.csv", data, entity="monthly_demand").entity == "monthly_demand"


# ---------------------------------------------------------------- 9. 저장 실패·롤백
def test_storage_failure_rolls_back_everything(svc, repo):
    b = upload(svc)
    repo.fail_insert_after = 1                                       # 2행 중 1행 쓴 뒤 실패
    with pytest.raises(ApiError) as e:
        approve(svc, b)
    assert code_of(e) == (500, ErrorCode.PERSISTENCE_ERROR) and repo.rollbacks == 1
    after = svc.get_batch(b.batch_id)
    assert after.status == "READY_FOR_APPROVAL" and after.approved_by is None and repo.state.products == {}
    repo.fail_insert_after = None
    assert approve(svc, after).status == "APPROVED" and products(svc)[1] == 2


def test_fake_store_refuses_wrong_entity_writes(svc, repo):
    """배치 entity 와 대상 테이블이 다르면 저장소가 거부한다 (DB 트리거의 모형)."""
    from app.batches.repository import RepositoryError
    b = upload(svc)
    with repo.transaction() as tx:
        tx.mark_approved(b.batch_id, APPROVER, 2)
        with pytest.raises(RepositoryError, match="matching entity"):
            tx.insert_monthly_demand(b.batch_id, [])


# ---------------------------------------------------------------- 10. 월별 수요 연결
def test_demand_is_blocked_until_product_is_approved_then_revalidation_unlocks_it(svc):
    d = svc.create_batch("d.csv", DEMAND)
    assert d.status == "HAS_ERRORS"
    assert issues(d) == [("PRODUCT_NOT_REGISTERED", "A2"), ("PRODUCT_NOT_REGISTERED", "A3")]
    with pytest.raises(ApiError) as e:
        approve(svc, d)
    assert code_of(e) == (409, ErrorCode.APPROVAL_BLOCKED_ERRORS)

    pending = upload(svc)                                            # 업로드만 된 제품은 참조할 수 없다
    r = svc.revalidate(d.batch_id, expected_version=d.version, column_overrides={}, default_unit=None, sheet=None)
    assert r.status == "HAS_ERRORS"

    approve(svc, pending)
    r2 = svc.revalidate(d.batch_id, expected_version=r.version, column_overrides={}, default_unit=None, sheet=None)
    assert r2.status == "READY_FOR_APPROVAL"
    assert approve(svc, r2).status == "APPROVED"
    items, total = svc.list_approved_demand(None, None, None, 10, 0)
    assert total == 2 and items[0].product_id == "0001001"


def test_rejected_product_batch_does_not_register(svc):
    b = upload(svc)
    svc.reject(b.batch_id, rejected_by=APPROVER, reason="잘못된 파일")
    assert products(svc) == ([], 0)
    assert svc.create_batch("d.csv", DEMAND).status == "HAS_ERRORS"


def test_demand_uploaded_after_products_is_ready_immediately(svc):
    approve(svc, upload(svc))
    assert svc.create_batch("d.csv", DEMAND).status == "READY_FOR_APPROVAL"


def test_demand_name_or_unit_conflicting_with_master_is_rejected(svc):
    approve(svc, upload(svc))
    b = svc.create_batch("a.csv", (DEMAND_HDR + "0001001,히터 Z,2026-01,1,EA\n").encode())
    assert b.status == "HAS_ERRORS" and ("EXISTING_PRODUCT_CONFLICT", "B2") in issues(b)        # PoC M13: 코드·품명이 다른 제품을 가리킴
    c = svc.create_batch("b.csv", (DEMAND_HDR + "0001001,히터 A,2026-01,1,kg\n").encode())
    assert c.status == "HAS_ERRORS" and ("UNIT_DIMENSION_MISMATCH", "E2") in issues(c)


def test_same_dimension_unit_warns_against_base_unit(svc):
    approve(svc, upload(svc, "제품코드,제품명,단위\nM1,소재,kg\n".encode()))
    b = svc.create_batch("a.csv", (DEMAND_HDR + "M1,소재,2026-01,1000,g\n").encode())
    assert b.status == "READY_FOR_APPROVAL" and ("UNIT_DIFFERS_FROM_BASE", "E2") in issues(b)


def test_demand_for_unregistered_code_suggests_variant_without_merging(svc):
    approve(svc, upload(svc))
    b = svc.create_batch("a.csv", (DEMAND_HDR + "1001,히터 A,2026-01,1,EA\n").encode())
    i = b.validation_result["sheets"][0]["issues"][0]
    assert b.status == "HAS_ERRORS" and i["code"] == "PRODUCT_NOT_REGISTERED" and i["evidence"]["similar_codes"] == ["0001001"]


# ---------------------------------------------------------------- 저장 없는 미리보기
def test_preview_runs_product_rules_but_says_master_was_not_checked():
    r = run_preview("p.csv", GOOD, entity="product")
    assert r.status == "READY_FOR_APPROVAL" and r.master_data_checked is False
    assert r.sheets[0].records[0].product_id == "0001001"
    d = run_preview("d.csv", DEMAND)
    assert d.status == "READY_FOR_APPROVAL" and d.master_data_checked is False       # 제품 대조 없음 → 승인 가능성을 뜻하지 않음


# ---------------------------------------------------------------- API
@pytest.fixture
def client(repo):
    service = BatchService(repo)
    app.dependency_overrides[get_service] = lambda: service
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_api_flow_product_then_demand(client):
    r = client.post("/batches", files={"file": ("p.csv", GOOD)}, data={"entity": "product"})
    assert r.status_code == 201
    p = r.json()
    assert p["entity"] == "product" and p["status"] == "READY_FOR_APPROVAL" and p["validation"]["master_data_checked"] is True
    assert p["validation"]["sheets"][0]["records"][0]["safety_stock_qty"] == "10"
    assert client.get("/products").json()["total"] == 0
    r = client.post(f"/batches/{p['batch_id']}/approve", json={"approved_by": APPROVER, "expected_result_digest": p["result_digest"]})
    assert r.status_code == 200 and r.json()["approved_record_count"] == 2
    page = client.get("/products", params={"q": "히터 B"}).json()
    assert page["total"] == 1 and page["items"][0] == {"product_id": "0001002", "product_name": "히터 B", "base_unit": "EA", "product_type": "SEMI_FINISHED",
                                                      "safety_stock_qty": "0", "source_type": "CLIENT_UPLOAD", "batch_id": p["batch_id"]}
    assert client.get("/products", params={"product_id": "0001001"}).json()["total"] == 1
    d = client.post("/batches", files={"file": ("d.csv", DEMAND)}).json()
    assert d["status"] == "READY_FOR_APPROVAL"
    assert client.post(f"/batches/{d['batch_id']}/approve", json={"approved_by": APPROVER, "expected_result_digest": d["result_digest"]}).status_code == 200
    assert client.get("/demand/monthly").json()["total"] == 2


def test_api_detail_roundtrips_product_records_and_persisted_snapshot(client):
    p = client.post("/batches", files={"file": ("p.csv", GOOD)}, data={"entity": "product"}).json()
    got = client.get(f"/batches/{p['batch_id']}").json()
    assert got["validation"]["sheets"][0]["records"] == p["validation"]["sheets"][0]["records"]
    assert "period" not in got["validation"]["sheets"][0]["records"][0]


def test_preview_api_accepts_product_entity(client):
    r = client.post("/ingest/preview", files={"file": ("p.csv", GOOD)}, data={"entity": "product"})
    assert r.status_code == 200 and r.json()["master_data_checked"] is False
