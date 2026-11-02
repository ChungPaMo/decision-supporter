"""계획 결과 Excel 출력의 입력 계약 — ⚠ 초안 (plan-export-0.1-draft).

예측·계획 엔진은 아직 없다. 이 계약은 엔진 구현에 묶이지 않게 '출력에 필요한 표현'만 정의하며, 예측·최적화 담당과 업무 규칙·UX 담당의
확인 전이다 (docs/api_contract_exports.md §6). 데이터 계약 v1 과 별개이며 어떤 DB 테이블에도 대응하지 않는다.

- 수량은 모두 10진 문자열. JSON 숫자는 부동소수 오차가 있어 받지 않는다.
- 값이 없으면 null(또는 생략). 서버는 0 으로 채우지 않고 '미산출' 로 표시한다.
- 제약조건·KPI 는 항목을 고정하지 않고 배열로 받는다 (다중 CAPA: 수량·작업시간·설비를 같은 배열에 나란히 둔다).
- 알 수 없는 필드는 거부한다: 승인 여부 등을 요청에 끼워 넣을 수 없다.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

EXPORT_CONTRACT_VERSION = "plan-export-0.1-draft"

Dec = Annotated[str, Field(pattern=r"^-?[0-9]{1,18}(\.[0-9]{1,6})?$", description="10진 문자열 (예: \"1200.5\"). JSON 숫자 불가")]
Period = Annotated[str, Field(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$", description="YYYY-MM")]
Id = Annotated[str, Field(min_length=1, max_length=60)]
Short = Annotated[str, Field(max_length=200)]
Text = Annotated[str, Field(max_length=2000)]
MAX_LINES = 20_000


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Horizon(_Model):
    period_from: Period
    period_to: Period


class ForecastBasis(_Model):
    """수요 기준. 데이터 계약 v1 의 ForecastRow 와 같은 이름을 쓴다. 없으면 '미산출'."""
    quantile: Literal["P50", "P80", "P90"] | None = None
    p50_method: Short | None = None
    quantile_model_version: Short | None = None
    trained_until: dt.date | None = None


class Kpi(_Model):
    """계획안 비교 항목. 코드와 산식은 미확정이라 고정하지 않는다."""
    code: Id
    label: Short
    value: Dec | None = None
    unit: Short | None = None


class ProductionLine(_Model):
    product_id: Id
    product_name: Short | None = None
    period: Period
    planned_qty: Dec | None = None
    unit: Short | None = None
    demand_qty: Dec | None = None
    production_mode: Short | None = Field(None, description="예: INHOUSE, OUTSOURCE (미확정)")
    ending_inventory_qty: Dec | None = None
    note: Text | None = None


class MaterialLine(_Model):
    material_id: Id
    material_name: Short | None = None
    period: Period
    required_qty: Dec | None = None
    available_qty: Dec | None = None
    shortage_qty: Dec | None = None
    unit: Short | None = None
    note: Text | None = None


class ConstraintResult(_Model):
    """제약조건 1건. 다중 CAPA 는 차원이 다른 항목을 이 배열에 나란히 넣는다."""
    constraint_id: Id
    label: Short
    dimension: Short = Field(description="권장: QUANTITY, WORK_HOURS, EQUIPMENT_HOURS, MATERIAL, DUE_DATE, OTHER (미확정, 자유 문자열)")
    strength: Literal["HARD", "SOFT"]
    status: Literal["SATISFIED", "VIOLATED", "UNVERIFIED"]
    resource_id: Id | None = None
    period: Period | None = None
    limit_value: Dec | None = None
    used_value: Dec | None = None
    unit: Short | None = None
    source: Short | None = None
    note: Text | None = None


class PlanResult(_Model):
    plan_id: Id
    plan_type: Literal["BASELINE", "STABLE", "COST_SAVING"]
    feasibility: Literal["FEASIBLE", "INFEASIBLE", "UNVERIFIED"]
    kpis: list[Kpi] = Field(default=[], max_length=200)
    production: list[ProductionLine] = Field(default=[], max_length=MAX_LINES)
    materials: list[MaterialLine] = Field(default=[], max_length=MAX_LINES)
    constraints: list[ConstraintResult] = Field(default=[], max_length=MAX_LINES)
    assumptions: list[Text] = Field(default=[], max_length=200)


class Evidence(_Model):
    """계산 근거·데이터 출처 1건."""
    source_type: Literal["PUBLIC_DEMAND", "SYNTHETIC", "CLIENT_UPLOAD"]
    name: Short
    batch_id: Id | None = None
    as_of: Short | None = None
    description: Text | None = None


class PlanExportRequest(_Model):
    contract_version: Literal["plan-export-0.1-draft"]
    title: Short
    export_name: Short | None = Field(None, description="파일명에 쓸 이름. 서버가 안전한 문자만 남기고 DEMO_UNVERIFIED_ 접두사를 붙인다")
    data_classification: Literal["SYNTHETIC", "PUBLIC_DEMAND", "CLIENT_UPLOAD", "MIXED"]
    engine: Short | None = Field(None, description="결과를 만든 엔진 이름·버전. 없으면 미산출")
    generated_at: dt.datetime | None = None
    horizon: Horizon
    forecast_basis: ForecastBasis | None = None
    plans: list[PlanResult] = Field(min_length=1, max_length=3)
    evidence: list[Evidence] = Field(default=[], max_length=1000)

    @model_validator(mode="after")
    def _unique_plans(self) -> PlanExportRequest:
        types = [p.plan_type for p in self.plans]
        if len(set(types)) != len(types):
            raise ValueError("plan_type 이 중복됩니다 (계획안 유형별 1건)")
        if self.horizon.period_from > self.horizon.period_to:
            raise ValueError("horizon.period_from 이 period_to 보다 늦습니다")
        return self
