"""material, BOM and inventory as batch entities

0003 까지의 제약으로는 자재·BOM·재고를 배치로 승인할 수 없었다 (docs/decisions.md D-009):
  - ck_batch_entity 가 monthly_demand, product 만 허용
  - verify_approved_record_count 가 두 entity 만 셈 → 새 entity 는 커밋 시 거부됨
추가로 닫는 허점:
  - material, bom_header, bom_line, inventory 는 entity 일치 검사 대상이 아니었음 → 다른 entity 배치가 쓸 수 있었음
  - inventory.item_id 는 조건부 참조라 FK 가 없어 미등록 품목이 들어갈 수 있었음 → 트리거로 존재 확인

BOM 의 approved_record_count 는 구성품 줄(bom_line) 수다.
sales_order, production_capacity 는 아직 업로드 경로가 없어 entity 일치 검사 대상이 아니다.

이 마이그레이션은 작성만 했고 어떤 DB 에도 적용·검증하지 않았다 (Docker 개발 PC 에서 검증 필요).

Revision ID: 0004
Revises: 0003
"""
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

REQUIRE_APPROVED_BATCH = """
CREATE OR REPLACE FUNCTION require_approved_batch() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE required_entity text;
BEGIN
  required_entity := CASE TG_TABLE_NAME
    WHEN 'product' THEN 'product'
    WHEN 'monthly_demand' THEN 'monthly_demand'
    WHEN 'material' THEN 'material'
    WHEN 'bom_header' THEN 'bom'
    WHEN 'bom_line' THEN 'bom'
    WHEN 'inventory' THEN 'inventory'
  END;
  IF NOT EXISTS (SELECT 1 FROM import_batch
                 WHERE batch_id = NEW.batch_id AND status = 'APPROVED' AND approved_at = now()
                   AND (required_entity IS NULL OR entity = required_entity)) THEN
    RAISE EXCEPTION 'batch % is not APPROVED in this transaction for table %: business rows can only be written by the approving transaction of a matching entity',
      NEW.batch_id, TG_TABLE_NAME USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$
"""

VERIFY_RECORD_COUNT = """
CREATE OR REPLACE FUNCTION verify_approved_record_count() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE n bigint;
BEGIN
  IF NEW.entity = 'monthly_demand' THEN
    SELECT count(*) INTO n FROM monthly_demand WHERE batch_id = NEW.batch_id;
  ELSIF NEW.entity = 'product' THEN
    SELECT count(*) INTO n FROM product WHERE batch_id = NEW.batch_id;
  ELSIF NEW.entity = 'material' THEN
    SELECT count(*) INTO n FROM material WHERE batch_id = NEW.batch_id;
  ELSIF NEW.entity = 'bom' THEN
    SELECT count(*) INTO n FROM bom_line WHERE batch_id = NEW.batch_id;
  ELSIF NEW.entity = 'inventory' THEN
    SELECT count(*) INTO n FROM inventory WHERE batch_id = NEW.batch_id;
  END IF;
  IF n IS DISTINCT FROM NEW.approved_record_count::bigint THEN
    RAISE EXCEPTION 'batch %: approved_record_count % but % rows written', NEW.batch_id, NEW.approved_record_count, n
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NULL;
END $$
"""

CHECK_INVENTORY_ITEM = """
CREATE FUNCTION check_inventory_item() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF (NEW.item_type = 'PRODUCT' AND NOT EXISTS (SELECT 1 FROM product WHERE product_id = NEW.item_id))
     OR (NEW.item_type = 'MATERIAL' AND NOT EXISTS (SELECT 1 FROM material WHERE material_id = NEW.item_id)) THEN
    RAISE EXCEPTION 'inventory item % % is not registered', NEW.item_type, NEW.item_id USING ERRCODE = 'foreign_key_violation';
  END IF;
  RETURN NEW;
END $$
"""

# 0003 본문으로 복원
OLD_REQUIRE_APPROVED_BATCH = """
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

OLD_VERIFY_RECORD_COUNT = """
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


def upgrade() -> None:
    op.drop_constraint("ck_batch_entity", "import_batch")
    op.create_check_constraint("ck_batch_entity", "import_batch", "entity IN ('monthly_demand', 'product', 'material', 'bom', 'inventory')")
    op.execute(REQUIRE_APPROVED_BATCH)
    op.execute(VERIFY_RECORD_COUNT)
    op.execute(CHECK_INVENTORY_ITEM)
    op.execute("CREATE TRIGGER trg_inventory_item_exists BEFORE INSERT OR UPDATE ON inventory FOR EACH ROW EXECUTE FUNCTION check_inventory_item()")


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_inventory_item_exists ON inventory")
    op.execute("DROP FUNCTION IF EXISTS check_inventory_item()")
    op.execute(OLD_VERIFY_RECORD_COUNT)
    op.execute(OLD_REQUIRE_APPROVED_BATCH)
    # 자재·BOM·재고 배치가 있으면 아래 제약 복원이 실패한다 (승인 기록을 임의로 지우지 않는다).
    op.drop_constraint("ck_batch_entity", "import_batch")
    op.create_check_constraint("ck_batch_entity", "import_batch", "entity IN ('monthly_demand', 'product')")
