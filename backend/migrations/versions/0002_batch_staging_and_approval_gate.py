"""batch staging columns, source file table, and a stricter approval gate

0001 의 허점 (docs/decisions.md D-007):
  G1 REJECTED 배치를 다시 APPROVED 로 바꿀 수 있었다.
  G2 배치를 처음부터 APPROVED 로 INSERT 해 검증 단계를 건너뛸 수 있었다.
  G3 승인된 배치에 나중에 행을 추가·수정할 수 있었다.
  G4 업무 테이블 행 삭제를 막지 않았다.
  G5 승인 커밋에 행이 실제로 들어갔는지 DB 가 확인하지 않았다.
  G6 상태값에 매핑 보류·오류 구분이 없고 컬럼 길이(12)가 짧았다.
  G7 승인 전 원본·설정·검증 결과를 보관할 곳이 없었다.

이 마이그레이션은 작성만 했고 어떤 DB 에도 적용·검증하지 않았다 (Docker 개발 PC 에서 검증 필요).

Revision ID: 0002
Revises: 0001
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

GOVERNED = ["product", "material", "monthly_demand", "sales_order", "bom_header", "bom_line", "inventory", "production_capacity"]
NEW_STATUSES = "'MAPPING_HOLD', 'HAS_ERRORS', 'READY_FOR_APPROVAL', 'APPROVED', 'REJECTED'"
OLD_STATUSES = "'UPLOADED', 'VALIDATED', 'APPROVED', 'REJECTED'"

# 업무 행은 '승인한 바로 그 트랜잭션' 안에서만 쓸 수 있다: approved_at 은 승인 UPDATE 때 트리거가 now()(=트랜잭션 시작 시각)로 채운다.
REQUIRE_APPROVED_BATCH = """
CREATE OR REPLACE FUNCTION require_approved_batch() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM import_batch
                 WHERE batch_id = NEW.batch_id AND status = 'APPROVED' AND approved_at = now()) THEN
    RAISE EXCEPTION 'batch % is not APPROVED in this transaction: business rows can only be written by the approving transaction', NEW.batch_id
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$
"""

GUARD_BATCH_UPDATE = """
CREATE OR REPLACE FUNCTION lock_approved_batch() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF OLD.status IN ('APPROVED', 'REJECTED') THEN
    RAISE EXCEPTION 'batch % is % and immutable', OLD.batch_id, OLD.status USING ERRCODE = 'check_violation';
  END IF;
  IF NEW.batch_id <> OLD.batch_id OR NEW.source_sha256 <> OLD.source_sha256
     OR NEW.source_type <> OLD.source_type OR NEW.entity <> OLD.entity THEN
    RAISE EXCEPTION 'batch % identity columns are immutable', OLD.batch_id USING ERRCODE = 'check_violation';
  END IF;
  IF NEW.status = 'APPROVED' THEN
    IF OLD.status <> 'READY_FOR_APPROVAL' THEN
      RAISE EXCEPTION 'only a READY_FOR_APPROVAL batch can be approved (was %)', OLD.status USING ERRCODE = 'check_violation';
    END IF;
    NEW.approved_at := now();
  END IF;
  RETURN NEW;
END $$
"""

CHECK_NEW_BATCH = """
CREATE FUNCTION check_new_batch() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.status IN ('APPROVED', 'REJECTED') THEN
    RAISE EXCEPTION 'a new batch cannot start in final state %', NEW.status USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$
"""

FORBID_CHANGE = """
CREATE FUNCTION forbid_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION '% on % is not allowed (append-only)', TG_OP, TG_TABLE_NAME USING ERRCODE = 'check_violation';
END $$
"""

# 승인 커밋 시점(DEFERRED)에 '승인된 행 수 == 실제로 들어간 행 수' 를 DB 가 확인 → 승인만 되고 행이 빠지는 부분 커밋 차단.
VERIFY_RECORD_COUNT = """
CREATE FUNCTION verify_approved_record_count() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE n bigint;
BEGIN
  IF NEW.entity = 'monthly_demand' THEN
    SELECT count(*) INTO n FROM monthly_demand WHERE batch_id = NEW.batch_id;
  END IF;
  IF n IS DISTINCT FROM NEW.approved_record_count::bigint THEN
    RAISE EXCEPTION 'batch %: approved_record_count % but % rows written', NEW.batch_id, NEW.approved_record_count, n
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NULL;
END $$
"""

FORECAST_INPUT_VIEW = """
CREATE OR REPLACE VIEW v_forecast_input_monthly_demand AS
SELECT d.product_id, d.period, d.demand_qty, d.unit, b.source_type, b.batch_id, b.approved_at
FROM monthly_demand d JOIN import_batch b ON b.batch_id = d.batch_id
WHERE b.status = 'APPROVED' AND b.blocking_error_count = 0 AND b.entity = 'monthly_demand'
"""


def upgrade() -> None:
    # --- 컬럼 (G6, G7) ---
    op.alter_column("import_batch", "status", type_=sa.String(20), server_default="MAPPING_HOLD")
    op.add_column("import_batch", sa.Column("entity", sa.String(30), nullable=False, server_default="monthly_demand"))
    op.add_column("import_batch", sa.Column("created_by", sa.String(60)))
    op.add_column("import_batch", sa.Column("default_unit", sa.String(10)))
    op.add_column("import_batch", sa.Column("column_overrides", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")))
    op.add_column("import_batch", sa.Column("sheet_filter", sa.String(260)))
    op.add_column("import_batch", sa.Column("warning_count", sa.Integer, nullable=False, server_default="0"))
    op.add_column("import_batch", sa.Column("result_digest", sa.String(64)))
    op.add_column("import_batch", sa.Column("validation_result", postgresql.JSONB))
    op.add_column("import_batch", sa.Column("version", sa.Integer, nullable=False, server_default="1"))
    op.add_column("import_batch", sa.Column("approved_record_count", sa.Integer))
    op.add_column("import_batch", sa.Column("rejected_by", sa.String(60)))
    op.add_column("import_batch", sa.Column("rejected_at", sa.DateTime(timezone=True)))
    op.add_column("import_batch", sa.Column("reject_reason", sa.String(500)))

    # --- 기존 행 이전 (0001 에는 UPLOADED/VALIDATED 만 있었다. 가장 보수적으로: 승인 가능 상태로 승격하지 않는다) ---
    op.drop_constraint("ck_status_batchstatus", "import_batch")
    op.execute("UPDATE import_batch SET status = 'MAPPING_HOLD' WHERE status = 'UPLOADED'")
    op.execute("UPDATE import_batch SET status = 'HAS_ERRORS' WHERE status = 'VALIDATED' AND blocking_error_count > 0")
    op.execute("UPDATE import_batch SET status = 'MAPPING_HOLD' WHERE status = 'VALIDATED'")
    op.execute("UPDATE import_batch SET approved_record_count = (SELECT count(*) FROM monthly_demand m WHERE m.batch_id = import_batch.batch_id) "
               "WHERE status = 'APPROVED'")
    op.create_check_constraint("ck_status_batchstatus", "import_batch", f"status IN ({NEW_STATUSES})")
    op.create_check_constraint("ck_batch_approved_count", "import_batch", "status <> 'APPROVED' OR approved_record_count > 0")
    op.create_check_constraint("ck_batch_ready_no_errors", "import_batch", "status <> 'READY_FOR_APPROVAL' OR blocking_error_count = 0")
    op.create_check_constraint("ck_batch_errors_counted", "import_batch", "status <> 'HAS_ERRORS' OR blocking_error_count > 0")
    op.create_check_constraint("ck_batch_rejected_meta", "import_batch",
                               "status <> 'REJECTED' OR (rejected_by IS NOT NULL AND rejected_at IS NOT NULL AND reject_reason IS NOT NULL)")
    op.create_check_constraint("ck_batch_entity", "import_batch", "entity IN ('monthly_demand')")

    op.create_table(
        "batch_source_file",
        sa.Column("batch_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("import_batch.batch_id"), primary_key=True),
        sa.Column("content", sa.LargeBinary, nullable=False),
        sa.Column("size_bytes", sa.Integer, nullable=False),
        sa.CheckConstraint("size_bytes = octet_length(content) AND size_bytes > 0", name="ck_source_file_size"),
    )

    # --- 게이트 (G1~G5) ---
    op.execute(REQUIRE_APPROVED_BATCH)
    op.execute(GUARD_BATCH_UPDATE)
    op.execute(CHECK_NEW_BATCH)
    op.execute(FORBID_CHANGE)
    op.execute(VERIFY_RECORD_COUNT)
    op.execute("CREATE TRIGGER trg_batch_new BEFORE INSERT ON import_batch FOR EACH ROW EXECUTE FUNCTION check_new_batch()")
    op.execute("CREATE TRIGGER trg_batch_no_delete BEFORE DELETE ON import_batch FOR EACH ROW EXECUTE FUNCTION forbid_change()")
    op.execute("CREATE TRIGGER trg_source_file_append_only BEFORE UPDATE OR DELETE ON batch_source_file "
               "FOR EACH ROW EXECUTE FUNCTION forbid_change()")
    for t in GOVERNED:
        op.execute(f"CREATE TRIGGER trg_{t}_no_delete BEFORE DELETE ON {t} FOR EACH ROW EXECUTE FUNCTION forbid_change()")
    op.execute("CREATE CONSTRAINT TRIGGER trg_batch_record_count AFTER UPDATE ON import_batch DEFERRABLE INITIALLY DEFERRED "
               "FOR EACH ROW WHEN (OLD.status IS DISTINCT FROM 'APPROVED' AND NEW.status = 'APPROVED') "
               "EXECUTE FUNCTION verify_approved_record_count()")
    op.execute(FORECAST_INPUT_VIEW)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_batch_record_count ON import_batch")
    for t in GOVERNED:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{t}_no_delete ON {t}")
    op.execute("DROP TRIGGER IF EXISTS trg_source_file_append_only ON batch_source_file")
    op.execute("DROP TRIGGER IF EXISTS trg_batch_no_delete ON import_batch")
    op.execute("DROP TRIGGER IF EXISTS trg_batch_new ON import_batch")
    op.execute("DROP FUNCTION IF EXISTS verify_approved_record_count()")
    op.execute("DROP FUNCTION IF EXISTS forbid_change()")
    op.execute("DROP FUNCTION IF EXISTS check_new_batch()")
    op.drop_table("batch_source_file")
    # 0001 의 함수 본문으로 복원
    op.execute("""
CREATE OR REPLACE FUNCTION require_approved_batch() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM import_batch WHERE batch_id = NEW.batch_id AND status = 'APPROVED') THEN
    RAISE EXCEPTION 'batch % is not APPROVED: business rows require an approved batch', NEW.batch_id
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$""")
    op.execute("""
CREATE OR REPLACE FUNCTION lock_approved_batch() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF OLD.status = 'APPROVED' AND (NEW.status <> OLD.status OR NEW.source_type <> OLD.source_type
                                  OR NEW.source_sha256 <> OLD.source_sha256) THEN
    RAISE EXCEPTION 'batch % is APPROVED and immutable', OLD.batch_id USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$""")
    op.execute("""
CREATE OR REPLACE VIEW v_forecast_input_monthly_demand AS
SELECT d.product_id, d.period, d.demand_qty, d.unit, b.source_type, b.batch_id
FROM monthly_demand d JOIN import_batch b ON b.batch_id = d.batch_id
WHERE b.status = 'APPROVED' AND b.blocking_error_count = 0""")
    for c in ("ck_batch_entity", "ck_batch_rejected_meta", "ck_batch_errors_counted", "ck_batch_ready_no_errors", "ck_batch_approved_count", "ck_status_batchstatus"):
        op.drop_constraint(c, "import_batch")
    op.execute("UPDATE import_batch SET status = 'UPLOADED' WHERE status IN ('MAPPING_HOLD', 'HAS_ERRORS')")
    op.execute("UPDATE import_batch SET status = 'VALIDATED' WHERE status = 'READY_FOR_APPROVAL'")
    op.create_check_constraint("ck_status_batchstatus", "import_batch", f"status IN ({OLD_STATUSES})")
    for c in ("reject_reason", "rejected_at", "rejected_by", "approved_record_count", "version", "validation_result", "result_digest",
              "warning_count", "sheet_filter", "column_overrides", "default_unit", "created_by", "entity"):
        op.drop_column("import_batch", c)
    op.alter_column("import_batch", "status", type_=sa.String(12), server_default="UPLOADED")
