"""자재 마스터·BOM·재고 업로드·검증·승인 (DB 없음). InMemoryBatchRepository 는 테스트 전용이며 DB 동작을 증명하지 않는다."""
import datetime as dt
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

APPROVER = "REVIEWER-01"
B_HDR = "BOM코드,제품코드,버전,상태,활성,유효시작일,유효종료일,기준수량,순번,구성유형,구성품코드,소요량,단위,스크랩률\n"
L1 = "B1,FG001,V1,APPROVED,Y,2026-01-01,2026-06-30,1,1,MATERIAL,M001,2.5,kg,0.05\n"
L2 = "B1,FG001,V1,APPROVED,Y,2026-01-01,2026-06-30,1,2,PRODUCT,FG002,1,EA,\n"
BOM = (B_HDR + L1 + L2).encode()
I_HDR = "품목구분,품목코드,기준일,현재고,할당재고,가용재고,단위\n"
INV = (I_HDR + "PRODUCT,FG001,2026-10-31,10,3,7,EA\n자재,M001,2026/10/31,500.5,0,500.5,kg\n").encode()


@pytest.fixture
def repo():
    return InMemoryBatchRepository(products={"FG001": "히터 A", "FG002": "히터 B"}, materials={"M001": ("강판", "kg"), "M002": ("볼트", "EA")})


@pytest.fixture
def svc(repo):
    return BatchService(repo)


def up(svc, data, entity, name="f.csv", **kw):
    return svc.create_batch(name, data, entity=entity, **kw)


def approve(svc, b, by=APPROVER):
    return svc.approve(b.batch_id, approved_by=by, expected_result_digest=b.result_digest)


def issues(b):
    return [(i["code"], i["location"]["cell_ref"] if i["location"] else None) for i in b.validation_result["sheets"][0]["issues"]]


def code_of(e):
    return e.value.status, e.value.code


def bom_csv(*rows):
    return (B_HDR + "".join(r + "\n" for r in rows)).encode()


def line(bom="B1", product="FG001", ver="V1", status="APPROVED", active="Y", vf="2026-01-01", vt="", base="1", no="1", ctype="MATERIAL", comp="M001",
         qty="2", unit="kg", scrap=""):
    return ",".join([bom, product, ver, status, active, vf, vt, base, no, ctype, comp, qty, unit, scrap])


# ---------------------------------------------------------------- 자재 마스터
def test_material_master_uses_the_same_approval_flow(repo, svc):
    b = up(svc, "자재코드,자재명,단위,안전재고\n000101,알루미늄,kg,10\n".encode(), "material")
    assert b.status == "READY_FOR_APPROVAL" and svc.list_materials("000101", None, 10, 0)[1] == 0
    a = approve(svc, b)
    assert a.status == "APPROVED" and a.approved_record_count == 1
    m = svc.list_materials("000101", None, 10, 0)[0][0]
    assert (m.material_id, m.material_name, m.base_unit, str(m.safety_stock_qty)) == ("000101", "알루미늄", "kg", "10")


@pytest.mark.parametrize("row,code,ref", [
    ("M9,자재,상자,", "UNIT_UNKNOWN", "C2"),
    ("M9,,kg,", "NAME_MISSING", "B2"),
    ("M9,자재,kg,-1", "QTY_NEGATIVE", "D2"),
    (",자재,kg,", "CODE_MISSING", "A2"),
])
def test_material_value_errors(svc, row, code, ref):
    b = up(svc, ("자재코드,자재명,단위,안전재고\n" + row + "\n").encode(), "material")
    assert b.status == "HAS_ERRORS" and (code, ref) in issues(b)


def test_material_duplicates_and_existing_conflicts(svc):
    d = up(svc, "자재코드,자재명,단위\nM9,자재,kg\nM9,자재,kg\n".encode(), "material", "a.csv")
    assert ("DUPLICATE_MATERIAL_ID", "A2") in issues(d)
    c = up(svc, "자재코드,자재명,단위\nM001,다른 이름,kg\n".encode(), "material", "b.csv")
    assert ("EXISTING_MATERIAL_CONFLICT", "B2") in issues(c)
    n = up(svc, "자재코드,자재명,단위\nM900,강판,kg\n".encode(), "material", "c.csv")
    assert ("EXISTING_NAME_CONFLICT", "A2") in issues(n)
    s = up(svc, "자재코드,자재명,단위,안전재고\nM001,강판,kg,0\n".encode(), "material", "d.csv")
    assert ("MATERIAL_ALREADY_REGISTERED", "A2") in issues(s)
    assert [x["code"] for x in approval_blockers(s)] == [ErrorCode.EMPTY_BATCH]


def test_product_and_material_code_spaces_are_separate(svc):
    assert up(svc, "자재코드,자재명,단위\nFG001,같은 코드 자재,kg\n".encode(), "material").status == "READY_FOR_APPROVAL"


# ---------------------------------------------------------------- BOM 정상
def test_bom_upload_approve_and_query(repo, svc):
    b = up(svc, BOM, "bom")
    assert b.status == "READY_FOR_APPROVAL" and repo.state.boms == {}             # 승인 전 저장 없음
    a = approve(svc, b)
    assert a.status == "APPROVED" and a.approved_record_count == 2                # 구성품 줄 수
    items, total = svc.list_boms("FG001", None, False, 10, 0)
    h = items[0].header
    assert total == 1 and (h.bom_id, h.bom_version, h.is_active, h.valid_from, h.valid_to) == ("B1", "V1", True, dt.date(2026, 1, 1), dt.date(2026, 6, 30))
    assert [(x.line_no, x.component_type, x.component_id, str(x.qty_per), x.unit, str(x.scrap_rate)) for x in items[0].lines] == [
        (1, "MATERIAL", "M001", "2.5", "kg", "0.05"), (2, "PRODUCT", "FG002", "1", "EA", "0")]


def test_bom_optional_columns_default_without_invention(svc):
    hdr = "BOM코드,제품코드,버전,상태,활성,유효시작일,순번,구성유형,구성품코드,소요량,단위\n"
    b = up(svc, (hdr + "B1,FG001,V1,승인,예,20260101,1,자재,M001,2,kg\n").encode(), "bom")
    assert b.status == "READY_FOR_APPROVAL"
    r = b.validation_result["sheets"][0]["records"][0]
    assert (r["status"], r["is_active"], r["valid_from"], r["valid_to"], r["base_qty"], r["scrap_rate"]) == ("APPROVED", True, "2026-01-01", None, "1", "0")


def test_bom_missing_required_column_is_hold(svc):
    b = up(svc, "BOM코드,제품코드,버전,상태,활성,유효시작일,순번,구성유형,구성품코드,소요량\nB1,FG001,V1,APPROVED,Y,2026-01-01,1,MATERIAL,M001,2\n".encode(), "bom")
    assert b.status == "MAPPING_HOLD" and b.validation_result["sheets"][0]["holds"][0]["field"] == "unit"
    with pytest.raises(ApiError) as e:
        approve(svc, b)
    assert code_of(e) == (409, ErrorCode.APPROVAL_BLOCKED_HOLD)


# ---------------------------------------------------------------- BOM 참조
def test_unregistered_product_and_components_are_blocked(svc):
    b = up(svc, bom_csv(line(product="ZZ9", comp="MX"), line(product="ZZ9", no="2", ctype="PRODUCT", comp="ZZ8", unit="EA")), "bom")
    assert b.status == "HAS_ERRORS"
    got = issues(b)
    assert ("PRODUCT_NOT_REGISTERED", "B2") in got and ("MATERIAL_NOT_REGISTERED", "K2") in got and ("PRODUCT_NOT_REGISTERED", "K3") in got


def test_component_type_decides_which_master_is_checked(svc):
    b = up(svc, bom_csv(line(comp="FG002", unit="EA")), "bom")          # FG002 는 제품이지 자재가 아니다
    assert ("MATERIAL_NOT_REGISTERED", "K2") in issues(b)


def test_material_approved_later_unlocks_bom_after_revalidation(svc):
    b = up(svc, bom_csv(line(comp="M777")), "bom")
    assert b.status == "HAS_ERRORS"
    m = up(svc, "자재코드,자재명,단위\nM777,신규 자재,kg\n".encode(), "material")
    approve(svc, m)
    r = svc.revalidate(b.batch_id, expected_version=b.version, column_overrides={}, default_unit=None, sheet=None)
    assert r.status == "READY_FOR_APPROVAL" and approve(svc, r).status == "APPROVED"


# ---------------------------------------------------------------- BOM 값
@pytest.mark.parametrize("kw,code,ref", [
    (dict(qty="0"), "QTY_NOT_POSITIVE", "L2"),
    (dict(qty="-1"), "QTY_NEGATIVE", "L2"),
    (dict(qty="abc"), "QTY_INVALID", "L2"),
    (dict(qty=""), "QTY_MISSING", "L2"),
    (dict(qty="1.1234567"), "QTY_RANGE", "L2"),
    (dict(scrap="1"), "SCRAP_RATE_RANGE", "N2"),
    (dict(scrap="5"), "SCRAP_RATE_RANGE", "N2"),
    (dict(scrap="-0.1"), "QTY_NEGATIVE", "N2"),
    (dict(scrap="5%"), "QTY_INVALID", "N2"),
    (dict(unit="상자"), "UNIT_UNKNOWN", "M2"),
    (dict(unit=""), "UNIT_MISSING", "M2"),
    (dict(unit="EA"), "UNIT_DIMENSION_MISMATCH", "M2"),
    (dict(no="0"), "BOM_LINE_NO_INVALID", "I2"),
    (dict(no="1.5"), "BOM_LINE_NO_INVALID", "I2"),
    (dict(base="0"), "QTY_NOT_POSITIVE", "H2"),
    (dict(vf="2026-02-30"), "DATE_INVALID", "F2"),
    (dict(vf=""), "DATE_MISSING", "F2"),
    (dict(vf="2026-07-01", vt="2026-06-30"), "BOM_PERIOD_INVALID", "G2"),
    (dict(status="ACTIVE"), "BOM_STATUS_INVALID", "D2"),
    (dict(active="X"), "BOM_ACTIVE_INVALID", "E2"),
    (dict(ctype="부품"), "BOM_COMPONENT_TYPE_INVALID", "J2"),
    (dict(status="DRAFT"), "BOM_ACTIVE_NOT_APPROVED", "E2"),
    (dict(ver="V 1"), "CODE_INVALID", "C2"),
    (dict(ctype="PRODUCT", comp="FG001", unit="EA"), "BOM_SELF_REFERENCE", "K2"),
])
def test_bom_value_errors_with_cell_position(svc, kw, code, ref):
    b = up(svc, bom_csv(line(**kw)), "bom")
    assert b.status == "HAS_ERRORS" and (code, ref) in issues(b)
    assert b.validation_result["sheets"][0]["records"] == []


def test_same_dimension_unit_warns(svc):
    b = up(svc, bom_csv(line(unit="g", qty="500")), "bom")
    assert b.status == "READY_FOR_APPROVAL" and ("UNIT_DIFFERS_FROM_BASE", "M2") in issues(b)


def test_draft_inactive_bom_is_allowed(svc):
    assert up(svc, bom_csv(line(status="DRAFT", active="N")), "bom").status == "READY_FOR_APPROVAL"


# ---------------------------------------------------------------- BOM 구조·버전·기간
def test_header_values_must_agree_across_lines(svc):
    b = up(svc, bom_csv(line(), line(no="2", comp="M002", unit="EA", ver="V2")), "bom")
    assert b.status == "HAS_ERRORS" and ("BOM_HEADER_INCONSISTENT", "C3") in issues(b)


def test_duplicate_line_and_repeated_component(svc):
    d = up(svc, bom_csv(line(), line(comp="M002", unit="EA")), "bom", "a.csv")
    assert {("BOM_LINE_DUPLICATE", "I2"), ("BOM_LINE_DUPLICATE", "I3")} <= set(issues(d))
    w = up(svc, bom_csv(line(), line(no="2")), "bom", "b.csv")
    assert w.status == "READY_FOR_APPROVAL" and ("BOM_COMPONENT_REPEATED", "K2") in issues(w)


def test_same_version_under_two_bom_ids_in_file(svc):
    b = up(svc, bom_csv(line(status="DRAFT", active="N"), line(bom="B2", status="DRAFT", active="N")), "bom")
    assert ("BOM_VERSION_CONFLICT", "C2") in issues(b) and ("BOM_VERSION_CONFLICT", "C3") in issues(b)


def test_active_periods_overlapping_in_file(svc):
    b = up(svc, bom_csv(line(vt="2026-06-30"), line(bom="B2", ver="V2", vf="2026-06-30")), "bom")
    assert b.status == "HAS_ERRORS" and ("BOM_ACTIVE_OVERLAP", "F2") in issues(b) and ("BOM_ACTIVE_OVERLAP", "F3") in issues(b)
    ok = up(svc, bom_csv(line(vt="2026-06-30"), line(bom="B2", ver="V2", vf="2026-07-01")), "bom", "ok.csv")
    assert ok.status == "READY_FOR_APPROVAL"


def test_inactive_version_may_overlap(svc):
    assert up(svc, bom_csv(line(), line(bom="B2", ver="V2", active="N")), "bom").status == "READY_FOR_APPROVAL"


def test_conflicts_with_approved_boms(svc):
    approve(svc, up(svc, bom_csv(line(vt="2026-06-30")), "bom", "a.csv"))
    same_id = up(svc, bom_csv(line(ver="V2", vf="2026-07-01")), "bom", "b.csv")
    assert ("EXISTING_BOM_CONFLICT", "A2") in issues(same_id)
    same_ver = up(svc, bom_csv(line(bom="B2", vf="2026-07-01")), "bom", "c.csv")
    assert ("BOM_VERSION_CONFLICT", "C2") in issues(same_ver)
    overlap = up(svc, bom_csv(line(bom="B3", ver="V3", vf="2026-03-01")), "bom", "d.csv")
    assert ("BOM_ACTIVE_OVERLAP", "F2") in issues(overlap)
    next_version = up(svc, bom_csv(line(bom="B4", ver="V4", vf="2026-07-01")), "bom", "e.csv")
    assert next_version.status == "READY_FOR_APPROVAL" and approve(svc, next_version).status == "APPROVED"
    assert svc.list_boms("FG001", None, True, 10, 0)[1] == 2


def test_bom_cycle_across_batches(svc):
    approve(svc, up(svc, bom_csv(line(product="FG001", ctype="PRODUCT", comp="FG002", unit="EA")), "bom", "a.csv"))
    b = up(svc, bom_csv(line(bom="B2", product="FG002", ctype="PRODUCT", comp="FG001", unit="EA")), "bom", "b.csv")
    assert b.status == "HAS_ERRORS" and any(c == "BOM_CYCLE" for c, _ in issues(b))


def test_other_batch_approving_first_stops_the_waiting_one(svc):
    waiting = up(svc, bom_csv(line()), "bom", "a.csv")
    approve(svc, up(svc, bom_csv(line(bom="B2", ver="V2")), "bom", "b.csv"))
    with pytest.raises(ApiError) as e:
        approve(svc, waiting)
    assert code_of(e) == (409, ErrorCode.RESULT_CHANGED) and svc.list_boms(None, None, False, 10, 0)[1] == 1


# ---------------------------------------------------------------- 재고 정상
def test_inventory_upload_approve_and_query(repo, svc):
    b = up(svc, INV, "inventory")
    assert b.status == "READY_FOR_APPROVAL" and repo.state.inventory == {}
    assert approve(svc, b).approved_record_count == 2
    items, total = svc.list_inventory(None, None, None, 10, 0)
    assert total == 2 and (items[1].item_type, items[1].item_id, items[1].as_of_date, str(items[1].on_hand_qty)) == ("PRODUCT", "FG001", dt.date(2026, 10, 31), "10")


def test_inventory_excel_date_cell_and_text_code(svc):
    wb = openpyxl.Workbook()
    ws = wb.active
    for row in [["품목구분", "품목코드", "기준일", "현재고", "단위"], ["자재", "M001", dt.datetime(2026, 10, 31), 5, "kg"]]:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    b = up(svc, buf.getvalue(), "inventory", "i.xlsx")
    assert b.status == "READY_FOR_APPROVAL" and ("ALLOCATED_DEFAULTED", None) in issues(b)
    assert b.validation_result["sheets"][0]["records"][0]["as_of_date"] == "2026-10-31"


def test_available_is_computed_not_stored(svc):
    b = up(svc, "품목구분,품목코드,기준일,현재고,할당재고,단위\nPRODUCT,FG001,2026-10-31,10,3,EA\n".encode(), "inventory")
    assert b.validation_result["sheets"][0]["records"][0]["available_qty"] == "7"


# ---------------------------------------------------------------- 재고 오류
def inv(row):
    return (I_HDR + row + "\n").encode()


@pytest.mark.parametrize("row,code,ref", [
    ("PRODUCT,FG001,2026-10-31,-1,0,,EA", "QTY_NEGATIVE", "D2"),
    ("PRODUCT,FG001,2026-10-31,5,-1,,EA", "QTY_NEGATIVE", "E2"),
    ("PRODUCT,FG001,2026-10-31,5,6,,EA", "ALLOCATED_EXCEEDS_ON_HAND", "E2"),
    ("PRODUCT,FG001,2026-10-31,5,1,9,EA", "AVAILABLE_MISMATCH", "F2"),
    ("PRODUCT,FG001,2026-10-31,abc,0,,EA", "QTY_INVALID", "D2"),
    ("PRODUCT,FG001,2026-10-31,,0,,EA", "QTY_MISSING", "D2"),
    ("PRODUCT,FG001,2026-02-30,5,0,,EA", "DATE_INVALID", "C2"),
    ("PRODUCT,FG001,,5,0,,EA", "DATE_MISSING", "C2"),
    ("부품,FG001,2026-10-31,5,0,,EA", "ITEM_TYPE_INVALID", "A2"),
    ("PRODUCT,,2026-10-31,5,0,,EA", "CODE_MISSING", "B2"),
    ("PRODUCT,ZZ9,2026-10-31,5,0,,EA", "PRODUCT_NOT_REGISTERED", "B2"),
    ("MATERIAL,FG001,2026-10-31,5,0,,kg", "MATERIAL_NOT_REGISTERED", "B2"),
    ("PRODUCT,FG001,2026-10-31,5,0,,kg", "UNIT_DIMENSION_MISMATCH", "G2"),
    ("PRODUCT,FG001,2026-10-31,5,0,,상자", "UNIT_UNKNOWN", "G2"),
    ("PRODUCT,FG001,2026-10-31,5,0,,", "UNIT_MISSING", "G2"),
])
def test_inventory_errors_with_cell_position(svc, row, code, ref):
    b = up(svc, inv(row), "inventory")
    assert b.status == "HAS_ERRORS" and (code, ref) in issues(b)
    assert b.validation_result["sheets"][0]["records"] == []
    with pytest.raises(ApiError) as e:
        approve(svc, b)
    assert code_of(e) == (409, ErrorCode.APPROVAL_BLOCKED_ERRORS) and svc.list_inventory(None, None, None, 10, 0)[1] == 0


def test_same_dimension_inventory_unit_warns(svc):
    b = up(svc, inv("MATERIAL,M001,2026-10-31,500,0,500,g"), "inventory")
    assert b.status == "READY_FOR_APPROVAL" and ("UNIT_DIFFERS_FROM_BASE", "G2") in issues(b)


def test_duplicate_item_and_date_in_file_even_with_same_values(svc):
    b = up(svc, (I_HDR + "PRODUCT,FG001,2026-10-31,5,0,5,EA\nPRODUCT,FG001,2026/10/31,5,0,5,EA\n").encode(), "inventory")
    assert b.status == "HAS_ERRORS" and {("INVENTORY_DUPLICATE", "B2"), ("INVENTORY_DUPLICATE", "B3")} <= set(issues(b))


def test_same_code_as_product_and_material_is_not_a_duplicate(repo, svc):
    repo.state.materials["FG001"] = repo.state.materials["M001"]
    b = up(svc, (I_HDR + "PRODUCT,FG001,2026-10-31,5,0,5,EA\nMATERIAL,FG001,2026-10-31,5,0,5,kg\n").encode(), "inventory")
    assert not any(c == "INVENTORY_DUPLICATE" for c, _ in issues(b))


def test_key_already_approved_blocks_second_batch(svc):
    approve(svc, up(svc, INV, "inventory", "a.csv"))
    other = up(svc, inv("PRODUCT,FG001,2026-10-31,99,0,99,EA"), "inventory", "b.csv")
    with pytest.raises(ApiError) as e:
        approve(svc, other)
    assert code_of(e) == (409, ErrorCode.KEYS_ALREADY_APPROVED)
    assert e.value.details["keys"] == [{"item_type": "PRODUCT", "item_id": "FG001", "as_of_date": "2026-10-31"}]
    assert svc.get_batch(other.batch_id).status == "READY_FOR_APPROVAL"
    assert str(svc.list_inventory("PRODUCT", None, None, 10, 0)[0][0].on_hand_qty) == "10"    # 덮어쓰기 없음
    later = up(svc, inv("PRODUCT,FG001,2026-11-30,8,0,8,EA"), "inventory", "c.csv")
    assert approve(svc, later).status == "APPROVED"                                            # 다른 기준일은 허용


def test_inventory_missing_required_column_is_hold(svc):
    b = up(svc, "품목구분,품목코드,현재고,단위\nPRODUCT,FG001,5,EA\n".encode(), "inventory")
    assert b.status == "MAPPING_HOLD"


# ---------------------------------------------------------------- 공통 승인 게이트 (BOM·재고·자재)
CASES = [("material", "자재코드,자재명,단위\nM900,신규,kg\nM901,신규2,kg\n".encode(), lambda r: r.state.materials),
         ("bom", BOM, lambda r: r.state.boms),
         ("inventory", INV, lambda r: r.state.inventory)]


@pytest.mark.parametrize("entity,data,table", CASES)
def test_storage_failure_rolls_back_everything(repo, svc, entity, data, table):
    b = up(svc, data, entity)
    before = len(table(repo))
    repo.fail_insert_after = 1                                      # 2행 중 1행 쓴 뒤 실패
    with pytest.raises(ApiError) as e:
        approve(svc, b)
    assert code_of(e) == (500, ErrorCode.PERSISTENCE_ERROR) and repo.rollbacks == 1
    after = svc.get_batch(b.batch_id)
    assert after.status == "READY_FOR_APPROVAL" and after.approved_by is None and len(table(repo)) == before
    repo.fail_insert_after = None
    assert approve(svc, after).status == "APPROVED"


@pytest.mark.parametrize("entity,data,table", CASES)
def test_duplicate_approval_digest_actor_and_finalized(repo, svc, entity, data, table):
    b = up(svc, data, entity)
    with pytest.raises(ApiError) as e:
        svc.approve(b.batch_id, approved_by=APPROVER, expected_result_digest="0" * 64)
    assert code_of(e) == (409, ErrorCode.RESULT_CHANGED)
    with pytest.raises(ApiError) as e:
        svc.approve(b.batch_id, approved_by="홍길동", expected_result_digest=b.result_digest)
    assert code_of(e) == (422, ErrorCode.ACTOR_REQUIRED)
    first = approve(svc, b)
    size = len(table(repo))
    with pytest.raises(ApiError) as e:
        approve(svc, b)
    assert code_of(e) == (409, ErrorCode.ALREADY_APPROVED) and len(table(repo)) == size
    with pytest.raises(ApiError) as e:
        svc.revalidate(b.batch_id, expected_version=first.version, column_overrides={}, default_unit=None, sheet=None)
    assert code_of(e) == (409, ErrorCode.BATCH_FINALIZED)


@pytest.mark.parametrize("entity,data,table", CASES)
def test_rejected_batch_writes_nothing_and_file_is_kept(repo, svc, entity, data, table):
    b = up(svc, data, entity)
    before = len(table(repo))
    svc.reject(b.batch_id, rejected_by=APPROVER, reason="잘못된 파일")
    assert len(table(repo)) == before and repo.state.files[b.batch_id] == data


def test_fake_store_refuses_wrong_entity_writes(repo, svc):
    from app.batches.repository import RepositoryError
    b = up(svc, INV, "inventory")
    with repo.transaction() as tx:
        tx.mark_approved(b.batch_id, APPROVER, 2)
        with pytest.raises(RepositoryError, match="matching entity"):
            tx.insert_materials(b.batch_id, [])
        with pytest.raises(RepositoryError, match="matching entity"):
            tx.insert_bom(b.batch_id, [], [])


def test_overrides_are_checked_per_entity(svc):
    with pytest.raises(ApiError) as e:
        up(svc, BOM, "bom", column_overrides={"demand_qty": "A"})
    assert code_of(e) == (422, ErrorCode.BAD_COLUMN_OVERRIDES)


def test_default_unit_is_ignored_with_warning(svc):
    b = up(svc, INV, "inventory", default_unit="EA")
    assert ("DEFAULT_UNIT_IGNORED", None) in issues(b)


def test_same_bom_file_cannot_be_uploaded_twice(svc):
    up(svc, BOM, "bom")
    with pytest.raises(ApiError) as e:
        up(svc, BOM, "bom", "copy.csv")
    assert code_of(e) == (409, ErrorCode.DUPLICATE_UPLOAD)


# ---------------------------------------------------------------- 기존 기능과의 관계
def test_full_chain_product_material_bom_inventory_demand():
    svc = BatchService(InMemoryBatchRepository())
    approve(svc, up(svc, "제품코드,제품명,단위\nP1,히터,EA\n".encode(), "product", "p.csv"))
    approve(svc, up(svc, "자재코드,자재명,단위\nM1,강판,kg\n".encode(), "material", "m.csv"))
    bom = "BOM코드,제품코드,버전,상태,활성,유효시작일,순번,구성유형,구성품코드,소요량,단위\nB,P1,V1,APPROVED,Y,2026-01-01,1,MATERIAL,M1,2,kg\n".encode()
    approve(svc, up(svc, bom, "bom", "b.csv"))
    approve(svc, up(svc, "품목구분,품목코드,기준일,현재고,단위\nPRODUCT,P1,2026-10-31,1,EA\nMATERIAL,M1,2026-10-31,1,kg\n".encode(), "inventory", "i.csv"))
    approve(svc, up(svc, "제품코드,기준월,수요량,단위\nP1,2026-11,5,EA\n".encode(), "monthly_demand", "d.csv"))
    assert (svc.list_boms(None, None, False, 10, 0)[1], svc.list_inventory(None, None, None, 10, 0)[1], svc.list_approved_demand(None, None, None, 10, 0)[1]) == (1, 2, 1)


def test_preview_does_not_check_master_data():
    r = run_preview("b.csv", bom_csv(line(comp="NOT_REGISTERED")), entity="bom")
    assert r.status == "READY_FOR_APPROVAL" and r.master_data_checked is False
    r2 = run_preview("i.csv", inv("PRODUCT,NOPE,2026-10-31,5,0,5,EA"), entity="inventory")
    assert r2.status == "READY_FOR_APPROVAL" and r2.master_data_checked is False


# ---------------------------------------------------------------- API
@pytest.fixture
def client(repo):
    service = BatchService(repo)
    app.dependency_overrides[get_service] = lambda: service
    yield TestClient(app)
    app.dependency_overrides.clear()


def post_approve(client, b):
    return client.post(f"/batches/{b['batch_id']}/approve", json={"approved_by": APPROVER, "expected_result_digest": b["result_digest"]})


def test_api_bom_flow(client):
    b = client.post("/batches", files={"file": ("b.csv", BOM)}, data={"entity": "bom"}).json()
    assert b["entity"] == "bom" and b["status"] == "READY_FOR_APPROVAL"
    assert b["validation"]["sheets"][0]["records"][0]["qty_per"] == "2.5"
    assert client.get(f"/batches/{b['batch_id']}").json()["validation"]["sheets"][0]["records"] == b["validation"]["sheets"][0]["records"]
    assert client.get("/boms").json()["total"] == 0
    assert post_approve(client, b).status_code == 200
    page = client.get("/boms", params={"product_id": "FG001", "active_only": True}).json()
    assert page["total"] == 1
    item = page["items"][0]
    assert item["valid_to"] == "2026-06-30" and item["lines"][0] == {"line_no": 1, "component_type": "MATERIAL", "component_id": "M001", "qty_per": "2.5",
                                                                    "unit": "kg", "scrap_rate": "0.05"}


def test_api_inventory_flow_and_available_qty(client):
    b = client.post("/batches", files={"file": ("i.csv", INV)}, data={"entity": "inventory"}).json()
    assert post_approve(client, b).status_code == 200
    page = client.get("/inventory", params={"item_type": "PRODUCT", "as_of_date": "2026-10-31"}).json()
    assert page["total"] == 1 and page["items"][0]["available_qty"] == "7" and page["items"][0]["on_hand_qty"] == "10"
    assert client.get("/inventory", params={"item_type": "BAD"}).status_code == 422
    assert client.get("/inventory", params={"as_of_date": "2026-11-31"}).status_code == 422


def test_api_material_flow(client):
    b = client.post("/batches", files={"file": ("m.csv", "자재코드,자재명,단위\nM9,신규,kg\n".encode())}, data={"entity": "material"}).json()
    assert post_approve(client, b).status_code == 200
    assert client.get("/materials", params={"q": "신규"}).json()["items"][0]["material_id"] == "M9"


@pytest.mark.parametrize("path", ["/materials", "/boms", "/inventory"])
def test_new_queries_are_501_without_persistence(path):
    app.dependency_overrides.clear()
    r = TestClient(app).get(path)
    assert r.status_code == 501 and r.json()["detail"]["code"] == "PERSISTENCE_DISABLED"


def test_openapi_lists_new_paths():
    paths = TestClient(app).get("/openapi.json").json()["paths"]
    assert {"/materials", "/boms", "/inventory"} <= set(paths)
