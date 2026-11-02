from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.batches import router as batches_router
from app.api.health import router as health_router
from app.api.ingest import router as ingest_router
from app.config import get_settings
from app.errors import ApiError, ErrorCode

app = FastAPI(title="충파모 과제 7 MVP", version=get_settings().app_version)
app.include_router(health_router)
app.include_router(ingest_router)
app.include_router(batches_router)


@app.exception_handler(ApiError)
async def api_error_handler(_: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(exc.body(), status_code=exc.status)


@app.exception_handler(RequestValidationError)
async def validation_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    """FastAPI 기본 422(목록 형태)를 다른 오류와 같은 {"detail": {code, message, details}} 로 통일. 입력값 자체는 응답에 싣지 않는다."""
    fields = [{"loc": [str(x) for x in e["loc"]], "message": e["msg"]} for e in exc.errors()]
    return JSONResponse({"detail": {"code": ErrorCode.REQUEST_INVALID.value, "message": "요청 형식이 올바르지 않습니다.", "details": fields}},
                        status_code=422)
