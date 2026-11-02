"""DB 수준 안전장치 시험 (마이그레이션 0003 까지 적용한 DB 필요).

⚠ 이 파일의 어떤 테스트도 아직 실행·통과한 적이 없다. Docker 가 없는 PC 에서는 skip 되며, Docker 개발 PC 에서
`docker compose up -d db` + `alembic upgrade head` 후 실행해 skip 이 0 이고 모두 통과해야 검증된 것이다.
"""
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError


def _batch(conn, status="READY_FOR_APPROVAL", sha=None, errors=0, source_type="SYNTHETIC", entity="monthly_demand"):
    bid = uuid.uuid4()
    conn.execute(text(
        "INSERT INTO import_batch (batch_id, source_type, source_name, source_sha256, status, blocking_error_count, entity) "
        "VALUES (:b, :t, 'f.csv', :s, :st, :e, :en)"),
        dict(b=bid, t=source_type, s=sha or uuid.uuid4().hex * 2, st=status, e=errors, en=entity))
    return bid


def _approve(conn, bid, n=1):
    """READY → APPROVED. approved_at 은 트리거가 now() 로 채운다 (같은 트랜잭션에서만 업무 행 쓰기 가능)."""
    conn.execute(text("UPDATE import_batch SET status = 'APPROVED', approved_by = 'REVIEWER-01', approved_record_count = :n WHERE batch_id = :b"),
                 dict(b=bid, n=n))


def _approved_batch(conn, n=1, entity="monthly_demand"):
    bid = _batch(conn, entity=entity)
    _approve(conn, bid, n)
    return bid


def _product(conn, bid, pid="FG001"):
    conn.execute(text("INSERT INTO product (product_id, product_name, product_type, base_unit, batch_id) "
                      "VALUES (:p, 'Heater', 'FINISHED', 'EA', :b)"), dict(p=pid, b=bid))


def _demand(conn, bid, pid="FG001", period="2026-01"):
    conn.execute(text("INSERT INTO monthly_demand (product_id, period, demand_qty, unit, batch_id) VALUES (:p, :m, 10, 'EA', :b)"),
                 dict(p=pid, m=period, b=bid))


def _fails(conn, match=None, exc=DBAPIError):
    class _Ctx:
        def __enter__(self_):
            self_.sp = conn.begin_nested()
            self_.raises = pytest.raises(exc, match=match)
            return self_.raises.__enter__()

        def __exit__(self_, *a):
            ok = self_.raises.__exit__(*a)
            self_.sp.rollback()
            return ok
    return _Ctx()


def test_unit_seed(conn):
    assert conn.execute(text("SELECT count(*) FROM unit")).scalar_one() == 6


# ---------------------------------------------------------------- 상태 전이 (G1, G2, G6)
def test_new_batch_cannot_start_final(conn):
    for st in ("APPROVED", "REJECTED"):
        with _fails(conn, "cannot start in a final state"):
            _batch(conn, st)


def test_ready_requires_zero_errors_and_has_errors_requires_some(conn):
    with _fails(conn, exc=IntegrityError):
        _batch(conn, "READY_FOR_APPROVAL", errors=1)
    with _fails(conn, exc=IntegrityError):
        _batch(conn, "HAS_ERRORS", errors=0)


def test_only_ready_batch_can_be_approved(conn):
    for st, err in (("MAPPING_HOLD", 0), ("HAS_ERRORS", 2)):
        bid = _batch(conn, st, errors=err)
        with _fails(conn, "only a READY_FOR_APPROVAL batch can be approved"):
            _approve(conn, bid)


def test_final_batches_are_immutable_and_rejected_cannot_become_approved(conn):
    approved = _approved_batch(conn)
    with _fails(conn, "immutable"):
        conn.execute(text("UPDATE import_batch SET status = 'REJECTED', rejected_by = 'R', rejected_at = now(), reject_reason = 'x' WHERE batch_id = :b"), dict(b=approved))
    with _fails(conn, "immutable"):
        conn.execute(text("UPDATE import_batch SET warning_count = 9 WHERE batch_id = :b"), dict(b=approved))
    rejected = _batch(conn)
    conn.execute(text("UPDATE import_batch SET status = 'REJECTED', rejected_by = 'R', rejected_at = now(), reject_reason = 'x' WHERE batch_id = :b"), dict(b=rejected))
    with _fails(conn, "immutable"):
        _approve(conn, rejected)


def test_batch_identity_columns_immutable(conn):
    bid = _batch(conn)
    with _fails(conn, "identity columns are immutable"):
        conn.execute(text("UPDATE import_batch SET source_sha256 = :s WHERE batch_id = :b"), dict(b=bid, s="f" * 64))


def test_reject_requires_metadata(conn):
    bid = _batch(conn)
    with _fails(conn, exc=IntegrityError):
        conn.execute(text("UPDATE import_batch SET status = 'REJECTED' WHERE batch_id = :b"), dict(b=bid))


def test_batches_cannot_be_deleted(conn):
    bid = _batch(conn)
    with _fails(conn, "not allowed"):
        conn.execute(text("DELETE FROM import_batch WHERE batch_id = :b"), dict(b=bid))


# ---------------------------------------------------------------- 업무 행 쓰기 게이트 (G3, G4)
def test_rows_require_batch_approved_in_this_transaction(conn):
    ok = _approved_batch(conn, entity="product")
    _product(conn, ok)                                                            # 승인한 같은 트랜잭션: 통과
    with _fails(conn, "not APPROVED in this transaction"):
        _product(conn, _batch(conn, entity="product"), pid="FG002")               # 승인 전 배치


def test_batch_entity_must_match_target_table(conn):
    demand_batch = _approved_batch(conn)
    with _fails(conn, "matching entity"):
        _product(conn, demand_batch)                                              # 수요 배치로 제품 등록 불가
    product_batch = _approved_batch(conn, entity="product")
    _product(conn, product_batch)
    with _fails(conn, "matching entity"):
        _demand(conn, product_batch)                                              # 제품 배치로 수요 기록 불가


def test_product_type_is_optional(conn):
    bid = _approved_batch(conn, entity="product")
    conn.execute(text("INSERT INTO product (product_id, product_name, base_unit, batch_id) VALUES ('FG009', 'x', 'EA', :b)"), dict(b=bid))
    assert conn.execute(text("SELECT product_type FROM product WHERE product_id = 'FG009'")).scalar_one() is None


def test_business_rows_cannot_be_deleted(conn):
    ok = _approved_batch(conn, entity="product")
    _product(conn, ok)
    with _fails(conn, "not allowed"):
        conn.execute(text("DELETE FROM product WHERE product_id = 'FG001'"))


# ---------------------------------------------------------------- 승인 원자성 (G5)
def test_approved_record_count_is_checked_at_commit_time(conn):
    _product(conn, _approved_batch(conn, entity="product"))
    ok = _approved_batch(conn, n=2)
    _demand(conn, ok)                                                             # 1행만 기록
    with _fails(conn, "approved_record_count 2 but 1 rows written"):
        conn.execute(text("SET CONSTRAINTS trg_batch_record_count IMMEDIATE"))
    _demand(conn, ok, period="2026-02")
    conn.execute(text("SET CONSTRAINTS trg_batch_record_count IMMEDIATE"))        # 2행 → 통과


def test_product_batch_record_count_is_checked_at_commit_time(conn):
    ok = _approved_batch(conn, n=2, entity="product")
    _product(conn, ok)
    with _fails(conn, "approved_record_count 2 but 1 rows written"):
        conn.execute(text("SET CONSTRAINTS trg_batch_record_count IMMEDIATE"))
    _product(conn, ok, pid="FG002")
    conn.execute(text("SET CONSTRAINTS trg_batch_record_count IMMEDIATE"))


def test_approved_count_must_be_positive(conn):
    bid = _batch(conn)
    with _fails(conn, exc=IntegrityError):
        _approve(conn, bid, n=0)


# ---------------------------------------------------------------- 원본 보관 (G7)
def test_source_file_is_append_only_and_size_checked(conn):
    bid = _batch(conn)
    conn.execute(text("INSERT INTO batch_source_file (batch_id, content, size_bytes) VALUES (:b, :c, 3)"), dict(b=bid, c=b"abc"))
    with _fails(conn, "append-only"):
        conn.execute(text("UPDATE batch_source_file SET content = :c, size_bytes = 3 WHERE batch_id = :b"), dict(b=bid, c=b"xyz"))
    with _fails(conn, "append-only"):
        conn.execute(text("DELETE FROM batch_source_file WHERE batch_id = :b"), dict(b=bid))
    other = _batch(conn)
    with _fails(conn, exc=IntegrityError):
        conn.execute(text("INSERT INTO batch_source_file (batch_id, content, size_bytes) VALUES (:b, :c, 99)"), dict(b=other, c=b"abc"))


# ---------------------------------------------------------------- 중복 업로드, 제약, 뷰 (0001)
def test_duplicate_upload_blocked_until_rejected(conn):
    sha = "a" * 64
    first = _batch(conn, "READY_FOR_APPROVAL", sha=sha)
    with _fails(conn, exc=IntegrityError):
        _batch(conn, "MAPPING_HOLD", sha=sha)
    _batch(conn, "MAPPING_HOLD", sha=sha, entity="product")                       # 같은 파일이라도 entity 가 다르면 허용
    conn.execute(text("UPDATE import_batch SET status = 'REJECTED', rejected_by = 'R', rejected_at = now(), reject_reason = 'x' WHERE batch_id = :b"), dict(b=first))
    _batch(conn, "MAPPING_HOLD", sha=sha)


def test_bom_active_versions_cannot_overlap(conn):
    bid = _approved_batch(conn, n=1, entity="product")
    _product(conn, bid)
    ins = ("INSERT INTO bom_header (bom_id, product_id, bom_version, status, is_active, valid_from, valid_to, batch_id) "
           "VALUES (:i, 'FG001', :v, 'APPROVED', true, :f, :t, :b)")
    conn.execute(text(ins), dict(i="B1", v="V1", f="2026-01-01", t="2026-06-30", b=bid))
    conn.execute(text(ins), dict(i="B2", v="V2", f="2026-07-01", t=None, b=bid))
    with _fails(conn, exc=IntegrityError):
        conn.execute(text(ins), dict(i="B3", v="V3", f="2026-05-01", t="2026-08-01", b=bid))


def test_forecast_quantile_order_and_period_format(conn):
    _product(conn, _approved_batch(conn, entity="product"))
    ins = ("INSERT INTO demand_forecast (product_id, target_month, horizon, p50, p80, p90, p50_method, quantile_model_version, trained_until) "
           "VALUES ('FG001', :m, 1, :a, :b, :c, 'median', 'q-0.1', '2026-10-31')")
    conn.execute(text(ins), dict(m="2026-11", a=1, b=2, c=3))
    for bad in (dict(m="2026-11", a=3, b=2, c=1), dict(m="2026-13", a=1, b=2, c=3)):
        with _fails(conn, exc=IntegrityError):
            conn.execute(text(ins), bad)


def test_forecast_input_view_has_only_approved_clean_demand(conn):
    _product(conn, _approved_batch(conn, entity="product"))
    ok = _approved_batch(conn)
    _demand(conn, ok)
    rows = conn.execute(text("SELECT product_id, period, demand_qty, source_type FROM v_forecast_input_monthly_demand")).all()
    assert [(r[0], r[1], int(r[2]), r[3]) for r in rows] == [("FG001", "2026-01", 10, "SYNTHETIC")]


# ---------------------------------------------------------------- 실제 커밋을 가로지르는 시험 (별도 트랜잭션)
def test_rows_cannot_be_added_or_changed_after_the_approving_transaction_commits(test_engine):
    """승인 트랜잭션이 커밋된 뒤 같은 배치에 행을 추가·수정하는 시도를 DB 가 거부해야 한다 (G3)."""
    try:
        with test_engine.begin() as c:                                            # tx1: 배치 생성
            pid_batch, bid = _batch(c, entity="product"), _batch(c)
        with test_engine.begin() as c:                                            # tx2: 승인 + 행 INSERT (커밋 시 행 수 검증)
            _approve(c, pid_batch, 1)
            _approve(c, bid, 1)
            _product(c, pid_batch)
            _demand(c, bid)
        with pytest.raises(DBAPIError, match="not APPROVED in this transaction"):
            with test_engine.begin() as c:                                        # tx3: 승인된 배치에 행 추가
                _demand(c, bid, period="2026-02")
        with pytest.raises(DBAPIError, match="not APPROVED in this transaction"):
            with test_engine.begin() as c:                                        # tx4: 기존 행 수정
                c.execute(text("UPDATE monthly_demand SET demand_qty = 999 WHERE batch_id = :b"), dict(b=bid))
        with pytest.raises(DBAPIError, match="approved_record_count 5 but 0 rows written"):
            with test_engine.begin() as c:                                        # 행 없이 승인만 커밋하려는 시도 → 커밋 시점에 거부
                other = _batch(c)
                _approve(c, other, 5)
    finally:
        with test_engine.begin() as c:                                            # 정리: 테스트 DB 만 (TRUNCATE 는 행 트리거를 타지 않는다)
            c.execute(text("TRUNCATE monthly_demand, product, batch_source_file, import_batch CASCADE"))
