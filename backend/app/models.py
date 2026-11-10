"""PostgreSQL 데이터 모델 v1. 계약: docs/data_contract_v1.md, app/contracts.py.

설계 원칙
- 수량은 NUMERIC (PoC SQLite 는 정밀도 보존용으로 TEXT 를 썼다. PostgreSQL 은 NUMERIC 으로 해결).
- 열거형은 PG ENUM 이 아니라 TEXT + CHECK (마이그레이션에서 값 추가·삭제가 쉽다).
- 모든 업무 테이블 행은 batch_id 로 출처(source_type)와 승인 상태를 추적한다.
  승인된 배치만 행을 쓸 수 있다는 규칙은 DB 트리거(migrations)가 한 번 더 강제한다.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint, Date, DateTime, ForeignKey, Identity, Index, Integer, LargeBinary, Numeric, String,
    UniqueConstraint, func, text,
)
from sqlalchemy.dialects.postgresql import ExcludeConstraint, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.contracts import (
    BatchStatus, BomStatus, ComponentType, ItemType, ProductType, SourceType, UnitDimension,
)

QTY = Numeric(18, 4)
PERIOD_RE = r"^\d{4}-(0[1-9]|1[0-2])$"


def _in(col: str, enum_cls) -> CheckConstraint:
    values = ", ".join(f"'{m.value}'" for m in enum_cls)
    return CheckConstraint(f"{col} IN ({values})", name=f"ck_{col}_{enum_cls.__name__.lower()}")


class Base(DeclarativeBase):
    pass


class ImportBatch(Base):
    """업로드 1회 = 배치 1개. 출처 구분·중복 업로드 탐지·승인 상태의 기준."""
    __tablename__ = "import_batch"

    batch_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    source_type: Mapped[str] = mapped_column(String(20))
    source_name: Mapped[str] = mapped_column(String(260))                 # 파일명 또는 데이터셋 이름 (경로·사용자명 금지)
    source_note: Mapped[str | None] = mapped_column(String(500))          # PUBLIC_DEMAND: 원 출처·라이선스, SYNTHETIC: 생성 방법
    source_sha256: Mapped[str] = mapped_column(String(64))                # 원본 바이트의 SHA-256 (중복 업로드 탐지)
    status: Mapped[str] = mapped_column(String(20), server_default=BatchStatus.MAPPING_HOLD.value)
    uploaded_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    approved_by: Mapped[str | None] = mapped_column(String(60))           # 역할 코드. 실명 금지
    approved_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    blocking_error_count: Mapped[int] = mapped_column(Integer, server_default="0")   # 검증 ERROR 수. 0이어야 승인 가능
    # --- 0002: 승인 전 임시 보관(staging)과 승인 감사 정보 ---
    entity: Mapped[str] = mapped_column(String(30), server_default="monthly_demand")
    created_by: Mapped[str | None] = mapped_column(String(60))
    default_unit: Mapped[str | None] = mapped_column(String(10))                        # 사용자가 지정한 설정 3종: 재검증 입력
    column_overrides: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    sheet_filter: Mapped[str | None] = mapped_column(String(260))
    warning_count: Mapped[int] = mapped_column(Integer, server_default="0")
    result_digest: Mapped[str | None] = mapped_column(String(64))                       # 검증 결과(레코드·이슈 전체)의 SHA-256. 승인 요청이 이 값을 가리켜야 한다
    validation_result: Mapped[dict | None] = mapped_column(JSONB)                       # 화면 표시용 스냅샷(레코드는 앞 100건). 승인에는 쓰지 않고 원본 재검증 결과를 쓴다
    version: Mapped[int] = mapped_column(Integer, server_default="1")                   # 낙관적 잠금: 재검증마다 +1
    approved_record_count: Mapped[int | None] = mapped_column(Integer)                  # 승인 시 반영한 행 수. 커밋 시점에 DB가 실제 행 수와 대조
    rejected_by: Mapped[str | None] = mapped_column(String(60))
    rejected_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    reject_reason: Mapped[str | None] = mapped_column(String(500))

    __table_args__ = (
        _in("source_type", SourceType),
        _in("status", BatchStatus),
        CheckConstraint("char_length(source_sha256) = 64", name="ck_batch_sha_len"),
        CheckConstraint("blocking_error_count >= 0 AND (status <> 'APPROVED' OR blocking_error_count = 0)", name="ck_batch_no_errors_when_approved"),
        CheckConstraint("status <> 'APPROVED' OR (approved_by IS NOT NULL AND approved_at IS NOT NULL)", name="ck_batch_approved_meta"),
        CheckConstraint("status <> 'APPROVED' OR approved_record_count > 0", name="ck_batch_approved_count"),
        CheckConstraint("status <> 'READY_FOR_APPROVAL' OR blocking_error_count = 0", name="ck_batch_ready_no_errors"),
        CheckConstraint("status <> 'HAS_ERRORS' OR blocking_error_count > 0", name="ck_batch_errors_counted"),
        CheckConstraint("status <> 'REJECTED' OR (rejected_by IS NOT NULL AND rejected_at IS NOT NULL AND reject_reason IS NOT NULL)", name="ck_batch_rejected_meta"),
        CheckConstraint("entity IN ('monthly_demand', 'product', 'material', 'bom', 'inventory')", name="ck_batch_entity"),
        # 같은 entity 에 같은 원본이 살아있는(반려되지 않은) 배치로 있으면 재업로드를 막는다. 반려 후 재업로드는 허용.
        Index("uq_batch_live_entity_sha", "entity", "source_sha256", unique=True, postgresql_where=text("status <> 'REJECTED'")),
    )


class BatchSourceFile(Base):
    """업로드 원본 바이트. 한 번 쓰면 수정·삭제 불가(트리거). 재검증과 승인은 항상 이 바이트를 다시 파싱한다."""
    __tablename__ = "batch_source_file"

    batch_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("import_batch.batch_id"), primary_key=True)
    content: Mapped[bytes] = mapped_column(LargeBinary)
    size_bytes: Mapped[int] = mapped_column(Integer)

    __table_args__ = (CheckConstraint("size_bytes = octet_length(content) AND size_bytes > 0", name="ck_source_file_size"),)


def _batch_fk() -> Mapped[uuid.UUID]:
    """승인된 배치에서 온 행의 출처 FK."""
    return mapped_column(UUID(as_uuid=True), ForeignKey("import_batch.batch_id"))


class Unit(Base):
    """단위 기준표. 마이그레이션이 기본값을 넣는 참조 데이터이며 업로드 대상이 아니다."""
    __tablename__ = "unit"

    unit_code: Mapped[str] = mapped_column(String(40), primary_key=True)
    dimension: Mapped[str] = mapped_column(String(10))
    to_base_factor: Mapped[Decimal] = mapped_column(Numeric(24, 12))

    __table_args__ = (_in("dimension", UnitDimension), CheckConstraint("to_base_factor > 0", name="ck_unit_factor_pos"))


class Product(Base):
    __tablename__ = "product"

    product_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    product_name: Mapped[str] = mapped_column(String(200))
    product_type: Mapped[str | None] = mapped_column(String(15))      # 0003: 선택 필드. 파일에 없으면 NULL
    base_unit: Mapped[str] = mapped_column(ForeignKey("unit.unit_code"))
    safety_stock_qty: Mapped[Decimal] = mapped_column(QTY, server_default="0")
    batch_id: Mapped[uuid.UUID] = _batch_fk()

    __table_args__ = (_in("product_type", ProductType), CheckConstraint("safety_stock_qty >= 0", name="ck_product_ss"))


class Material(Base):
    __tablename__ = "material"

    material_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    material_name: Mapped[str] = mapped_column(String(200))
    base_unit: Mapped[str] = mapped_column(ForeignKey("unit.unit_code"))
    safety_stock_qty: Mapped[Decimal] = mapped_column(QTY, server_default="0")
    batch_id: Mapped[uuid.UUID] = _batch_fk()

    __table_args__ = (CheckConstraint("safety_stock_qty >= 0", name="ck_material_ss"),)


class MonthlyDemand(Base):
    """제품별 월별 수요 실적/주문 합계 (예측 입력)."""
    __tablename__ = "monthly_demand"

    product_id: Mapped[str] = mapped_column(ForeignKey("product.product_id"), primary_key=True)
    period: Mapped[str] = mapped_column(String(7), primary_key=True)       # YYYY-MM
    demand_qty: Mapped[Decimal] = mapped_column(QTY)
    unit: Mapped[str] = mapped_column(ForeignKey("unit.unit_code"))
    batch_id: Mapped[uuid.UUID] = _batch_fk()

    __table_args__ = (
        CheckConstraint("demand_qty >= 0", name="ck_demand_qty"),
        CheckConstraint(f"period ~ '{PERIOD_RE}'", name="ck_demand_period"),
    )

class SalesOrder(Base):
    __tablename__ = "sales_order"

    order_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    product_id: Mapped[str] = mapped_column(ForeignKey("product.product_id"))
    order_qty: Mapped[Decimal] = mapped_column(QTY)
    unit: Mapped[str] = mapped_column(ForeignKey("unit.unit_code"))
    order_date: Mapped[dt.date] = mapped_column(Date)
    due_date: Mapped[dt.date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(10))
    batch_id: Mapped[uuid.UUID] = _batch_fk()

    __table_args__ = (
        CheckConstraint("status IN ('OPEN','CLOSED','CANCELLED')", name="ck_order_status"),
        CheckConstraint("order_qty > 0", name="ck_order_qty"),
        CheckConstraint("due_date >= order_date", name="ck_order_dates"),
    )


class BomHeader(Base):
    """BOM 버전. 같은 제품의 활성 버전은 유효기간이 겹칠 수 없다 (EXCLUDE, btree_gist 필요)."""
    __tablename__ = "bom_header"

    bom_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    product_id: Mapped[str] = mapped_column(ForeignKey("product.product_id"))
    bom_version: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(10))
    is_active: Mapped[bool]
    valid_from: Mapped[dt.date] = mapped_column(Date)
    valid_to: Mapped[dt.date | None] = mapped_column(Date)
    base_qty: Mapped[Decimal] = mapped_column(QTY, server_default="1")
    batch_id: Mapped[uuid.UUID] = _batch_fk()

    __table_args__ = (
        _in("status", BomStatus),
        UniqueConstraint("product_id", "bom_version", name="uq_bom_product_version"),
        CheckConstraint("NOT (is_active AND status <> 'APPROVED')", name="ck_bom_active_approved"),
        CheckConstraint("valid_to IS NULL OR valid_to >= valid_from", name="ck_bom_period"),
        CheckConstraint("base_qty > 0", name="ck_bom_base_qty"),
        ExcludeConstraint(
            ("product_id", "="), (text("daterange(valid_from, valid_to, '[]')"), "&&"),
            using="gist", where=text("is_active"), name="ex_bom_active_overlap",
        ),
    )


class BomLine(Base):
    __tablename__ = "bom_line"

    bom_id: Mapped[str] = mapped_column(ForeignKey("bom_header.bom_id"), primary_key=True)
    line_no: Mapped[int] = mapped_column(Integer, primary_key=True)
    component_type: Mapped[str] = mapped_column(String(10))
    component_material_id: Mapped[str | None] = mapped_column(ForeignKey("material.material_id"))
    component_product_id: Mapped[str | None] = mapped_column(ForeignKey("product.product_id"))
    qty_per: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    unit: Mapped[str] = mapped_column(ForeignKey("unit.unit_code"))
    scrap_rate: Mapped[Decimal] = mapped_column(Numeric(7, 6), server_default="0")
    batch_id: Mapped[uuid.UUID] = _batch_fk()

    __table_args__ = (
        _in("component_type", ComponentType),
        CheckConstraint("line_no >= 1", name="ck_bomline_no"),
        CheckConstraint("qty_per > 0", name="ck_bomline_qty"),
        CheckConstraint("scrap_rate >= 0 AND scrap_rate < 1", name="ck_bomline_scrap"),
        CheckConstraint(
            "(component_type = 'MATERIAL' AND component_material_id IS NOT NULL AND component_product_id IS NULL)"
            " OR (component_type = 'PRODUCT' AND component_product_id IS NOT NULL AND component_material_id IS NULL)",
            name="ck_bomline_component",
        ),
    )


class Inventory(Base):
    """기준일별 재고. item_id 는 item_type 에 따라 product/material 을 가리키는 조건부 참조라 FK 대신 검증 단계에서 확인한다."""
    __tablename__ = "inventory"

    item_type: Mapped[str] = mapped_column(String(10), primary_key=True)
    item_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    as_of_date: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    on_hand_qty: Mapped[Decimal] = mapped_column(QTY)
    allocated_qty: Mapped[Decimal] = mapped_column(QTY, server_default="0")
    unit: Mapped[str] = mapped_column(ForeignKey("unit.unit_code"))
    batch_id: Mapped[uuid.UUID] = _batch_fk()

    __table_args__ = (
        _in("item_type", ItemType),
        CheckConstraint("on_hand_qty >= 0 AND allocated_qty >= 0 AND allocated_qty <= on_hand_qty", name="ck_inventory_qty"),
    )


class ProductionCapacity(Base):
    __tablename__ = "production_capacity"

    resource_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    resource_name: Mapped[str] = mapped_column(String(200))
    regular_hours_per_day: Mapped[Decimal] = mapped_column(Numeric(5, 2))
    overtime_hours_per_day_max: Mapped[Decimal] = mapped_column(Numeric(5, 2))
    batch_id: Mapped[uuid.UUID] = _batch_fk()

    __table_args__ = (
        CheckConstraint(
            "regular_hours_per_day >= 0 AND overtime_hours_per_day_max >= 0"
            " AND regular_hours_per_day + overtime_hours_per_day_max <= 24",
            name="ck_capacity_hours",
        ),
    )


class DemandForecast(Base):
    """예측 결과 (예측 모듈 출력 저장소). 업로드가 아니므로 배치 승인 대상이 아니다."""
    __tablename__ = "demand_forecast"

    forecast_id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    forecast_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("forecast_run.execution_id"))
    product_id: Mapped[str] = mapped_column(ForeignKey("product.product_id"))
    target_month: Mapped[str] = mapped_column(String(7))       # YYYY-MM
    horizon: Mapped[int] = mapped_column(Integer)
    p50: Mapped[Decimal] = mapped_column(QTY)
    p80: Mapped[Decimal] = mapped_column(QTY)
    p90: Mapped[Decimal] = mapped_column(QTY)
    p50_method: Mapped[str] = mapped_column(String(60))
    quantile_model_version: Mapped[str] = mapped_column(String(60))
    trained_until: Mapped[dt.date] = mapped_column(Date)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint("horizon >= 1", name="ck_forecast_horizon"),
        CheckConstraint("p50 >= 0 AND p50 <= p80 AND p80 <= p90", name="ck_forecast_quantiles"),
        CheckConstraint(f"target_month ~ '{PERIOD_RE}'", name="ck_forecast_month"),
        UniqueConstraint("forecast_run_id", "product_id", "target_month", "horizon", name="uq_forecast_execution_row"),
        Index("uq_forecast_legacy", "product_id", "target_month", "horizon", "quantile_model_version", "trained_until",
              unique=True, postgresql_where=text("forecast_run_id IS NULL")),
    )


class ForecastExecution(Base):
    __tablename__ = "forecast_run"
    execution_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    dedup_key: Mapped[str] = mapped_column(String(64))
    input_digest: Mapped[str] = mapped_column(String(71))
    parameters: Mapped[dict] = mapped_column(JSONB)
    input_snapshot: Mapped[dict] = mapped_column(JSONB)
    runtime_identity: Mapped[dict] = mapped_column(JSONB)
    input_row_count: Mapped[int] = mapped_column(Integer)
    forecast_count: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(24))
    quantile_model_version: Mapped[str | None] = mapped_column(String(60))
    result: Mapped[dict | None] = mapped_column(JSONB)
    failure: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp())
    __table_args__ = (
        CheckConstraint("input_row_count >= 0"), CheckConstraint("forecast_count >= 0"),
        CheckConstraint("status IN ('OK','PARTIAL','DATA_INSUFFICIENT','INPUT_INVALID','FAILED')"),
        CheckConstraint("(status = 'FAILED' AND result IS NULL AND failure IS NOT NULL AND forecast_count = 0) OR "
                        "(status <> 'FAILED' AND result IS NOT NULL AND failure IS NULL AND result->>'status' = status)", name="ck_forecast_run_result"),
        Index("uq_forecast_dedup_completed", "dedup_key", unique=True, postgresql_where=text("status <> 'FAILED'")),
        Index("ix_forecast_run_created", text("created_at DESC"), text("execution_id DESC")),
    )


class ForecastInputBatch(Base):
    __tablename__ = "forecast_run_input"
    execution_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("forecast_run.execution_id"), primary_key=True)
    batch_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("import_batch.batch_id"), primary_key=True)
    batch_version: Mapped[int] = mapped_column(Integer)
    source_sha256: Mapped[str] = mapped_column(String(64))
    result_digest: Mapped[str | None] = mapped_column(String(64))
    approved_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))


# 승인 트리거를 붙일 테이블 (migrations/versions 의 초기 마이그레이션에서 사용)
GOVERNED_TABLES = [
    "product", "material", "monthly_demand", "sales_order", "bom_header", "bom_line", "inventory", "production_capacity",
]
