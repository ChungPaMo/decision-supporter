"""DB 수준 안전장치 시험. 전용 PostgreSQL 컨테이너가 없으면 건너뛴다."""
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError


def _batch(conn, status="APPROVED", sha=None, errors=0, source_type="SYNTHETIC"):
    bid = uuid.uuid4()
    approved = ", 'REVIEWER-01', now()" if status == "APPROVED" else ", NULL, NULL"
    conn.execute(text(
        "INSERT INTO import_batch (batch_id, source_type, source_name, source_sha256, status, blocking_error_count, approved_by, approved_at) "
        f"VALUES (:b, :t, 'f.csv', :s, :st, :e{approved})"),
        dict(b=bid, t=source_type, s=sha or uuid.uuid4().hex * 2, st=status, e=errors))
    return bid


def _product(conn, bid, pid="FG001"):
    conn.execute(text("INSERT INTO product (product_id, product_name, product_type, base_unit, batch_id) "
                      "VALUES (:p, 'Heater', 'FINISHED', 'EA', :b)"), dict(p=pid, b=bid))


def test_unit_seed(conn):
    assert conn.execute(text("SELECT count(*) FROM unit")).scalar_one() == 6


def test_rows_require_approved_batch(conn):
    _product(conn, _batch(conn, "APPROVED"))                      # 승인된 배치: 통과
    with pytest.raises(DBAPIError, match="not APPROVED"):
        with conn.begin_nested():
            _product(conn, _batch(conn, "VALIDATED"), pid="FG002")   # 승인 전 배치: 거부


def test_approved_batch_cannot_have_errors(conn):
    with pytest.raises(IntegrityError):
        with conn.begin_nested():
            _batch(conn, "APPROVED", errors=1)


def test_approved_batch_is_immutable(conn):
    bid = _batch(conn, "APPROVED")
    with pytest.raises(DBAPIError, match="immutable"):
        with conn.begin_nested():
            conn.execute(text("UPDATE import_batch SET status = 'REJECTED' WHERE batch_id = :b"), dict(b=bid))


def test_duplicate_upload_blocked_until_rejected(conn):
    sha = "a" * 64
    first = _batch(conn, "VALIDATED", sha=sha)
    with pytest.raises(IntegrityError):
        with conn.begin_nested():
            _batch(conn, "UPLOADED", sha=sha)                     # 같은 파일 재업로드
    conn.execute(text("UPDATE import_batch SET status = 'REJECTED' WHERE batch_id = :b"), dict(b=first))
    _batch(conn, "UPLOADED", sha=sha)                             # 반려 후에는 허용


def test_bom_active_versions_cannot_overlap(conn):
    bid = _batch(conn)
    _product(conn, bid)
    ins = ("INSERT INTO bom_header (bom_id, product_id, bom_version, status, is_active, valid_from, valid_to, batch_id) "
           "VALUES (:i, 'FG001', :v, 'APPROVED', true, :f, :t, :b)")
    conn.execute(text(ins), dict(i="B1", v="V1", f="2026-01-01", t="2026-06-30", b=bid))
    conn.execute(text(ins), dict(i="B2", v="V2", f="2026-07-01", t=None, b=bid))        # 이어지는 기간: 통과
    with pytest.raises(IntegrityError):
        with conn.begin_nested():
            conn.execute(text(ins), dict(i="B3", v="V3", f="2026-05-01", t="2026-08-01", b=bid))   # 겹침: 거부


def test_forecast_quantile_order_and_period_format(conn):
    _product(conn, _batch(conn))
    ins = ("INSERT INTO demand_forecast (product_id, target_month, horizon, p50, p80, p90, p50_method, quantile_model_version, trained_until) "
           "VALUES ('FG001', :m, 1, :a, :b, :c, 'median', 'q-0.1', '2026-10-31')")
    conn.execute(text(ins), dict(m="2026-11", a=1, b=2, c=3))
    for bad in (dict(m="2026-11", a=3, b=2, c=1), dict(m="2026-13", a=1, b=2, c=3)):
        with pytest.raises(IntegrityError):
            with conn.begin_nested():
                conn.execute(text(ins), bad)


def test_forecast_input_view_has_only_approved_clean_demand(conn):
    ok = _batch(conn, "APPROVED")
    _product(conn, ok)
    conn.execute(text("INSERT INTO monthly_demand (product_id, period, demand_qty, unit, batch_id) VALUES ('FG001', '2026-01', 10, 'EA', :b)"), dict(b=ok))
    rows = conn.execute(text("SELECT product_id, period, demand_qty, source_type FROM v_forecast_input_monthly_demand")).all()
    assert [(r[0], r[1], int(r[2]), r[3]) for r in rows] == [("FG001", "2026-01", 10, "SYNTHETIC")]
