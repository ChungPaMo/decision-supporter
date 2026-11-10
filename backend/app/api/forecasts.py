import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from app.errors import ApiError, ErrorCode, ErrorResponse
from app.config import get_settings
from app.forecast.runs import ForecastRuns
from app.forecast.runs_schemas import ForecastExecution, ForecastHistory, ForecastRunReply, ForecastRunRequest
from app.forecast.schemas import DevForecastRequest, ForecastResponse
from app.forecast.service import forecast

router = APIRouter(tags=["development-forecast"])
history_router = APIRouter(prefix="/forecasts", tags=["approved-demand-forecast"], responses={
    404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}, 422: {"model": ErrorResponse},
    500: {"model": ErrorResponse}, 501: {"model": ErrorResponse}, 503: {"model": ErrorResponse},
})


def get_forecast_runs():
    if get_settings().persistence != "postgres":
        raise ApiError(501, ErrorCode.PERSISTENCE_DISABLED, "예측 실행 저장이 비활성화되어 있습니다.")
    from app.db import get_engine
    return ForecastRuns(get_engine())


Runs = Annotated[ForecastRuns, Depends(get_forecast_runs)]


@history_router.post("", response_model=ForecastRunReply, status_code=201,
                     summary="승인 월별 수요 스냅샷으로 예측 실행·저장",
                     responses={200: {"model": ForecastRunReply, "description": "동일 완료 실행 재사용"},
                                422: {"model": ForecastRunReply | ErrorResponse},
                                500: {"model": ForecastRunReply | ErrorResponse},
                                503: {"model": ForecastRunReply | ErrorResponse}})
def create_forecast(body: ForecastRunRequest, runs: Runs):
    reply = runs.create(body)
    status = 200 if reply.reused else 201
    if reply.execution.status == "INPUT_INVALID":
        status = 422
    elif reply.execution.status == "FAILED":
        status = 503 if reply.execution.failure.code == ErrorCode.FORECAST_ENGINE_UNAVAILABLE else 500
    return JSONResponse(status_code=status, content=reply.model_dump(mode="json", exclude_unset=True))


@history_router.get("", response_model=ForecastHistory, summary="예측 실행 이력 (실패·이력 부족 포함)")
def list_forecasts(runs: Runs, limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0)):
    return runs.list(limit, offset)


@history_router.get("/{execution_id}", response_model=ForecastExecution, response_model_exclude_unset=True,
                    summary="저장된 예측·제외·경고·오류 및 입력 출처 조회")
def get_forecast(execution_id: uuid.UUID, runs: Runs):
    return runs.get(execution_id)


@router.post(
    "/dev/forecasts", response_model=ForecastResponse, response_model_exclude_unset=True,
    summary="합성 JSON 입력으로 예측 실행 (개발용, DB 저장·승인 조회 없음)",
    description="SYNTHETIC 행만 허용합니다. batch_id는 입력 표식이며 승인 증명이 아닙니다. "
                "OK/PARTIAL/DATA_INSUFFICIENT는 200, 엔진 INPUT_INVALID는 422(run=null)입니다.",
    responses={
        422: {"description": "요청 스키마 오류 또는 엔진 INPUT_INVALID",
              "model": ForecastResponse | ErrorResponse},
        500: {"model": ErrorResponse, "description": "엔진 내부 오류 (세부 내용 비공개)"},
        503: {"model": ErrorResponse, "description": "엔진 의존성 미설치"},
    },
)
def create_dev_forecast(body: DevForecastRequest):
    result = forecast(body)
    if result.status == "INPUT_INVALID":
        return JSONResponse(status_code=422, content=result.model_dump(mode="json", exclude_unset=True))
    return result
