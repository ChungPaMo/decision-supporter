"""product master as a batch entity

0002 의 제약으로는 제품 마스터를 배치로 승인할 수 없었다 (docs/decisions.md D-008):
  - ck_batch_entity 가 monthly_demand 만 허용
  - verify_approved_record_count 가 monthly_demand 행만 셈 → 제품 배치는 커밋 시 거부됨
  - product.product_type 이 NOT NULL 인데 계약상 선택 필드 → 값이 없으면 지어내야 함
추가로 닫는 허점:
  - 수요 배치가 product 테이블에(또는 그 반대로) 행을 쓸 수 있었음 → 배치 entity 와 대상 테이블 일치 강제
  - 중복 업로드 기준이 sha 뿐이라 한 통합문서의 제품 시트·수요 시트를 각각 올릴 수 없었음 → (entity, sha)

이 마이그레이션은 작성만 했고 어떤 DB 에도 적용·검증하지 않았다 (Docker 개발 PC 에서 검증 필요).

Revision ID: 0003
Revises: 0002
"""
import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

# 배치 entity 와 같은 이름의 테이블에만 쓸 수 있다. 아직 entity 가 없는 테이블(bom_header 등)은 업로드 경로가 없어 이 조건을 적용하지 않는다.
REQUIRE_APPROVED_BATCH = """
CREATE OR REPLACE FUNCTION require_approved_batch() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM import_batch
                 WHERE batch_id = NEW.batch_id AND status = 'APPROVED' AND approved_at = now()
                   AND (TG_TABLE_NAME NOT IN ('product', 'monthly_demand') OR entity = TG_TABLE_NAME)) THEN
    RAISE EXCEPTION 'batch % is not APPROVED in this transaction for table %: business rows can only be written by the approving transaction of a matching entity',
      NEW.batch_id, TG_TABLE_NAME USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$
"""

# entity 분기가 없으면 n 이 NULL 로 남아 대조에 실패한다 (알 수 없는 entity 는 승인되지 않는 쪽으로 닫힘).
VERIFY_RECORD_COUNT = """
CREATE OR REPLACE FUNCTION verify_approved_record_count() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE n bigint;
BEGIN
  IF NEW.entity = 'monthly_demand' THEN
    SELECT count(*) INTO n FROM monthly_demand WHERE batch_id = NEW.batch_id;
  ELSIF NEW.entity = 'product' THEN
    SELECT count(*) INTO n FROM product WHERE batch_id = NEW.batch_id;
  END IF;
  IF n IS DISTINCT FROM NEW.approved_record_count::bigint THEN
    RAISE EXCEPTION 'batch %: approved_record_count % but % rows written', NEW.batch_id, NEW.approved_record_count, n
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NULL;
END $$
"""

OLD_REQUIRE_APPROVED_BATCH = """
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

OLD_VERIFY_RECORD_COUNT = """
CREATE OR REPLACE FUNCTION verify_approved_record_count() RETURNS trigger LANGUAGE plpgsql AS $$
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


def upgrade() -> None:
    op.drop_constraint("ck_batch_entity", "import_batch")
    op.create_check_constraint("ck_batch_entity", "import_batch", "entity IN ('monthly_demand', 'product')")
    op.drop_index("uq_batch_live_sha", table_name="import_batch")
    op.create_index("uq_batch_live_entity_sha", "import_batch", ["entity", "source_sha256"], unique=True,
                    postgresql_where=sa.text("status <> 'REJECTED'"))
    op.alter_column("product", "product_type", existing_type=sa.String(15), nullable=True)
    op.execute(REQUIRE_APPROVED_BATCH)
    op.execute(VERIFY_RECORD_COUNT)


def downgrade() -> None:
    op.execute(OLD_VERIFY_RECORD_COUNT)
    op.execute(OLD_REQUIRE_APPROVED_BATCH)
    # 되돌리면 NOT NULL 이 복원되므로 product_type 이 NULL 인 제품이 있으면 실패한다 (값을 지어내지 않고 멈춘다).
    op.alter_column("product", "product_type", existing_type=sa.String(15), nullable=False)
    op.drop_index("uq_batch_live_entity_sha", table_name="import_batch")
    op.create_index("uq_batch_live_sha", "import_batch", ["source_sha256"], unique=True,
                    postgresql_where=sa.text("status <> 'REJECTED'"))
    # 제품 배치가 있으면 아래 제약 복원이 실패한다 (승인 기록을 임의로 지우지 않는다).
    op.drop_constraint("ck_batch_entity", "import_batch")
    op.create_check_constraint("ck_batch_entity", "import_batch", "entity IN ('monthly_demand')")
