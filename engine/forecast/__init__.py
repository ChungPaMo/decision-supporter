"""월별 수요예측 모듈. `run(request: dict) -> dict`."""

def run(request: dict) -> dict:
    # 계약/OpenAPI 조회에는 모델 라이브러리가 필요하지 않다.
    from engine.forecast.service import run as forecast_run

    return forecast_run(request)

__all__ = ["run"]
