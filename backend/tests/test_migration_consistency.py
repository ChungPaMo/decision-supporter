"""마이그레이션 SQL 을 '생성만' 해서(오프라인 모드, DB 접속 없음) 모델과 어긋나지 않는지 정적으로 확인한다.
실제 DB 적용 결과는 증명하지 않는다 (Docker 개발 PC 에서 alembic upgrade head 로 검증)."""
import re
import subprocess
import sys
from pathlib import Path

import pytest

from app.contracts import BatchStatus
from app.models import Base

BACKEND = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def sql() -> str:
    r = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head", "--sql"], cwd=BACKEND, capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    return r.stdout


def _create_cols(sql: str, table: str) -> set[str]:
    block = re.search(rf"CREATE TABLE {table} \((.*?)\n\);", sql, re.S).group(1)
    return {m.group(1) for m in re.finditer(r"^\s+([a-z0-9_]+) [A-Z]", block, re.M)}


@pytest.mark.parametrize("table", ["import_batch", "batch_source_file", "monthly_demand"])
def test_columns_match_models(sql, table):
    created = _create_cols(sql, table)
    added = set(re.findall(rf"ALTER TABLE {table} ADD COLUMN ([a-z_]+)", sql))
    assert created | added == set(Base.metadata.tables[table].columns.keys())


def test_status_check_matches_enum(sql):
    final = re.findall(r"ck_status_batchstatus CHECK \(status IN \(([^)]*)\)\)", sql)[-1]
    assert set(re.findall(r"'([A-Z_]+)'", final)) == {s.value for s in BatchStatus}


def test_gate_objects_are_generated(sql):
    for needle in ("trg_batch_new", "trg_batch_no_delete", "trg_source_file_append_only", "trg_batch_record_count",
                   "CREATE CONSTRAINT TRIGGER", "DEFERRABLE INITIALLY DEFERRED", "approved_at = now()", "forbid_change",
                   "trg_monthly_demand_no_delete", "v_forecast_input_monthly_demand", "uq_batch_live_entity_sha",
                   "ALTER COLUMN product_type DROP NOT NULL", "SELECT count(*) INTO n FROM product", "entity = TG_TABLE_NAME"):
        assert needle in sql, needle


def test_entity_check_matches_supported_entities(sql):
    from app.ingest.pipeline import ENTITIES
    final = re.findall(r"ck_batch_entity CHECK \(entity IN \(([^)]*)\)\)", sql)[-1]
    assert set(re.findall(r"'([a-z_]+)'", final)) == set(ENTITIES)


def test_migration_chain_is_linear():
    versions = BACKEND / "migrations" / "versions"
    revs = {}
    for f in versions.glob("*.py"):
        t = f.read_text(encoding="utf-8")
        revs[re.search(r'^revision = "(\w+)"', t, re.M).group(1)] = re.search(r'^down_revision = (.+)$', t, re.M).group(1).strip('"')
    assert revs == {"0001": "None", "0002": "0001", "0003": "0002"}
