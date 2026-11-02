"""데이터 계약 v1 (docs/data_contract_v1.md 의 코드 표현).

- 수량은 모두 Decimal. float·NaN·지수 표기는 받지 않는다 (PoC E05: pydantic lax 모드가 NaN·전각 숫자를 통과시킴).
- 필드 이름은 DB 열 이름과 같다. tests/test_contracts.py 가 이를 검사한다.
- 업로드·승인 메타(source_type, batch_id)는 행 계약이 아니라 배치 계약에 속한다. 행에는 DB가 배치에서 채운다.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CONTRACT_VERSION = "1.2"

Qty = Annotated[Decimal, Field(ge=0, allow_inf_nan=False, max_digits=18, decimal_places=4)]
Period = Annotated[str, Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")]   # YYYY-MM
CODE_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.\-]*$"
Code = Annotated[str, Field(min_length=1, max_length=40, pattern=CODE_PATTERN)]


class SourceType(StrEnum):
    PUBLIC_DEMAND = "PUBLIC_DEMAND"    # 공개 수요 데이터 (원 출처·라이선스를 import_batch.source_note 에 기록)
    SYNTHETIC = "SYNTHETIC"            # 합성 BOM·재고·생산능력 (실제 기업 값이 아님)
    CLIENT_UPLOAD = "CLIENT_UPLOAD"    # 수요기업이 올린 실제 파일


class BatchStatus(StrEnum):
    """배치 상태. 검증 결과가 곧 상태다(업로드·재검증 때마다 서버가 다시 계산). 최종 상태는 APPROVED, REJECTED."""
    MAPPING_HOLD = "MAPPING_HOLD"                # 열 매핑이 불확실 → 사용자가 열을 지정해야 함. 승인 불가
    HAS_ERRORS = "HAS_ERRORS"                    # 검증 ERROR 1건 이상. 승인 불가 (원본 수정 후 재업로드, 또는 제품 등록 후 재검증)
    READY_FOR_APPROVAL = "READY_FOR_APPROVAL"    # 오류 0건. 사용자의 명시적 승인 대기 (경고는 있을 수 있음)
    APPROVED = "APPROVED"                        # 승인됨. 최종·불변. 승인한 트랜잭션에서만 업무 행을 쓸 수 있다
    REJECTED = "REJECTED"                        # 반려됨. 최종·불변. 같은 파일 재업로드는 허용


class UnitDimension(StrEnum):
    COUNT = "COUNT"
    MASS = "MASS"
    TIME = "TIME"


class ProductType(StrEnum):
    FINISHED = "FINISHED"
    SEMI_FINISHED = "SEMI_FINISHED"


class ComponentType(StrEnum):
    MATERIAL = "MATERIAL"
    PRODUCT = "PRODUCT"


class BomStatus(StrEnum):
    DRAFT = "DRAFT"
    APPROVED = "APPROVED"


class ItemType(StrEnum):
    PRODUCT = "PRODUCT"
    MATERIAL = "MATERIAL"


class _Row(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class UnitRow(_Row):
    unit_code: Code
    dimension: UnitDimension
    to_base_factor: Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]   # 같은 dimension 의 기준 단위 1개 = 몇 개


class ProductRow(_Row):
    product_id: Code
    product_name: Annotated[str, Field(min_length=1, max_length=200)]
    product_type: ProductType | None = None      # 선택. 없으면 값을 지어내지 않고 NULL
    base_unit: Code
    safety_stock_qty: Qty = Decimal(0)


class MaterialRow(_Row):
    material_id: Code
    material_name: Annotated[str, Field(min_length=1, max_length=200)]
    base_unit: Code
    safety_stock_qty: Qty = Decimal(0)


class MonthlyDemandRow(_Row):
    """제품별 월별 수요 실적/주문 합계. 예측 모듈의 입력."""
    product_id: Code
    period: Period                 # YYYY-MM
    demand_qty: Qty
    unit: Code

class SalesOrderRow(_Row):
    order_id: Code
    product_id: Code
    order_qty: Annotated[Decimal, Field(gt=0, allow_inf_nan=False, max_digits=18, decimal_places=4)]
    unit: Code
    order_date: dt.date
    due_date: dt.date
    status: str = Field(pattern=r"^(OPEN|CLOSED|CANCELLED)$")

    @model_validator(mode="after")
    def _dates(self) -> SalesOrderRow:
        if self.due_date < self.order_date:
            raise ValueError("due_date 가 order_date 보다 빠르다")
        return self


class BomHeaderRow(_Row):
    bom_id: Code
    product_id: Code
    bom_version: Code
    status: BomStatus
    is_active: bool
    valid_from: dt.date
    valid_to: dt.date | None = None
    base_qty: Annotated[Decimal, Field(gt=0, allow_inf_nan=False)] = Decimal(1)

    @model_validator(mode="after")
    def _period(self) -> BomHeaderRow:
        if self.valid_to is not None and self.valid_to < self.valid_from:
            raise ValueError("valid_to 가 valid_from 보다 빠르다")
        if self.is_active and self.status != BomStatus.APPROVED:
            raise ValueError("APPROVED 가 아닌 BOM 은 활성화할 수 없다")
        return self


class BomLineRow(_Row):
    bom_id: Code
    line_no: Annotated[int, Field(ge=1)]
    component_type: ComponentType
    component_id: Code             # component_type 에 따라 material_id 또는 product_id
    qty_per: Annotated[Decimal, Field(gt=0, allow_inf_nan=False, max_digits=18, decimal_places=6)]
    unit: Code
    scrap_rate: Annotated[Decimal, Field(ge=0, lt=1, allow_inf_nan=False)] = Decimal(0)


class InventoryRow(_Row):
    item_type: ItemType
    item_id: Code
    as_of_date: dt.date
    on_hand_qty: Qty
    allocated_qty: Qty = Decimal(0)
    unit: Code

    @model_validator(mode="after")
    def _alloc(self) -> InventoryRow:
        if self.allocated_qty > self.on_hand_qty:
            raise ValueError("allocated_qty 가 on_hand_qty 보다 크다")
        return self


class CapacityRow(_Row):
    """생산능력. PoC 와 같이 자원별 하루 시간 기준. 제품 1개당 시간(Routing) 환산은 v1 범위 밖."""
    resource_id: Code
    resource_name: Annotated[str, Field(min_length=1, max_length=200)]
    regular_hours_per_day: Annotated[Decimal, Field(ge=0, le=24, allow_inf_nan=False)]
    overtime_hours_per_day_max: Annotated[Decimal, Field(ge=0, le=24, allow_inf_nan=False)]

    @model_validator(mode="after")
    def _day(self) -> CapacityRow:
        if self.regular_hours_per_day + self.overtime_hours_per_day_max > 24:
            raise ValueError("정규+잔업 시간이 24시간을 넘는다")
        return self


class ForecastRow(_Row):
    """예측 모듈(예측·최적화 담당) 출력. 제품·목표월별 P50/P80/P90."""
    product_id: Code
    target_month: Period           # YYYY-MM
    horizon: Annotated[int, Field(ge=1, le=36)]       # 학습 종료월(trained_until) 기준 몇 개월 뒤인지
    p50: Qty
    p80: Qty
    p90: Qty
    p50_method: Annotated[str, Field(min_length=1, max_length=60)]
    quantile_model_version: Annotated[str, Field(min_length=1, max_length=60)]
    trained_until: dt.date

    @model_validator(mode="after")
    def _ordered(self) -> ForecastRow:
        if not (self.p50 <= self.p80 <= self.p90):
            raise ValueError("p50 <= p80 <= p90 이어야 한다")
        return self
