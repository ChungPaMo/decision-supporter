"""배치 API 요청·응답 계약 (OpenAPI 로 노출). 화면은 status, approval_blockers[].code, 오류 detail.code 로 분기한다."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Literal

from pydantic import BaseModel, Field

from app.batches.repository import ApprovedDemand, ApprovedProduct, BatchRecord
from app.batches.service import approval_blockers
from app.contracts import BatchStatus, SourceType
from app.errors import ErrorCode
from app.ingest.pipeline import PreviewOut


class Blocker(BaseModel):
    code: ErrorCode
    message: str


class FileInfo(BaseModel):
    name: str
    sha256: str
    size_bytes: int | None = None
    format: str | None = None


class BatchSettings(BaseModel):
    default_unit: str | None = None
    column_overrides: dict[str, str] = {}
    sheet: str | None = None


class BatchSummary(BaseModel):
    batch_id: uuid.UUID
    entity: Literal["monthly_demand", "product"]
    status: BatchStatus = Field(description="MAPPING_HOLD → 열 지정 필요 / HAS_ERRORS → 원본 수정 후 재업로드 / READY_FOR_APPROVAL → 승인 가능 / APPROVED, REJECTED → 최종")
    source_type: SourceType
    source_note: str | None
    file: FileInfo
    version: int = Field(description="재검증 요청 시 expected_version 으로 되돌려 보낸다 (동시 수정 방지)")
    created_by: str | None
    created_at: dt.datetime
    settings: BatchSettings
    blocking_error_count: int
    warning_count: int
    result_digest: str | None = Field(description="현재 검증 결과의 지문. 승인 요청의 expected_result_digest 로 되돌려 보낸다")
    approval_blockers: list[Blocker] = Field(description="비어 있으면 승인 요청 가능. (제품, 월) 키 중복은 승인 요청 시점에 확인되어 여기에는 없다")
    approved_by: str | None = None
    approved_at: dt.datetime | None = None
    approved_record_count: int | None = None
    rejected_by: str | None = None
    rejected_at: dt.datetime | None = None
    reject_reason: str | None = None


class BatchDetail(BatchSummary):
    validation: PreviewOut | None = Field(description="검증 결과 요약. 시트별 issues·records 는 앞부분만 담기며 전체 건수는 issues_total, records_total")


class BatchPage(BaseModel):
    items: list[BatchSummary]
    total: int
    limit: int
    offset: int


class RevalidateRequest(BaseModel):
    expected_version: int = Field(ge=1, description="조회한 배치의 version")
    column_overrides: dict[str, str] = Field(default={}, description="{필드: 헤더 이름 또는 열 문자}. 설정 전체를 교체한다(병합 아님)")
    default_unit: str | None = None
    sheet: str | None = None


class ApproveRequest(BaseModel):
    approved_by: str = Field(description="승인자 역할 코드 (예: REVIEWER-01). 실명·공백 불가")
    expected_result_digest: str = Field(min_length=64, max_length=64, description="화면에서 확인한 배치의 result_digest")


class RejectRequest(BaseModel):
    rejected_by: str = Field(description="반려자 역할 코드")
    reason: str = Field(min_length=1, max_length=500)


class DemandItem(BaseModel):
    product_id: str
    period: str = Field(description="YYYY-MM")
    demand_qty: str = Field(description="10진 문자열")
    unit: str
    source_type: SourceType
    batch_id: uuid.UUID


class DemandPage(BaseModel):
    items: list[DemandItem]
    total: int
    limit: int
    offset: int


class ProductItem(BaseModel):
    product_id: str
    product_name: str
    base_unit: str
    product_type: str | None
    safety_stock_qty: str = Field(description="10진 문자열")
    source_type: SourceType
    batch_id: uuid.UUID


class ProductPage(BaseModel):
    items: list[ProductItem]
    total: int
    limit: int
    offset: int


def to_summary(b: BatchRecord) -> BatchSummary:
    f = (b.validation_result or {}).get("file", {})
    return BatchSummary(
        batch_id=b.batch_id, entity=b.entity, status=b.status, source_type=b.source_type, source_note=b.source_note,
        file=FileInfo(name=b.source_name, sha256=b.source_sha256, size_bytes=f.get("size_bytes"), format=f.get("format")),
        version=b.version, created_by=b.created_by, created_at=b.created_at,
        settings=BatchSettings(default_unit=b.default_unit, column_overrides=b.column_overrides or {}, sheet=b.sheet_filter),
        blocking_error_count=b.blocking_error_count, warning_count=b.warning_count, result_digest=b.result_digest,
        approval_blockers=[Blocker(**x) for x in approval_blockers(b)],
        approved_by=b.approved_by, approved_at=b.approved_at, approved_record_count=b.approved_record_count,
        rejected_by=b.rejected_by, rejected_at=b.rejected_at, reject_reason=b.reject_reason)


def to_detail(b: BatchRecord) -> BatchDetail:
    v = PreviewOut.model_validate(b.validation_result) if b.validation_result else None
    return BatchDetail(**to_summary(b).model_dump(), validation=v)


def to_product_item(p: ApprovedProduct) -> ProductItem:
    return ProductItem(product_id=p.product_id, product_name=p.product_name, base_unit=p.base_unit, product_type=p.product_type,
                       safety_stock_qty=format(p.safety_stock_qty, "f"), source_type=p.source_type, batch_id=p.batch_id)


def to_demand_item(d: ApprovedDemand) -> DemandItem:
    return DemandItem(product_id=d.product_id, period=d.period, demand_qty=format(d.demand_qty, "f"), unit=d.unit,
                      source_type=d.source_type, batch_id=d.batch_id)
