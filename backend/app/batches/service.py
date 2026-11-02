"""배치 업로드·재검증·승인·반려 비즈니스 로직. DB 와 FastAPI 를 모르고, BatchRepository 인터페이스만 쓴다.

승인 데이터 흐름 (한 트랜잭션):
  1) 배치 행 잠금(SELECT … FOR UPDATE)  2) 상태·승인자·결과 해시 확인
  3) 저장된 원본 바이트를 다시 파싱·검증해 해시가 같은지 재확인 (화면에서 본 결과 == 지금 승인할 결과)
  4) (월별 수요) 키 중복 확인  5) 배치를 APPROVED 로 변경  6) 업무 행 INSERT (entity 별 테이블)
  어느 단계든 예외가 나면 트랜잭션 전체가 롤백된다 (배치는 승인 전 상태 그대로).

제품 마스터 연결: 업로드·재검증·승인 때마다 승인된 제품 목록을 읽어 검증에 넘긴다. 미등록 제품은 수요 배치의 ERROR(셀 위치 포함)가 되고,
제품 승인 후 재검증하면 풀린다. 승인 시 재검증하므로 그 사이 제품이 바뀌었으면 해시가 달라져 RESULT_CHANGED 로 멈춘다.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from contextlib import contextmanager
from decimal import Decimal

from app.batches.repository import (
    ApprovedDemand, ApprovedProduct, BatchRecord, BatchRepository, BatchTx, DemandRow, DuplicateBatchError, ProductInsert,
    RepositoryError, RepositoryUnavailable, ValidationUpdate,
)
from app.contracts import BatchStatus, SourceType
from app.errors import ApiError, ErrorCode
from app.ingest.mapping import ENTITY_FIELDS
from app.ingest.parsing import FileError
from app.ingest.pipeline import ENTITIES, PreviewOut, ProductRecordOut, RecordOut, result_digest, run_preview, snapshot
from app.ingest.validation import MasterData

ACTOR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{1,59}$")      # 역할 코드 (예: REVIEWER-01). 공백·한글(실명) 불가
MAX_LISTED = 50
FINAL = {BatchStatus.APPROVED.value, BatchStatus.REJECTED.value}


def _check_actor(actor: str | None, what: str) -> str:
    if not actor or not ACTOR_RE.match(actor.strip()):
        raise ApiError(422, ErrorCode.ACTOR_REQUIRED, f"{what} 식별 정보(역할 코드, 예: REVIEWER-01)가 필요합니다. 실명·공백은 사용할 수 없습니다.")
    return actor.strip()


def approval_blockers(b: BatchRecord) -> list[dict]:
    """지금 이 배치를 승인할 수 없는 이유 목록 (화면의 승인 버튼 비활성 사유). 비어 있으면 승인 요청 가능.
    (제품, 월) 키 중복은 승인 요청 시점에만 확인한다."""
    if b.status == BatchStatus.APPROVED.value:
        return [{"code": ErrorCode.ALREADY_APPROVED, "message": "이미 승인된 배치입니다."}]
    if b.status == BatchStatus.REJECTED.value:
        return [{"code": ErrorCode.BATCH_REJECTED, "message": "반려된 배치입니다."}]
    if b.status == BatchStatus.MAPPING_HOLD.value:
        return [{"code": ErrorCode.APPROVAL_BLOCKED_HOLD, "message": "열 매핑이 보류 상태입니다. 열을 지정해 다시 검증하세요."}]
    if b.status == BatchStatus.HAS_ERRORS.value:
        return [{"code": ErrorCode.APPROVAL_BLOCKED_ERRORS, "message": f"검증 오류 {b.blocking_error_count}건이 있습니다. 원본을 고쳐 다시 업로드하거나, 제품 등록이 원인이면 등록 후 다시 검증하세요."}]
    total = sum(s.get("valid_records", 0) for s in (b.validation_result or {}).get("sheets", []) if s.get("status") == "MAPPED")
    if total == 0:
        return [{"code": ErrorCode.EMPTY_BATCH, "message": "승인할 레코드가 없습니다."}]
    return []


class BatchService:
    def __init__(self, repo: BatchRepository) -> None:
        self.repo = repo

    # ------------------------------------------------------------ 트랜잭션 + 저장소 예외 변환
    @contextmanager
    def _tx(self):
        try:
            with self.repo.transaction() as tx:
                yield tx
        except RepositoryUnavailable as exc:
            raise ApiError(503, ErrorCode.DB_UNAVAILABLE, "데이터베이스에 연결할 수 없습니다.") from exc
        except DuplicateBatchError as exc:
            raise ApiError(409, ErrorCode.DUPLICATE_UPLOAD, "같은 파일이 이미 업로드되어 있습니다.") from exc
        except RepositoryError as exc:
            raise ApiError(500, ErrorCode.PERSISTENCE_ERROR, "저장 중 오류가 발생해 모든 변경을 취소했습니다 (상태 변경 없음).") from exc

    @staticmethod
    def _run(tx: BatchTx, filename, data, entity, default_unit, overrides, sheet) -> PreviewOut:
        master = MasterData({p.product_id: p for p in tx.list_known_products()})
        try:
            return run_preview(filename, data, entity, default_unit, overrides or None, sheet, master)
        except FileError as exc:
            raise ApiError(exc.http_status, ErrorCode(exc.code), exc.message) from exc

    @staticmethod
    def _settings(default_unit, overrides, sheet) -> dict:
        return {"default_unit": default_unit, "column_overrides": overrides or {}, "sheet": sheet}

    @staticmethod
    def _check_overrides(overrides: dict | None, entity: str) -> None:
        fields = ENTITY_FIELDS[entity]
        bad = sorted(set(overrides or {}) - set(fields))
        if bad:
            raise ApiError(422, ErrorCode.BAD_COLUMN_OVERRIDES, f"알 수 없는 필드: {bad}. 허용: {sorted(fields)}")

    # ------------------------------------------------------------ 업로드
    def create_batch(self, filename: str, data: bytes, *, entity: str = "monthly_demand", default_unit: str | None = None,
                     column_overrides: dict | None = None, sheet: str | None = None, source_type: str = SourceType.CLIENT_UPLOAD.value,
                     source_note: str | None = None, created_by: str | None = None) -> BatchRecord:
        if entity not in ENTITIES:
            raise ApiError(422, ErrorCode.UNSUPPORTED_ENTITY, f"지원하는 entity: {list(ENTITIES)}")
        if source_type not in {s.value for s in SourceType}:
            raise ApiError(422, ErrorCode.REQUEST_INVALID, f"source_type 은 {[s.value for s in SourceType]} 중 하나여야 합니다.")
        if source_type != SourceType.CLIENT_UPLOAD.value and not (source_note or "").strip():
            raise ApiError(422, ErrorCode.SOURCE_NOTE_REQUIRED, "공개 수요 데이터·합성 데이터는 출처(원 출처·라이선스 또는 생성 방법) 설명이 필요합니다.")
        if created_by is not None:
            created_by = _check_actor(created_by, "업로더")
        self._check_overrides(column_overrides, entity)
        with self._tx() as tx:
            preview = self._run(tx, filename, data, entity, default_unit, column_overrides, sheet)
            digest = result_digest(preview, self._settings(default_unit, column_overrides, sheet))
            rec = BatchRecord(
                batch_id=uuid.uuid4(), entity=entity, status=preview.status, source_type=source_type, source_name=preview.file.name,
                source_note=(source_note or None), source_sha256=preview.file.sha256, created_by=created_by,
                created_at=dt.datetime.now(dt.timezone.utc), default_unit=default_unit, column_overrides=column_overrides or {},
                sheet_filter=sheet, blocking_error_count=preview.blocking_error_count, warning_count=preview.warning_count,
                result_digest=digest, validation_result=snapshot(preview))
            dup = tx.find_live_by_sha(rec.source_sha256, entity)
            if dup is not None:
                raise ApiError(409, ErrorCode.DUPLICATE_UPLOAD, "같은 파일이 이미 업로드되어 있습니다.",
                               {"batch_id": str(dup.batch_id), "status": dup.status})
            tx.insert_batch(rec, data)
        return rec

    # ------------------------------------------------------------ 조회
    def get_batch(self, batch_id: uuid.UUID) -> BatchRecord:
        with self._tx() as tx:
            b = tx.get_batch(batch_id)
        if b is None:
            raise ApiError(404, ErrorCode.BATCH_NOT_FOUND, "배치를 찾을 수 없습니다.")
        return b

    def list_batches(self, status: str | None, limit: int, offset: int):
        with self._tx() as tx:
            return tx.list_batches(status, limit, offset)

    def list_approved_demand(self, product_id: str | None, period_from: str | None, period_to: str | None,
                             limit: int, offset: int) -> tuple[list[ApprovedDemand], int]:
        with self._tx() as tx:
            return tx.list_approved_demand(product_id, period_from, period_to, limit, offset)

    def list_products(self, product_id: str | None, q: str | None, limit: int, offset: int) -> tuple[list[ApprovedProduct], int]:
        with self._tx() as tx:
            return tx.list_products(product_id, q, limit, offset)

    # ------------------------------------------------------------ 사용자 수정 → 재검증
    def revalidate(self, batch_id: uuid.UUID, *, expected_version: int, column_overrides: dict | None,
                   default_unit: str | None, sheet: str | None) -> BatchRecord:
        """설정 3종(열 지정, 기본 단위, 시트)을 통째로 교체하고 저장된 원본을 다시 검증한다. 원본 바이트는 바뀌지 않는다."""
        with self._tx() as tx:
            b = self._locked(tx, batch_id)
            self._check_overrides(column_overrides, b.entity)
            if b.status in FINAL:
                raise ApiError(409, ErrorCode.BATCH_FINALIZED, "이미 승인 또는 반려된 배치는 수정할 수 없습니다.", {"status": b.status})
            if b.version != expected_version:
                raise ApiError(409, ErrorCode.VERSION_CONFLICT, "다른 변경이 먼저 반영되었습니다. 다시 조회한 뒤 시도하세요.", {"current_version": b.version})
            preview = self._run(tx, b.source_name, tx.get_file(batch_id), b.entity, default_unit, column_overrides, sheet)
            digest = result_digest(preview, self._settings(default_unit, column_overrides, sheet))
            return tx.update_validation(batch_id, ValidationUpdate(
                status=preview.status, default_unit=default_unit, column_overrides=column_overrides or {}, sheet_filter=sheet,
                blocking_error_count=preview.blocking_error_count, warning_count=preview.warning_count,
                result_digest=digest, validation_result=snapshot(preview)))

    # ------------------------------------------------------------ 승인
    def approve(self, batch_id: uuid.UUID, *, approved_by: str | None, expected_result_digest: str) -> BatchRecord:
        actor = _check_actor(approved_by, "승인자")
        with self._tx() as tx:
            b = self._locked(tx, batch_id)
            if b.status == BatchStatus.APPROVED.value:
                raise ApiError(409, ErrorCode.ALREADY_APPROVED, "이미 승인된 배치입니다. 다시 반영하지 않습니다.",
                               {"approved_by": b.approved_by, "approved_at": b.approved_at.isoformat() if b.approved_at else None})
            blockers = approval_blockers(b)
            if blockers:
                code = blockers[0]["code"]
                raise ApiError(409, code, blockers[0]["message"], {"status": b.status})
            if b.result_digest != expected_result_digest:
                raise ApiError(409, ErrorCode.RESULT_CHANGED, "화면에서 본 검증 결과와 현재 결과가 다릅니다. 다시 조회한 뒤 확인하고 승인하세요.",
                               {"current_result_digest": b.result_digest})
            # 저장된 원본을 다시 검증한다. 요약(validation_result)은 신뢰하지 않는다.
            settings = self._settings(b.default_unit, b.column_overrides, b.sheet_filter)
            preview = self._run(tx, b.source_name, tx.get_file(batch_id), b.entity, b.default_unit, b.column_overrides, b.sheet_filter)
            if preview.status != BatchStatus.READY_FOR_APPROVAL.value or result_digest(preview, settings) != b.result_digest:
                raise ApiError(409, ErrorCode.RESULT_CHANGED,
                               "저장된 원본을 다시 검증한 결과가 기존 결과와 다릅니다 (검증 규칙이 바뀌었을 수 있음). 다시 검증한 뒤 승인하세요.",
                               {"recomputed_status": preview.status})
            records = [r for s in preview.sheets if s.status == "MAPPED" for r in s.records]
            if not records:
                raise ApiError(409, ErrorCode.EMPTY_BATCH, "승인할 레코드가 없습니다.")
            if b.entity == "product":
                written = self._commit_products(tx, batch_id, actor, records)
            else:
                written = self._commit_demand(tx, batch_id, actor, records)
            if written != len(records):                   # 방어: 어긋나면 예외 → 전체 롤백
                raise RepositoryError(f"expected {len(records)} rows, wrote {written}")
            return tx.get_batch(batch_id)

    @staticmethod
    def _commit_products(tx: BatchTx, batch_id: uuid.UUID, actor: str, records: list[ProductRecordOut]) -> int:
        tx.mark_approved(batch_id, actor, len(records))
        return tx.insert_products(batch_id, [ProductInsert(r.product_id, r.product_name, r.base_unit, r.product_type, Decimal(r.safety_stock_qty))
                                             for r in records])

    @staticmethod
    def _commit_demand(tx: BatchTx, batch_id: uuid.UUID, actor: str, records: list[RecordOut]) -> int:
        clash = tx.existing_demand_keys(sorted({(r.product_id, r.period) for r in records}))
        if clash:
            raise ApiError(409, ErrorCode.KEYS_ALREADY_APPROVED, f"이미 승인된 (제품, 월)이 {len(clash)}건 있습니다. 덮어쓰기는 지원하지 않습니다.",
                           {"keys": [{"product_id": p, "period": m} for p, m in clash[:MAX_LISTED]], "total": len(clash)})
        tx.mark_approved(batch_id, actor, len(records))
        return tx.insert_monthly_demand(batch_id, [DemandRow(r.product_id, r.period, Decimal(r.demand_qty), r.unit) for r in records])

    # ------------------------------------------------------------ 반려
    def reject(self, batch_id: uuid.UUID, *, rejected_by: str | None, reason: str | None) -> BatchRecord:
        actor = _check_actor(rejected_by, "반려자")
        if not reason or not reason.strip():
            raise ApiError(422, ErrorCode.REJECT_REASON_REQUIRED, "반려 사유를 입력하세요.")
        with self._tx() as tx:
            b = self._locked(tx, batch_id)
            if b.status == BatchStatus.APPROVED.value:
                raise ApiError(409, ErrorCode.ALREADY_APPROVED, "승인된 배치는 반려할 수 없습니다.", {"status": b.status})
            if b.status == BatchStatus.REJECTED.value:
                raise ApiError(409, ErrorCode.BATCH_REJECTED, "이미 반려된 배치입니다.", {"status": b.status})
            return tx.mark_rejected(batch_id, actor, reason.strip()[:500])

    @staticmethod
    def _locked(tx: BatchTx, batch_id: uuid.UUID) -> BatchRecord:
        b = tx.get_batch(batch_id, lock=True)
        if b is None:
            raise ApiError(404, ErrorCode.BATCH_NOT_FOUND, "배치를 찾을 수 없습니다.")
        return b
