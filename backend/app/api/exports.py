"""계획 결과 Excel 다운로드 (DEMO/UNVERIFIED).

제출된 JSON 을 XLSX 로 옮기기만 한다. 예측·계획 계산도, 승인 여부 확인도, DB 접근도 없다.
저장된 계획안의 승인을 확인하는 정식 출력은 계획 저장 기능이 생긴 뒤 별도 API 로 구현한다.
"""

from fastapi import APIRouter
from fastapi.responses import Response

from app.errors import ERROR_RESPONSES, ApiError, ErrorCode
from app.exports.contract import PlanExportRequest
from app.exports.xlsx import EXPORT_STATUS, MEDIA_TYPE, ExportTooLarge, build_workbook, content_disposition

router = APIRouter(tags=["exports"])


@router.post(
    "/exports/plans/xlsx",
    summary="계획 결과 JSON → XLSX (DEMO/UNVERIFIED, 공식 문서 아님)",
    response_class=Response,
    responses={
        200: {"description": "XLSX 파일. 파일명·시트·인쇄 머리글에 DEMO/UNVERIFIED 가 표시된다",
              "content": {MEDIA_TYPE: {"schema": {"type": "string", "format": "binary"}}},
              "headers": {"Content-Disposition": {"schema": {"type": "string"}, "description": "attachment; filename=...; filename*=UTF-8''..."},
                          "X-Export-Status": {"schema": {"type": "string", "enum": [EXPORT_STATUS]}}}},
        413: ERROR_RESPONSES[413], 422: ERROR_RESPONSES[422],
    },
)
def export_plan(req: PlanExportRequest) -> Response:
    try:
        data = build_workbook(req)
    except ExportTooLarge as exc:
        raise ApiError(413, ErrorCode.EXPORT_TOO_LARGE, f"출력 행이 너무 많습니다 (최대 {exc.limit}행).", {"rows": exc.rows, "max_rows": exc.limit}) from exc
    return Response(content=data, media_type=MEDIA_TYPE, headers={
        "Content-Disposition": content_disposition(req.export_name),
        "X-Export-Status": EXPORT_STATUS,
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "no-store",
    })
