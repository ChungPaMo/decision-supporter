"""파싱 → 매핑 → 검증을 한 번에 실행하고 API 응답 계약(pydantic)으로 변환한다. DB 를 쓰지 않는다.

승인·저장은 이 모듈의 책임이 아니다. 응답의 approval_required 는 항상 True, auto_approve 는 항상 False.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from app.ingest.mapping import map_sheet
from app.ingest.parsing import ParsedFile, parse_file
from app.ingest.validation import validate_sheet

ENTITIES = ("monthly_demand",)


class LocationOut(BaseModel):
    sheet: str
    row: int
    col: int | None = None
    cell_ref: str | None = None      # 예: C5


class IssueOut(BaseModel):
    severity: Literal["ERROR", "WARNING"]
    code: str
    message: str
    field: str | None = None
    location: LocationOut | None = None
    raw_value: str | None = None
    evidence: dict = {}


class FieldMappingOut(BaseModel):
    field: str
    col: int
    header: str
    method: Literal["ALIAS_STRONG", "ALIAS_WEAK", "USER_OVERRIDE"]
    review_required: bool


class MappingHoldOut(BaseModel):
    code: str
    message: str
    field: str | None = None
    candidates: list[str] = []


class RecordOut(BaseModel):
    product_id: str
    product_name: str | None
    period: str
    demand_qty: str                  # 10진 문자열 (float 금지)
    unit: str
    location: LocationOut
    review_required: bool


class SheetOut(BaseModel):
    name: str
    status: Literal["MAPPED", "HOLD", "IGNORED"]
    header_rows: list[int]
    data_rows: int
    mapping: list[FieldMappingOut]
    unmapped_headers: list[str]
    holds: list[MappingHoldOut]
    validated: bool
    error_rows: int
    valid_records: int
    issues: list[IssueOut]
    records: list[RecordOut]


class FileOut(BaseModel):
    name: str
    format: str
    size_bytes: int
    sha256: str
    meta: dict


class PreviewOut(BaseModel):
    entity: str
    file: FileOut
    status: Literal["READY_FOR_APPROVAL", "HAS_ERRORS", "MAPPING_HOLD"]
    approval_required: Literal[True] = True
    auto_approve: Literal[False] = False
    blocking_error_count: int
    warning_count: int
    sheets: list[SheetOut]


def _loc(l) -> LocationOut | None:
    return None if l is None else LocationOut(sheet=l.sheet, row=l.row, col=l.col, cell_ref=l.cell_ref)


def run_preview(filename: str, data: bytes, entity: str = "monthly_demand", default_unit: str | None = None,
                column_overrides: dict[str, str] | None = None, sheet: str | None = None) -> PreviewOut:
    """FileError 는 그대로 올라간다(API 가 변환)."""
    if entity not in ENTITIES:
        raise ValueError(f"지원하지 않는 entity: {entity}")
    parsed: ParsedFile = parse_file(filename, data)
    sheets_out: list[SheetOut] = []
    errors = warnings = 0
    for ps in parsed.sheets:
        if sheet is not None and ps.name != sheet:
            continue
        m = map_sheet(ps, column_overrides)
        v = validate_sheet(ps, m, default_unit) if m.status == "MAPPED" else None
        issues = [IssueOut(severity=i.severity, code=i.code, message=i.message, field=i.field, location=_loc(i.location),
                           raw_value=i.raw_value, evidence=i.evidence) for i in (v.issues if v else [])]
        errors += sum(i.severity == "ERROR" for i in issues)
        warnings += sum(i.severity == "WARNING" for i in issues)
        sheets_out.append(SheetOut(
            name=ps.name, status=m.status, header_rows=ps.header_rows, data_rows=len(ps.rows),
            mapping=[FieldMappingOut(field=f.field, col=f.col, header=f.header, method=f.method, review_required=f.review_required) for f in m.fields.values()],
            unmapped_headers=m.unmapped_headers,
            holds=[MappingHoldOut(code=h.code, message=h.message, field=h.field, candidates=h.candidates) for h in m.holds],
            validated=v is not None, error_rows=v.error_rows if v else 0, valid_records=len(v.records) if v else 0, issues=issues,
            records=[RecordOut(product_id=r.product_id, product_name=r.product_name, period=r.period, demand_qty=format(r.demand_qty, "f"),
                               unit=r.unit, location=_loc(r.location), review_required=r.review_required) for r in (v.records if v else [])],
        ))
    usable = [s for s in sheets_out if s.status == "MAPPED"]
    holds = [s for s in sheets_out if s.status == "HOLD"]
    if holds or not usable:
        status = "MAPPING_HOLD"          # 매핑이 불확실한 시트가 하나라도 있으면 승인 요청 불가 (조용히 건너뛰지 않는다)
    elif errors:
        status = "HAS_ERRORS"
    else:
        status = "READY_FOR_APPROVAL"
    return PreviewOut(entity=entity, file=FileOut(name=parsed.filename, format=parsed.format, size_bytes=parsed.size_bytes, sha256=parsed.sha256, meta=parsed.meta),
                      status=status, blocking_error_count=errors, warning_count=warnings, sheets=sheets_out)
