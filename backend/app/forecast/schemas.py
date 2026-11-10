"""엔진 계약을 재사용하고 개발 API의 합성 입력 범위만 좁힌다."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from engine.forecast.contract import DemandRow, ForecastRequest


class SyntheticDemandRow(DemandRow):
    source_type: Literal["SYNTHETIC"]
    demand_qty: Annotated[str, Field(pattern=r"^[0-9]{1,14}(\.[0-9]{1,4})?$")]
    period: Annotated[str, Field(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")]

    @field_validator("period")
    @classmethod
    def supported_year(cls, value):
        if not 1 <= int(value[:4]) <= 9998:
            raise ValueError("period 연도는 0001~9998 범위여야 합니다.")
        return value


class DevForecastRequest(ForecastRequest):
    rows: list[SyntheticDemandRow]

    @field_validator("trained_until")
    @classmethod
    def supported_year(cls, value):
        if value.year > 9998:
            raise ValueError("trained_until 연도는 9998 이하여야 합니다.")
        return value


class _Output(BaseModel):
    # 경고 종류마다 붙는 product_id/periods/training_rows 등의 정보도 보존한다.
    model_config = ConfigDict(extra="allow", strict=True)


class ForecastRun(_Output):
    run_id: str
    engine: str
    p50_method: Literal["MA12", "LGBM_L1"]
    quantile_model_version: str
    trained_until: str
    horizon_months: int
    random_seed: int
    input_digest: str
    generated_at: str
    data_classification: str | None


class ForecastValue(_Output):
    product_id: str
    target_month: str
    horizon: int
    p50: str
    p80: str
    p90: str
    p50_method: str
    quantile_model_version: str
    trained_until: str


class SkippedProduct(_Output):
    product_id: str
    reason_code: str
    detail: str
    observed_months: int | None


class Quantiles(_Output):
    p50: str
    p80: str
    p90: str


class ForecastAdjustment(_Output):
    product_id: str
    target_month: str
    code: str
    before: Quantiles
    after: Quantiles


class ForecastWarning(_Output):
    code: str
    detail: str


class ForecastIssue(_Output):
    code: str
    detail: str
    loc: str | None = None


class ForecastResponse(_Output):
    contract_version: Literal["forecast-output-0.1-draft"]
    request_id: str | None
    status: Literal["OK", "PARTIAL", "DATA_INSUFFICIENT", "INPUT_INVALID"]
    run: ForecastRun | None
    forecasts: list[ForecastValue]
    skipped: list[SkippedProduct]
    adjustments: list[ForecastAdjustment]
    warnings: list[ForecastWarning]
    errors: list[ForecastIssue]

    @model_validator(mode="after")
    def run_matches_status(self):
        if (self.status == "INPUT_INVALID") != (self.run is None):
            raise ValueError("run=null은 INPUT_INVALID일 때만 허용됩니다.")
        return self
