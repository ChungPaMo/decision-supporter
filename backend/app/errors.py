"""API 오류 계약. 모든 오류 응답은 {"detail": {"code", "message", "details"?}} 형태이며, 화면은 code 로 분기하고 message 를 그대로 보여줄 수 있다.

code 목록은 OpenAPI(components.schemas.ErrorCode)에 노출된다. 새 코드를 추가하면 docs/api_contract_batches.md 도 갱신한다.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel


class ErrorCode(StrEnum):
    # --- 공통 ---
    REQUEST_INVALID = "REQUEST_INVALID"                    # 422 요청 형식 오류 (details: 필드별 오류)
    UNSUPPORTED_ENTITY = "UNSUPPORTED_ENTITY"              # 422
    BAD_COLUMN_OVERRIDES = "BAD_COLUMN_OVERRIDES"          # 422
    PERSISTENCE_DISABLED = "PERSISTENCE_DISABLED"          # 501 서버에 DB 저장이 켜져 있지 않음 (PERSISTENCE=postgres 필요)
    DB_UNAVAILABLE = "DB_UNAVAILABLE"                      # 503 DB 연결 실패
    PERSISTENCE_ERROR = "PERSISTENCE_ERROR"                # 500 저장 실패 (롤백됨, 상태 변경 없음)
    # --- 파일 (ingest.parsing.FileError 와 동일) ---
    UNSUPPORTED_TYPE = "UNSUPPORTED_TYPE"                  # 415
    FILE_EMPTY = "FILE_EMPTY"                              # 422
    FILE_TOO_LARGE = "FILE_TOO_LARGE"                      # 413
    TOO_MANY_ROWS = "TOO_MANY_ROWS"                        # 413
    XLSX_CORRUPT = "XLSX_CORRUPT"                          # 422
    ENCODING_UNKNOWN = "ENCODING_UNKNOWN"                  # 422
    DELIMITER_UNKNOWN = "DELIMITER_UNKNOWN"                # 422
    NO_VISIBLE_SHEET = "NO_VISIBLE_SHEET"                  # 422
    # --- 배치 ---
    BATCH_NOT_FOUND = "BATCH_NOT_FOUND"                    # 404
    DUPLICATE_UPLOAD = "DUPLICATE_UPLOAD"                  # 409 같은 파일(SHA-256)이 반려되지 않은 배치로 이미 있음 (details.batch_id)
    SOURCE_NOTE_REQUIRED = "SOURCE_NOTE_REQUIRED"          # 422 공개 데이터/합성 데이터는 출처 설명 필수
    VERSION_CONFLICT = "VERSION_CONFLICT"                  # 409 다른 사용자가 먼저 수정함 → 다시 조회 후 재시도
    BATCH_FINALIZED = "BATCH_FINALIZED"                    # 409 이미 승인/반려된 배치는 수정 불가
    # --- 승인·반려 ---
    ACTOR_REQUIRED = "ACTOR_REQUIRED"                      # 422 승인자/반려자 식별 정보 누락·형식 오류 (역할 코드, 실명 금지)
    REJECT_REASON_REQUIRED = "REJECT_REASON_REQUIRED"      # 422
    APPROVAL_BLOCKED_HOLD = "APPROVAL_BLOCKED_HOLD"        # 409 매핑 보류 상태
    APPROVAL_BLOCKED_ERRORS = "APPROVAL_BLOCKED_ERRORS"    # 409 검증 오류 있음
    EMPTY_BATCH = "EMPTY_BATCH"                            # 409 승인할 레코드가 0건
    ALREADY_APPROVED = "ALREADY_APPROVED"                  # 409 중복 승인 (details.approved_by, approved_at)
    BATCH_REJECTED = "BATCH_REJECTED"                      # 409 반려된 배치
    RESULT_CHANGED = "RESULT_CHANGED"                      # 409 화면에서 본 검증 결과와 서버의 현재 결과가 다름 → 다시 조회
    KEYS_ALREADY_APPROVED = "KEYS_ALREADY_APPROVED"        # 409 이미 승인된 (제품, 월) 이 있음 (details.keys). 덮어쓰기 미지원


class ErrorBody(BaseModel):
    code: ErrorCode
    message: str
    details: dict | list | None = None


class ErrorResponse(BaseModel):
    detail: ErrorBody


class ApiError(Exception):
    def __init__(self, status: int, code: ErrorCode, message: str, details: dict | list | None = None) -> None:
        super().__init__(message)
        self.status, self.code, self.message, self.details = status, code, message, details

    def body(self) -> dict:
        d = {"code": self.code.value, "message": self.message}
        if self.details is not None:
            d["details"] = self.details
        return {"detail": d}


ERROR_RESPONSES = {s: {"model": ErrorResponse, "description": "오류 (detail.code 참고)"} for s in (404, 409, 413, 415, 422, 500, 501, 503)}
