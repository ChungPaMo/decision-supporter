"""승인 서비스 단위 테스트. DB 없이 InMemoryBatchRepository(tests/fakes.py)로 서비스 로직만 검증한다.
실제 DB 트랜잭션·트리거·마이그레이션 동작은 증명하지 않는다 (tests/test_db_rules.py 가 Docker 개발 PC 에서 검증)."""
import pytest

from app.batches.repository import RepositoryError
from app.batches.service import BatchService, approval_blockers
from app.errors import ApiError, ErrorCode
from tests.fakes import InMemoryBatchRepository

HDR = "제품코드,제품명,기준월,수요량,단위\n"
GOOD = (HDR + "FG001,히터 A,2026-01,100,EA\nFG001,히터 A,2026-02,120,EA\nFG002,히터 B,2026-01,50,EA\n").encode()
BAD = (HDR + "FG001,히터 A,2026-01,abc,EA\n").encode()
HOLD = "제품코드,제품명,수량,주문수량,단위\nFG001,히터 A,1,2,EA\n".encode()
APPROVER = "REVIEWER-01"


@pytest.fixture
def repo():
    return InMemoryBatchRepository(products={"FG001": "히터 A", "FG002": "히터 B"})


@pytest.fixture
def svc(repo):
    return BatchService(repo)


def code_of(exc_info) -> tuple[int, ErrorCode]:
    return exc_info.value.status, exc_info.value.code


def approve(svc, b, by=APPROVER):
    return svc.approve(b.batch_id, approved_by=by, expected_result_digest=b.result_digest)


def demand(svc):
    return svc.list_approved_demand(None, None, None, 1000, 0)


# ---------------------------------------------------------------- 업로드·임시 보관
def test_create_stores_original_bytes_and_validation_without_touching_business_tables(svc, repo):
    b = svc.create_batch("d.csv", GOOD)
    assert b.status == "READY_FOR_APPROVAL" and b.blocking_error_count == 0 and b.version == 1
    assert repo.state.files[b.batch_id] == GOOD                              # 원본 바이트 그대로
    assert b.source_sha256 and len(b.result_digest) == 64
    assert repo.state.demand == {}                                           # 승인 전에는 업무 데이터 없음
    assert demand(svc) == ([], 0)
    assert approval_blockers(b) == []


@pytest.mark.parametrize("data,status,blocker", [
    (BAD, "HAS_ERRORS", ErrorCode.APPROVAL_BLOCKED_ERRORS),
    (HOLD, "MAPPING_HOLD", ErrorCode.APPROVAL_BLOCKED_HOLD),
    ((HDR).encode(), "READY_FOR_APPROVAL", ErrorCode.EMPTY_BATCH),
])
def test_status_and_blockers_follow_validation(svc, data, status, blocker):
    b = svc.create_batch("d.csv", data)
    assert b.status == status
    assert [x["code"] for x in approval_blockers(b)] == [blocker]


def test_duplicate_upload_rejected_until_previous_is_rejected(svc):
    b = svc.create_batch("d.csv", GOOD)
    with pytest.raises(ApiError) as e:
        svc.create_batch("copy.csv", GOOD)
    assert code_of(e) == (409, ErrorCode.DUPLICATE_UPLOAD) and e.value.details["batch_id"] == str(b.batch_id)
    svc.reject(b.batch_id, rejected_by=APPROVER, reason="재업로드")
    assert svc.create_batch("copy.csv", GOOD).status == "READY_FOR_APPROVAL"


def test_source_note_required_for_public_or_synthetic(svc):
    with pytest.raises(ApiError) as e:
        svc.create_batch("d.csv", GOOD, source_type="PUBLIC_DEMAND")
    assert code_of(e) == (422, ErrorCode.SOURCE_NOTE_REQUIRED)
    assert svc.create_batch("d.csv", GOOD, source_type="PUBLIC_DEMAND", source_note="공공데이터포털 / 이용허락범위 제한 없음").source_type == "PUBLIC_DEMAND"


def test_file_errors_become_api_errors(svc):
    with pytest.raises(ApiError) as e:
        svc.create_batch("d.txt", b"x")
    assert code_of(e) == (415, ErrorCode.UNSUPPORTED_TYPE)


# ---------------------------------------------------------------- 정상 승인
def test_approve_writes_rows_and_exposes_them_to_forecast_input(svc, repo):
    b = svc.create_batch("d.csv", GOOD)
    a = approve(svc, b)
    assert a.status == "APPROVED" and a.approved_by == APPROVER and a.approved_at and a.approved_record_count == 3
    items, total = demand(svc)
    assert total == 3 and [(i.product_id, i.period, str(i.demand_qty)) for i in items] == [
        ("FG001", "2026-01", "100"), ("FG001", "2026-02", "120"), ("FG002", "2026-01", "50")]
    assert items[0].batch_id == b.batch_id and items[0].source_type == "CLIENT_UPLOAD"
    assert repo.rollbacks == 0


# ---------------------------------------------------------------- 승인 거부
def test_approval_refused_for_errors_hold_and_nothing_is_written(svc, repo):
    for data, code in ((BAD, ErrorCode.APPROVAL_BLOCKED_ERRORS), (HOLD, ErrorCode.APPROVAL_BLOCKED_HOLD)):
        b = svc.create_batch("d.csv", data)
        with pytest.raises(ApiError) as e:
            approve(svc, b)
        assert code_of(e) == (409, code)
        assert svc.get_batch(b.batch_id).status == b.status                  # 상태 불변
    assert demand(svc) == ([], 0)


@pytest.mark.parametrize("who", [None, "", "  ", "홍길동", "A B", "x", "a" * 61])
def test_approver_identity_required(svc, who):
    b = svc.create_batch("d.csv", GOOD)
    with pytest.raises(ApiError) as e:
        svc.approve(b.batch_id, approved_by=who, expected_result_digest=b.result_digest)
    assert code_of(e) == (422, ErrorCode.ACTOR_REQUIRED)
    assert svc.get_batch(b.batch_id).status == "READY_FOR_APPROVAL" and demand(svc) == ([], 0)


def test_approval_bound_to_the_result_the_reviewer_saw(svc):
    b = svc.create_batch("d.csv", GOOD)
    with pytest.raises(ApiError) as e:
        svc.approve(b.batch_id, approved_by=APPROVER, expected_result_digest="0" * 64)
    assert code_of(e) == (409, ErrorCode.RESULT_CHANGED)
    assert svc.get_batch(b.batch_id).status == "READY_FOR_APPROVAL"


def test_tampered_summary_does_not_bypass_recomputation(svc, repo):
    """저장된 요약/해시가 변조돼도 승인은 원본을 다시 검증하므로 통과하지 못한다."""
    b = svc.create_batch("d.csv", BAD)
    stored = repo.state.batches[b.batch_id]
    stored.status, stored.blocking_error_count = "READY_FOR_APPROVAL", 0          # 상태·요약을 위조
    stored.validation_result["sheets"][0]["valid_records"] = 5
    with pytest.raises(ApiError) as e:
        approve(svc, svc.get_batch(b.batch_id))
    assert code_of(e) == (409, ErrorCode.RESULT_CHANGED)
    assert demand(svc) == ([], 0) and repo.state.batches[b.batch_id].status == "READY_FOR_APPROVAL"


def test_empty_batch_cannot_be_approved(svc):
    b = svc.create_batch("d.csv", HDR.encode())
    with pytest.raises(ApiError) as e:
        approve(svc, b)
    assert code_of(e) == (409, ErrorCode.EMPTY_BATCH)


def test_unknown_products_are_validation_errors_with_cell_position(svc, repo):
    del repo.state.products["FG002"]
    b = svc.create_batch("d.csv", GOOD)
    assert b.status == "HAS_ERRORS"
    issue = b.validation_result["sheets"][0]["issues"][0]
    assert (issue["code"], issue["location"]["cell_ref"]) == ("PRODUCT_NOT_REGISTERED", "A4")
    with pytest.raises(ApiError) as e:
        approve(svc, b)
    assert code_of(e) == (409, ErrorCode.APPROVAL_BLOCKED_ERRORS) and demand(svc) == ([], 0)


def test_already_approved_keys_block_second_batch(svc):
    approve(svc, svc.create_batch("a.csv", GOOD))
    other = svc.create_batch("b.csv", (HDR + "FG001,히터 A,2026-01,999,EA\nFG001,히터 A,2026-03,1,EA\n").encode())
    with pytest.raises(ApiError) as e:
        approve(svc, other)
    assert code_of(e) == (409, ErrorCode.KEYS_ALREADY_APPROVED) and e.value.details["keys"] == [{"product_id": "FG001", "period": "2026-01"}]
    assert demand(svc)[1] == 3                                                # 첫 승인 데이터만 존재, 덮어쓰기 없음


# ---------------------------------------------------------------- 중복 승인·승인 후 변경
def test_duplicate_approval_does_not_write_again(svc, repo):
    b = svc.create_batch("d.csv", GOOD)
    first = approve(svc, b)
    for _ in range(2):
        with pytest.raises(ApiError) as e:
            approve(svc, b)
        assert code_of(e) == (409, ErrorCode.ALREADY_APPROVED) and e.value.details["approved_by"] == APPROVER
    after = svc.get_batch(b.batch_id)
    assert after.approved_at == first.approved_at and after.version == first.version and demand(svc)[1] == 3


def test_no_changes_after_approval_or_rejection(svc):
    done = approve(svc, svc.create_batch("a.csv", GOOD))
    for call in (lambda: svc.revalidate(done.batch_id, expected_version=done.version, column_overrides={}, default_unit=None, sheet=None),
                 ):
        with pytest.raises(ApiError) as e:
            call()
        assert code_of(e) == (409, ErrorCode.BATCH_FINALIZED)
    with pytest.raises(ApiError) as e:
        svc.reject(done.batch_id, rejected_by=APPROVER, reason="늦은 반려")
    assert code_of(e) == (409, ErrorCode.ALREADY_APPROVED)

    rej = svc.reject(svc.create_batch("b.csv", BAD).batch_id, rejected_by=APPROVER, reason="오류 많음")
    assert rej.status == "REJECTED" and rej.reject_reason == "오류 많음"
    with pytest.raises(ApiError) as e:
        svc.revalidate(rej.batch_id, expected_version=rej.version, column_overrides={}, default_unit=None, sheet=None)
    assert code_of(e) == (409, ErrorCode.BATCH_FINALIZED)
    with pytest.raises(ApiError) as e:
        svc.approve(rej.batch_id, approved_by=APPROVER, expected_result_digest=rej.result_digest)
    assert code_of(e) == (409, ErrorCode.BATCH_REJECTED)


def test_store_level_guards_mirror_db_triggers(repo, svc):
    """Fake 가 DB 트리거처럼 최종 상태 변경을 거부하는지 (서비스가 우회해도 저장소가 막는 구조의 모형)."""
    done = approve(svc, svc.create_batch("a.csv", GOOD))
    with repo.transaction() as tx:
        with pytest.raises(RepositoryError):
            tx.mark_rejected(done.batch_id, APPROVER, "x")
        with pytest.raises(RepositoryError):
            tx.insert_monthly_demand(done.batch_id, [])        # 승인한 트랜잭션이 아니면 업무 행 쓰기 불가


# ---------------------------------------------------------------- 저장 실패·롤백
def test_storage_failure_rolls_back_and_leaves_batch_approvable(svc, repo):
    b = svc.create_batch("d.csv", GOOD)
    repo.fail_insert_after = 1                                   # 3행 중 1행 쓴 뒤 실패
    with pytest.raises(ApiError) as e:
        approve(svc, b)
    assert code_of(e) == (500, ErrorCode.PERSISTENCE_ERROR)
    assert repo.rollbacks == 1
    after = svc.get_batch(b.batch_id)
    assert after.status == "READY_FOR_APPROVAL" and after.approved_by is None and after.approved_at is None   # 승인 표시도 롤백
    assert repo.state.demand == {} and demand(svc) == ([], 0)
    repo.fail_insert_after = None
    assert approve(svc, after).status == "APPROVED" and demand(svc)[1] == 3         # 이후 정상 승인 가능


def test_db_unavailable_maps_to_503(svc, repo):
    repo.unavailable = True
    with pytest.raises(ApiError) as e:
        svc.get_batch(__import__("uuid").uuid4())
    assert code_of(e) == (503, ErrorCode.DB_UNAVAILABLE)


# ---------------------------------------------------------------- 사용자 수정 → 재검증
def test_remapping_then_revalidation_unlocks_approval_and_keeps_original(svc, repo):
    data = "제품코드,제품명,기준월,수량,주문수량,단위\nFG001,히터 A,2026-01,5,7,EA\n".encode()
    b = svc.create_batch("d.csv", data)
    assert b.status == "MAPPING_HOLD" and b.validation_result["sheets"][0]["holds"][0]["candidates"] == ["수량", "주문수량"]
    ok = svc.revalidate(b.batch_id, expected_version=b.version, column_overrides={"demand_qty": "주문수량"}, default_unit=None, sheet=None)
    assert ok.status == "READY_FOR_APPROVAL" and ok.version == 2 and ok.result_digest != b.result_digest
    assert ok.column_overrides == {"demand_qty": "주문수량"}
    assert repo.state.files[b.batch_id] == data                                # 원본 바이트 불변
    assert approve(svc, ok).status == "APPROVED" and str(demand(svc)[0][0].demand_qty) == "7"


def test_revalidation_can_move_a_batch_back_to_errors(svc):
    data = "제품코드,기준월,수량,주문수량,단위\nFG001,2026-01,5,abc,EA\n".encode()
    b = svc.create_batch("d.csv", data)
    r = svc.revalidate(b.batch_id, expected_version=1, column_overrides={"demand_qty": "주문수량"}, default_unit=None, sheet=None)
    assert r.status == "HAS_ERRORS" and r.blocking_error_count == 1
    assert [x["code"] for x in approval_blockers(r)] == [ErrorCode.APPROVAL_BLOCKED_ERRORS]


def test_revalidate_with_default_unit_and_digest_changes_with_settings(svc):
    data = "제품코드,기준월,수요량\nFG001,2026-01,10\n".encode()
    b = svc.create_batch("d.csv", data)
    assert b.status == "HAS_ERRORS"
    r = svc.revalidate(b.batch_id, expected_version=1, column_overrides={}, default_unit="개", sheet=None)
    assert r.status == "READY_FOR_APPROVAL" and r.default_unit == "개" and r.warning_count == 1


def test_stale_version_and_bad_overrides_rejected(svc):
    b = svc.create_batch("d.csv", HOLD)
    svc.revalidate(b.batch_id, expected_version=1, column_overrides={"demand_qty": "주문수량"}, default_unit=None, sheet=None)
    with pytest.raises(ApiError) as e:
        svc.revalidate(b.batch_id, expected_version=1, column_overrides={}, default_unit=None, sheet=None)
    assert code_of(e) == (409, ErrorCode.VERSION_CONFLICT) and e.value.details["current_version"] == 2
    with pytest.raises(ApiError) as e:
        svc.revalidate(b.batch_id, expected_version=2, column_overrides={"nope": "A"}, default_unit=None, sheet=None)
    assert code_of(e) == (422, ErrorCode.BAD_COLUMN_OVERRIDES)


def test_old_digest_is_refused_after_revalidation(svc):
    b = svc.create_batch("d.csv", "제품코드,기준월,수요량\nFG001,2026-01,10\n".encode())
    r = svc.revalidate(b.batch_id, expected_version=1, column_overrides={}, default_unit="EA", sheet=None)
    with pytest.raises(ApiError) as e:
        svc.approve(r.batch_id, approved_by=APPROVER, expected_result_digest=b.result_digest)
    assert code_of(e) == (409, ErrorCode.RESULT_CHANGED)


# ---------------------------------------------------------------- 반려
def test_reject_requires_actor_and_reason(svc):
    b = svc.create_batch("d.csv", GOOD)
    with pytest.raises(ApiError) as e:
        svc.reject(b.batch_id, rejected_by="홍길동", reason="x")
    assert code_of(e) == (422, ErrorCode.ACTOR_REQUIRED)
    with pytest.raises(ApiError) as e:
        svc.reject(b.batch_id, rejected_by=APPROVER, reason=" ")
    assert code_of(e) == (422, ErrorCode.REJECT_REASON_REQUIRED)
    svc.reject(b.batch_id, rejected_by=APPROVER, reason="잘못된 파일")
    with pytest.raises(ApiError) as e:
        svc.reject(b.batch_id, rejected_by=APPROVER, reason="again")
    assert code_of(e) == (409, ErrorCode.BATCH_REJECTED)


# ---------------------------------------------------------------- 승인되지 않은 데이터의 예측 입력 차단
def test_unapproved_batches_never_reach_forecast_input(svc):
    svc.create_batch("ready.csv", GOOD)
    svc.create_batch("err.csv", BAD)
    svc.create_batch("hold.csv", HOLD)
    rejected = svc.create_batch("rej.csv", (HDR + "FG002,히터 B,2026-05,1,EA\n").encode())
    svc.reject(rejected.batch_id, rejected_by=APPROVER, reason="x")
    assert demand(svc) == ([], 0)
    assert svc.list_batches(None, 50, 0)[1] == 4
    assert svc.list_batches("REJECTED", 50, 0)[1] == 1


def test_forecast_input_view_sql_filters_unapproved_and_errors():
    """DB 없이 확인 가능한 정적 검사: 뷰 정의가 승인·오류 0 조건을 가진다 (실제 뷰 동작은 DB 에서 검증)."""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "migrations" / "versions" / "0002_batch_staging_and_approval_gate.py").read_text(encoding="utf-8")
    view = src.split('FORECAST_INPUT_VIEW = """')[1].split('"""')[0]
    assert "b.status = 'APPROVED'" in view and "b.blocking_error_count = 0" in view
