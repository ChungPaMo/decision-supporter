from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.config import get_settings
from app.db import get_engine

router = APIRouter()


@router.get("/health", summary="서버·DB 상태 확인")
def health() -> JSONResponse:
    """DB 연결이 되면 200, 안 되면 503. DB 오류 상세(접속 정보 포함 가능)는 응답에 싣지 않는다."""
    body = {"status": "ok", "version": get_settings().app_version, "database": {"status": "ok"}}
    try:
        with get_engine().connect() as conn:
            body["database"]["server_version"] = conn.execute(text("SHOW server_version")).scalar_one()
    except Exception:
        body["status"] = "degraded"
        body["database"] = {"status": "unavailable"}
        return JSONResponse(body, status_code=503)
    return JSONResponse(body)
