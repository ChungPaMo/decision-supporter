from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.batches import router as batches_router
from app.api.exports import router as exports_router
from app.api.health import router as health_router
from app.api.ingest import router as ingest_router
from app.config import Settings, get_settings
from app.errors import ApiError, ErrorCode
from app.exports.limits import ExportBodyLimit

app = FastAPI(title="충파모 과제 7 MVP", version=get_settings().app_version)
app.include_router(health_router)
app.include_router(ingest_router)
app.include_router(batches_router)
app.include_router(exports_router)


def configure_cors(target: FastAPI, settings: Settings) -> None:
    """허용 출처를 명시한 경우에만 켠다 (기본: CORS 없음). 쿠키 인증을 쓰지 않으므로 credentials 는 끈다."""
    origins = settings.cors_origins()
    if origins:
        target.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["GET", "POST"], allow_headers=["Content-Type"],
                              allow_credentials=False, expose_headers=["Content-Disposition", "X-Export-Status"])


# 나중에 추가한 미들웨어가 바깥쪽이다: CORS 를 바깥에 두어 413 응답에도 CORS 헤더가 붙게 한다
app.add_middleware(ExportBodyLimit)
configure_cors(app, get_settings())


@app.exception_handler(ApiError)
async def api_error_handler(_: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(exc.body(), status_code=exc.status)


@app.exception_handler(RequestValidationError)
async def validation_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    """FastAPI 기본 422(목록 형태)를 다른 오류와 같은 {"detail": {code, message, details}} 로 통일. 입력값 자체는 응답에 싣지 않는다."""
    fields = [{"loc": [str(x) for x in e["loc"]], "message": e["msg"]} for e in exc.errors()]
    return JSONResponse({"detail": {"code": ErrorCode.REQUEST_INVALID.value, "message": "요청 형식이 올바르지 않습니다.", "details": fields}},
                        status_code=422)
