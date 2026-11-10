"""엔진 호출·오류 경계. DB 조회/저장 및 예측 계산은 하지 않는다."""

import logging

from app.errors import ApiError, ErrorCode
from app.contracts import ForecastRow
from app.forecast.schemas import DevForecastRequest, ForecastResponse
from engine.forecast.contract import ForecastRequest

logger = logging.getLogger(__name__)


def _engine_run(request: dict) -> dict:
    from engine.forecast import run

    return run(request)


def forecast(request: DevForecastRequest) -> ForecastResponse:
    return execute(request)


def execute(request: ForecastRequest) -> ForecastResponse:
    try:
        # 생략한 기본값은 추가하지 않아 엔진 input_digest 의미도 유지한다.
        result = _engine_run(request.model_dump(mode="json", exclude_unset=True))
        for row in result.get("forecasts", []):
            # Validate established decimal/quantile rules without rewriting the
            # engine response or dropping any additional metadata.
            ForecastRow.model_validate({key: value for key, value in row.items() if key in ForecastRow.model_fields})
        return ForecastResponse.model_validate(result)
    except (ImportError, ModuleNotFoundError):
        raise ApiError(503, ErrorCode.FORECAST_ENGINE_UNAVAILABLE,
                       "예측 엔진 의존성이 준비되지 않았습니다.") from None
    except Exception as exc:
        # 입력 데이터/예외 문자열/경로를 응답이나 로그에 노출하지 않는다.
        logger.error("Forecast engine failed (%s)", type(exc).__name__)
        raise ApiError(500, ErrorCode.FORECAST_ENGINE_ERROR,
                       "예측 엔진 실행에 실패했습니다.") from None
