"""예측 요청 계약 `forecast-input-0.1-draft` (docs/engine_forecast_plan_contract.md §3).

입력 행은 백엔드 뷰 `v_forecast_input_monthly_demand` 의 행 그대로다. 수량은 10진 문자열만 받는다
(JSON 숫자·지수 표기·NaN 거부, 백엔드 계약 v1 과 같은 원칙).
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

REQUEST_VERSION = "forecast-input-0.1-draft"
RESPONSE_VERSION = "forecast-output-0.1-draft"

CODE_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.\-]*$"   # 백엔드 app/contracts.py 와 같음
Code = Annotated[str, Field(min_length=1, max_length=40, pattern=CODE_PATTERN)]
Period = Annotated[str, Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")]
QtyText = Annotated[str, Field(pattern=r"^\d{1,14}(\.\d{1,4})?$")]

P50Method = Literal["MA12", "LGBM_L1"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class DemandRow(_Model):
    product_id: Code
    period: Period
    demand_qty: QtyText
    unit: Code
    source_type: Literal["PUBLIC_DEMAND", "SYNTHETIC", "CLIENT_UPLOAD"]
    batch_id: Annotated[str, Field(min_length=1, max_length=80)]


class ForecastRequest(_Model):
    contract_version: Literal["forecast-input-0.1-draft"]
    request_id: Annotated[str, Field(min_length=1, max_length=80)]
    trained_until: dt.date
    # 기록(26-10-26 실험기록)에서 검증한 범위는 h=1~3. 4~12는 경고와 함께 허용.
    horizon_months: Annotated[int, Field(ge=1, le=12)] = 3
    p50_method: P50Method = "MA12"
    quantiles: list[Literal["P50", "P80", "P90"]] = ["P50", "P80", "P90"]
    min_history_months: Annotated[int, Field(ge=12, le=120)] = 12
    missing_month_policy: Literal["ZERO_FILL"] = "ZERO_FILL"
    random_seed: Annotated[int, Field(ge=0, le=2**31 - 1)] = 42
    rows: list[DemandRow]
