"""SqlBatchRepository + BatchService 통합 시험 (DB 필요).

⚠ 한 번도 실행·통과한 적 없다. Docker 가 없는 PC 에서는 skip. Docker 개발 PC 에서 마이그레이션 0004 까지 적용 후 실행해 검증한다.
이 시험은 실제 커밋을 하므로 테스트 DB(chungpamo_test)에서만 실행되고, 끝나면 테이블을 TRUNCATE 한다.
"""
from decimal import Decimal

import pytest
from sqlalchemy import text

from app.batches.repository import DemandRow, RepositoryError
from app.batches.service import BatchService
from app.batches.sql_repository import SqlBatchRepository
from app.errors import ApiError, ErrorCode

HDR = "제품코드,제품명,기준월,수요량,단위\n"
PRODUCT_HDR = "제품코드,제품명,단위\n"
GOOD = (HDR + "FG001,히터 A,2026-01,100,EA\nFG001,히터 A,2026-02,120,EA\nFG002,히터 B,2026-01,50,EA\n").encode()


@pytest.fixture
def svc(test_engine):
    _clean(test_engine)
    service = BatchService(SqlBatchRepository(test_engine))
    products = service.create_batch("products.csv", (PRODUCT_HDR + "FG001,히터 A,EA\nFG002,히터 B,EA\n").encode(), entity="product")
    service.approve(products.batch_id, approved_by="SEED-01", expected_result_digest=products.result_digest)
    yield service
    _clean(test_engine)


def _clean(engine):
    with engine.begin() as c:
        c.execute(text("TRUNCATE inventory, bom_line, bom_header, material, monthly_demand, product, batch_source_file, import_batch CASCADE"))


def test_upload_approve_query_roundtrip(svc, test_engine):
    b = svc.create_batch("d.csv", GOOD)
    assert b.status == "READY_FOR_APPROVAL"
    with test_engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM monthly_demand WHERE batch_id = :b"), dict(b=b.batch_id)).scalar_one() == 0
    with pytest.raises(ApiError) as e:
        svc.create_batch("copy.csv", GOOD)
    assert e.value.code == ErrorCode.DUPLICATE_UPLOAD
    a = svc.approve(b.batch_id, approved_by="REVIEWER-01", expected_result_digest=b.result_digest)
    assert a.status == "APPROVED" and a.approved_at is not None and a.approved_record_count == 3
    items, total = svc.list_approved_demand("FG001", None, None, 100, 0)
    assert total == 2 and [i.period for i in items] == ["2026-01", "2026-02"]
    with pytest.raises(ApiError) as e:
        svc.approve(b.batch_id, approved_by="REVIEWER-01", expected_result_digest=b.result_digest)
    assert e.value.code == ErrorCode.ALREADY_APPROVED


def test_revalidate_persists_settings_and_bumps_version(svc):
    b = svc.create_batch("d.csv", "제품코드,기준월,수량,주문수량,단위\nFG001,2026-03,1,2,EA\n".encode())
    assert b.status == "MAPPING_HOLD"
    r = svc.revalidate(b.batch_id, expected_version=b.version, column_overrides={"demand_qty": "주문수량"}, default_unit=None, sheet=None)
    got = svc.get_batch(b.batch_id)
    assert r.status == got.status == "READY_FOR_APPROVAL" and got.version == 2 and got.column_overrides == {"demand_qty": "주문수량"}


def test_product_batch_registers_and_unblocks_demand(svc):
    demand = svc.create_batch("a.csv", (HDR + "ZZ999,신규,2026-05,1,EA\n").encode())
    assert demand.status == "HAS_ERRORS"
    products = svc.create_batch("p.csv", (PRODUCT_HDR + "ZZ999,신규,EA\n").encode(), entity="product")
    a = svc.approve(products.batch_id, approved_by="REVIEWER-01", expected_result_digest=products.result_digest)
    assert a.status == "APPROVED" and a.approved_record_count == 1
    items, total = svc.list_products("ZZ999", None, 10, 0)
    assert total == 1 and items[0].product_type is None and items[0].batch_id == products.batch_id
    assert svc.list_products(None, "히터", 10, 0)[1] == 2
    r = svc.revalidate(demand.batch_id, expected_version=demand.version, column_overrides={}, default_unit=None, sheet=None)
    assert r.status == "READY_FOR_APPROVAL"
    assert svc.approve(r.batch_id, approved_by="REVIEWER-01", expected_result_digest=r.result_digest).status == "APPROVED"


def test_existing_key_blocks_and_leaves_state(svc):
    first = svc.create_batch("a.csv", (HDR + "FG001,히터 A,1990-01,5,EA\n").encode())
    svc.approve(first.batch_id, approved_by="REVIEWER-01", expected_result_digest=first.result_digest)
    clash = svc.create_batch("b.csv", (HDR + "FG001,히터 A,1990-01,6,EA\n").encode())
    with pytest.raises(ApiError) as e:
        svc.approve(clash.batch_id, approved_by="REVIEWER-01", expected_result_digest=clash.result_digest)
    assert e.value.code == ErrorCode.KEYS_ALREADY_APPROVED and svc.get_batch(clash.batch_id).status == "READY_FOR_APPROVAL"


def test_material_bom_inventory_roundtrip(svc):
    def ok(name, data, entity):
        b = svc.create_batch(name, data, entity=entity)
        assert b.status == "READY_FOR_APPROVAL", b.validation_result
        return svc.approve(b.batch_id, approved_by="REVIEWER-01", expected_result_digest=b.result_digest)

    ok("m.csv", "자재코드,자재명,단위\nM001,강판,kg\n".encode(), "material")
    assert svc.list_materials("M001", None, 10, 0)[1] == 1
    bom = ("BOM코드,제품코드,버전,상태,활성,유효시작일,유효종료일,순번,구성유형,구성품코드,소요량,단위,스크랩률\n"
           "B1,FG001,V1,APPROVED,Y,2026-01-01,,1,MATERIAL,M001,2.5,kg,0.05\n"
           "B1,FG001,V1,APPROVED,Y,2026-01-01,,2,PRODUCT,FG002,1,EA,\n").encode()
    assert ok("b.csv", bom, "bom").approved_record_count == 2
    items, total = svc.list_boms("FG001", None, True, 10, 0)
    assert total == 1 and [x.component_id for x in items[0].lines] == ["M001", "FG002"] and items[0].header.valid_to is None
    again = svc.create_batch("b2.csv", bom.replace(b"B1,", b"B2,").replace(b"V1", b"V2"), entity="bom")
    assert again.status == "HAS_ERRORS"                                       # 활성 기간 겹침
    inv = "품목구분,품목코드,기준일,현재고,할당재고,단위\nPRODUCT,FG001,2026-10-31,10,3,EA\nMATERIAL,M001,2026-10-31,5,0,kg\n".encode()
    ok("i.csv", inv, "inventory")
    rows, total = svc.list_inventory("PRODUCT", None, None, 10, 0)
    assert total == 1 and rows[0].on_hand_qty - rows[0].allocated_qty == 7
    clash = svc.create_batch("i2.csv", "품목구분,품목코드,기준일,현재고,단위\nPRODUCT,FG001,2026-10-31,1,EA\n".encode(), entity="inventory")
    with pytest.raises(ApiError) as e:
        svc.approve(clash.batch_id, approved_by="REVIEWER-01", expected_result_digest=clash.result_digest)
    assert e.value.code == ErrorCode.KEYS_ALREADY_APPROVED


def test_rejected_file_can_be_uploaded_again(svc):
    b = svc.create_batch("d.csv", GOOD)
    assert svc.reject(b.batch_id, rejected_by="REVIEWER-01", reason="재검토").status == "REJECTED"
    assert svc.create_batch("again.csv", GOOD).status == "READY_FOR_APPROVAL"


def test_failed_insert_rolls_back_the_whole_approval(svc, test_engine):
    """업무 행 INSERT 가 실패하면 배치의 APPROVED 표시도 함께 롤백되어야 한다 (한 트랜잭션). 없는 제품으로 FK 위반을 만든다."""
    b = svc.create_batch("d.csv", GOOD)
    repo = SqlBatchRepository(test_engine)
    with pytest.raises(RepositoryError):
        with repo.transaction() as tx:
            tx.mark_approved(b.batch_id, "REVIEWER-01", 1)
            tx.insert_monthly_demand(b.batch_id, [DemandRow("NO_SUCH_PRODUCT", "2026-01", Decimal(1), "EA")])
    after = svc.get_batch(b.batch_id)
    assert after.status == "READY_FOR_APPROVAL" and after.approved_by is None
