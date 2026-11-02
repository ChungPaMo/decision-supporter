"""배치 API: 업로드 보관 → 검증 결과 조회 → 열 지정·재검증 → 승인/반려 → 승인된 제품·월별 수요 조회.
제품 마스터는 별도 업로드 API 없이 entity=product 로 같은 배치 흐름을 탄다.

PERSISTENCE=disabled(기본)이면 모든 엔드포인트가 501 PERSISTENCE_DISABLED 를 돌려준다 — 영구 저장이 되는 것처럼 동작하지 않는다.
PERSISTENCE=postgres 경로(SqlBatchRepository)는 Docker 개발 PC 에서 검증하기 전까지 미검증이다.
POST /ingest/preview 는 저장 없이 검증만 하는 별도 API 로 그대로 유지된다.
"""

from __future__ import annotations

import json
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile

from app.batches.schemas import (
    ApproveRequest, BatchDetail, BatchPage, DemandPage, ProductPage, RejectRequest, RevalidateRequest, to_demand_item, to_detail,
    to_product_item, to_summary,
)
from app.batches.service import BatchService
from app.config import get_settings
from app.contracts import BatchStatus
from app.errors import ERROR_RESPONSES, ApiError, ErrorCode
from app.ingest.parsing import MAX_BYTES

router = APIRouter(tags=["batches"], responses=ERROR_RESPONSES)


def get_service() -> BatchService:
    """테스트는 app.dependency_overrides[get_service] 로 인메모리 저장소를 주입한다."""
    if get_settings().persistence != "postgres":
        raise ApiError(501, ErrorCode.PERSISTENCE_DISABLED,
                       "이 서버는 배치 저장이 꺼져 있습니다 (PERSISTENCE=disabled). 검증만 필요하면 POST /ingest/preview 를 사용하세요.")
    from app.batches.sql_repository import SqlBatchRepository
    from app.db import get_engine
    return BatchService(SqlBatchRepository(get_engine()))


Service = Annotated[BatchService, Depends(get_service)]


@router.post("/batches", response_model=BatchDetail, status_code=201, summary="파일 업로드 → 원본 보관 + 검증 (승인 전, 업무 테이블 반영 없음)")
async def create_batch(
    svc: Service,
    file: UploadFile = File(...),
    entity: str = Form("monthly_demand", description="monthly_demand | product"),
    default_unit: str | None = Form(None, description="월별 수요 파일에 단위 열이 없을 때 적용 (제품 마스터는 기준 단위 열 필수)"),
    column_overrides: str | None = Form(None, description='JSON 객체. 예: {"demand_qty": "C"}'),
    sheet: str | None = Form(None),
    source_type: str = Form("CLIENT_UPLOAD", description="CLIENT_UPLOAD | PUBLIC_DEMAND | SYNTHETIC (후자 둘은 source_note 필수)"),
    source_note: str | None = Form(None),
    created_by: str | None = Form(None, description="업로더 역할 코드(선택)"),
) -> BatchDetail:
    overrides = None
    if column_overrides:
        try:
            overrides = json.loads(column_overrides)
            if not isinstance(overrides, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in overrides.items()):
                raise ValueError
        except ValueError:
            raise ApiError(422, ErrorCode.BAD_COLUMN_OVERRIDES, "column_overrides 는 {필드: 열 이름} JSON 객체여야 합니다.")
    data = await file.read(MAX_BYTES + 1)
    rec = svc.create_batch(file.filename or "", data, entity=entity, default_unit=default_unit, column_overrides=overrides, sheet=sheet,
                           source_type=source_type, source_note=source_note, created_by=created_by)
    return to_detail(rec)


@router.get("/batches", response_model=BatchPage, summary="배치 목록 (최신순)")
def list_batches(svc: Service, status: BatchStatus | None = None, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)) -> BatchPage:
    items, total = svc.list_batches(status.value if status else None, limit, offset)
    return BatchPage(items=[to_summary(b) for b in items], total=total, limit=limit, offset=offset)


@router.get("/batches/{batch_id}", response_model=BatchDetail, summary="배치 상태·검증 결과 조회")
def get_batch(batch_id: uuid.UUID, svc: Service) -> BatchDetail:
    return to_detail(svc.get_batch(batch_id))


@router.post("/batches/{batch_id}/revalidate", response_model=BatchDetail, summary="열 지정·기본 단위 변경 후 재검증 (원본은 불변)")
def revalidate(batch_id: uuid.UUID, body: RevalidateRequest, svc: Service) -> BatchDetail:
    return to_detail(svc.revalidate(batch_id, expected_version=body.expected_version, column_overrides=body.column_overrides,
                                    default_unit=body.default_unit, sheet=body.sheet))


@router.post("/batches/{batch_id}/approve", response_model=BatchDetail,
             summary="명시적 승인 → 배치 entity 의 업무 테이블(product 또는 monthly_demand) 반영 (한 트랜잭션)")
def approve(batch_id: uuid.UUID, body: ApproveRequest, svc: Service) -> BatchDetail:
    return to_detail(svc.approve(batch_id, approved_by=body.approved_by, expected_result_digest=body.expected_result_digest))


@router.post("/batches/{batch_id}/reject", response_model=BatchDetail, summary="반려 (최종 상태)")
def reject(batch_id: uuid.UUID, body: RejectRequest, svc: Service) -> BatchDetail:
    return to_detail(svc.reject(batch_id, rejected_by=body.rejected_by, reason=body.reason))


@router.get("/products", response_model=ProductPage, summary="승인된 제품 마스터 조회 (product_id 정확히 일치, q 는 품명 부분 일치)")
def list_products(svc: Service, product_id: str | None = None, q: str | None = Query(None, min_length=1, max_length=100),
                  limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0)) -> ProductPage:
    items, total = svc.list_products(product_id, q, limit, offset)
    return ProductPage(items=[to_product_item(i) for i in items], total=total, limit=limit, offset=offset)


@router.get("/demand/monthly", response_model=DemandPage, summary="승인된 월별 수요 조회 (예측 모듈 입력과 같은 원천)")
def list_monthly_demand(svc: Service, product_id: str | None = None, period_from: str | None = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
                        period_to: str | None = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
                        limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0)) -> DemandPage:
    items, total = svc.list_approved_demand(product_id, period_from, period_to, limit, offset)
    return DemandPage(items=[to_demand_item(i) for i in items], total=total, limit=limit, offset=offset)
