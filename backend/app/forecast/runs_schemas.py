import datetime as dt
import uuid
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.forecast.schemas import ForecastResponse


class ForecastRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    contract_version: Literal["forecast-run-0.1"] = "forecast-run-0.1"
    trained_until: dt.date
    horizon_months: Annotated[int, Field(ge=1, le=12)] = 3
    p50_method: Literal["MA12", "LGBM_L1"] = "MA12"
    min_history_months: Annotated[int, Field(ge=12, le=120)] = 12
    random_seed: Annotated[int, Field(ge=0, le=2**31 - 1)] = 42

    @field_validator("trained_until")
    @classmethod
    def supported_year(cls, value):
        if value.year > 9998:
            raise ValueError("trained_until year must be <= 9998")
        return value


class ForecastFailure(BaseModel):
    code: str
    message: str


class ForecastExecutionSummary(BaseModel):
    execution_id: uuid.UUID
    status: Literal["OK", "PARTIAL", "DATA_INSUFFICIENT", "INPUT_INVALID", "FAILED"]
    input_digest: str
    dedup_key: str
    parameters: ForecastRunRequest
    runtime_identity: dict
    quantile_model_version: str | None
    input_row_count: int
    forecast_count: int
    created_at: dt.datetime
    finished_at: dt.datetime


class InputBatchSnapshot(BaseModel):
    batch_id: uuid.UUID
    batch_version: int
    source_sha256: str
    result_digest: str | None
    approved_at: dt.datetime


class ForecastExecution(ForecastExecutionSummary):
    input_batches: list[InputBatchSnapshot]
    result: ForecastResponse | None
    failure: ForecastFailure | None


class ForecastRunReply(BaseModel):
    execution: ForecastExecution
    reused: bool


class ForecastHistory(BaseModel):
    items: list[ForecastExecutionSummary]
    total: int
    limit: int
    offset: int
