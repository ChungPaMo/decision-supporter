"""업로드 파일 미리보기 API (초안). DB 를 쓰지 않고 파일을 저장하지 않는다 — 요청 처리 동안 메모리에만 존재한다.

승인(POST /batches/{id}/approve)과 DB 저장은 Docker 개발 PC 에서 검증한 뒤 연결한다.
"""

import json

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from app.ingest.parsing import MAX_BYTES, FileError
from app.ingest.pipeline import ENTITIES, PreviewOut, run_preview

router = APIRouter(prefix="/ingest", tags=["ingest"])

MAX_RETURNED_ISSUES = 1000
MAX_RETURNED_RECORDS = 100


@router.post("/preview", response_model=PreviewOut, summary="CSV/XLSX 파싱·매핑·검증 미리보기 (저장 없음)")
async def preview(
    file: UploadFile = File(...),
    entity: str = Form("monthly_demand"),
    default_unit: str | None = Form(None, description="파일에 단위 열이 없을 때 적용할 단위"),
    column_overrides: str | None = Form(None, description='JSON. 예: {"demand_qty": "C"} — 매핑 보류를 풀 때 열 이름 또는 열 문자 지정'),
    sheet: str | None = Form(None, description="XLSX 에서 이 시트만 처리"),
) -> PreviewOut:
    if entity not in ENTITIES:
        raise HTTPException(422, {"code": "UNSUPPORTED_ENTITY", "message": f"지원하는 entity: {list(ENTITIES)}"})
    overrides = None
    if column_overrides:
        try:
            overrides = json.loads(column_overrides)
            if not isinstance(overrides, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in overrides.items()):
                raise ValueError
        except ValueError:
            raise HTTPException(422, {"code": "BAD_COLUMN_OVERRIDES", "message": "column_overrides 는 {필드: 열 이름} JSON 객체여야 합니다."})
    data = await file.read(MAX_BYTES + 1)       # 한도보다 1바이트만 더 읽어 초과를 판별
    try:
        result = run_preview(file.filename or "", data, entity, default_unit, overrides, sheet)
    except FileError as exc:
        raise HTTPException(exc.http_status, {"code": exc.code, "message": exc.message})
    for s in result.sheets:                       # 응답 크기 제한: 전체 검증은 끝났고 표시만 자른다
        if len(s.issues) > MAX_RETURNED_ISSUES:
            s.issues = s.issues[:MAX_RETURNED_ISSUES]
        s.records = s.records[:MAX_RETURNED_RECORDS]
    return result
