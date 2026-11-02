"""계획 결과 → XLSX. 입력을 그대로 표시만 한다: 합계·부족량·순위를 계산하지 않고, 없는 값은 '미산출' 로 둔다.

현재 출력은 전부 DEMO/UNVERIFIED 다. 요청자가 낸 JSON 을 검증 없이 옮긴 것이므로 모든 시트 첫 행·인쇄 머리글·파일명·문서 속성에 표시하며,
요청으로 이 표시를 끌 수 없다. 저장된 계획안의 승인 여부를 확인하는 정식 출력은 별도 API 로 구현한다.
"""

from __future__ import annotations

import io
import re
import unicodedata
from decimal import Decimal
from urllib.parse import quote

from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from app.exports.contract import EXPORT_CONTRACT_VERSION, PlanExportRequest

MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
EXPORT_STATUS = "DEMO_UNVERIFIED"
BANNER = "DEMO / UNVERIFIED — 승인된 공식 계획이 아닙니다. 제출된 JSON 을 서버가 검증 없이 그대로 표시한 출력입니다."
MISSING = "미산출"
MAX_ROWS = 50_000
MAX_EXCEL_DIGITS = 15             # Excel 숫자는 유효숫자 15자리까지 정확하다. 넘으면 문자열로 보존

SHEETS = ("계획안 비교", "제품별 생산계획", "자재 소요량 및 부족량", "적용 제약조건", "계산 근거·데이터 출처")
PLAN_LABELS = {"BASELINE": "기준형", "STABLE": "안정형", "COST_SAVING": "비용절감형"}
PLAN_ORDER = ("BASELINE", "STABLE", "COST_SAVING")
PRODUCTION_COLUMNS = ("계획안", "제품코드", "제품명", "기간", "계획수량", "단위", "수요기준량", "예측 기준", "생산구분", "기말재고", "비고")
MATERIAL_COLUMNS = ("계획안", "자재코드", "자재명", "기간", "소요량", "가용량", "부족량", "단위", "비고")
CONSTRAINT_COLUMNS = ("계획안", "제약 ID", "제약 항목", "차원", "강도", "상태", "자원", "기간", "한도", "사용량", "단위", "출처", "비고")
EVIDENCE_COLUMNS = ("출처 구분", "이름", "배치 ID", "기준 시점", "설명")

_BANNER_FILL = PatternFill("solid", fgColor="C00000")
_HEADER_FILL = PatternFill("solid", fgColor="D9D9D9")
_MISSING_FONT = Font(color="808080", italic=True)
_FORMULA_PREFIXES = tuple("=+-@\t\r\n＝＋－＠")


class ExportTooLarge(Exception):
    def __init__(self, rows: int, limit: int) -> None:
        super().__init__(f"{rows} > {limit}")
        self.rows, self.limit = rows, limit


def safe_text(value: str) -> str:
    """셀 문자열 무력화: 제어 문자 제거, 수식 시작 문자는 작은따옴표를 앞에 붙인다.
    셀 타입도 문자열로 고정하지만, 다른 프로그램으로 옮기거나 CSV 로 저장해도 수식이 되지 않게 문자 자체를 바꾼다."""
    text = ILLEGAL_CHARACTERS_RE.sub("", value)
    return "'" + text if text.lstrip().startswith(_FORMULA_PREFIXES) else text      # 제어 문자를 지운 뒤 앞 공백만 남는 경우까지 본다


def _put_text(ws: Worksheet, row: int, col: int, value: str | None):
    cell = ws.cell(row=row, column=col)
    if value is None:
        cell.value = MISSING
        cell.font = _MISSING_FONT
    else:
        cell.value = safe_text(value)
    cell.data_type = "s"            # '=' 로 시작해도 openpyxl 이 수식으로 해석하지 않게 고정
    return cell


def _put_number(ws: Worksheet, row: int, col: int, value: str | None):
    """10진 문자열을 표시 자릿수까지 그대로 숫자 셀로. 15자리를 넘으면 값이 바뀌지 않도록 문자열로 둔다."""
    if value is None:
        return _put_text(ws, row, col, None)
    d = Decimal(value)
    exponent = d.as_tuple().exponent
    if len(d.as_tuple().digits) > MAX_EXCEL_DIGITS:
        cell = ws.cell(row=row, column=col, value=value)
        cell.data_type = "s"
    else:
        cell = ws.cell(row=row, column=col, value=d)
        cell.number_format = "#,##0" + ("." + "0" * -exponent if exponent < 0 else "")
    cell.alignment = Alignment(horizontal="right")
    return cell


def _start_sheet(ws: Worksheet, req: PlanExportRequest, columns: tuple[str, ...]) -> int:
    """공통 머리: 1행 DEMO 표시, 2행 제목·구분·기간, 4행 열 제목. 데이터 시작 행을 돌려준다."""
    ws.sheet_properties.tabColor = "C00000"
    ws.oddHeader.center.text = "DEMO / UNVERIFIED"
    ws.oddFooter.center.text = "승인된 공식 계획 아님"
    last = get_column_letter(max(len(columns), 2))
    ws.merge_cells(f"A1:{last}1")
    banner = ws["A1"]
    banner.value = BANNER
    banner.font = Font(bold=True, color="FFFFFF")
    banner.fill = _BANNER_FILL
    banner.alignment = Alignment(wrap_text=True, vertical="center")
    ws.row_dimensions[1].height = 32
    ws.merge_cells(f"A2:{last}2")
    _put_text(ws, 2, 1, f"{req.title} | 데이터 구분: {req.data_classification} | 기간: {req.horizon.period_from} ~ {req.horizon.period_to}")
    for c, name in enumerate(columns, start=1):
        cell = ws.cell(row=4, column=c, value=name)
        cell.font = Font(bold=True)
        cell.fill = _HEADER_FILL
        ws.column_dimensions[get_column_letter(c)].width = max(12, min(30, len(name) * 3))
    ws.freeze_panes = "A5"
    return 5


def _basis_label(req: PlanExportRequest) -> str | None:
    return req.forecast_basis.quantile if req.forecast_basis else None


def _ordered(req: PlanExportRequest):
    return sorted(req.plans, key=lambda p: PLAN_ORDER.index(p.plan_type))


def consistency_warnings(req: PlanExportRequest) -> list[str]:
    """입력끼리 모순되는 표시. 값을 고치지 않고 보는 사람에게 알리기만 한다."""
    out = []
    for p in _ordered(req):
        bad = [c.constraint_id for c in p.constraints if c.strength == "HARD" and c.status != "SATISFIED"]
        if p.feasibility == "FEASIBLE" and bad:
            out.append(f"{PLAN_LABELS[p.plan_type]}: FEASIBLE 로 제출됐지만 충족되지 않은 HARD 제약이 있습니다: {', '.join(bad[:10])}")
    return out


def _comparison(ws: Worksheet, req: PlanExportRequest) -> None:
    plans = _ordered(req)
    row = _start_sheet(ws, req, ("항목", "단위", *(PLAN_LABELS[p.plan_type] for p in plans)))
    _put_text(ws, row, 1, "계획안 ID")
    for c, p in enumerate(plans, start=3):
        _put_text(ws, row, c, p.plan_id)
    row += 1
    _put_text(ws, row, 1, "실행 가능성")
    for c, p in enumerate(plans, start=3):
        _put_text(ws, row, c, p.feasibility)
    row += 1
    # 같은 코드라도 단위가 다르면 환산하지 않고 다른 행으로 둔다
    keys: dict[tuple[str, str | None], str] = {}
    for p in plans:
        for k in p.kpis:
            keys.setdefault((k.code, k.unit), k.label)
    for (code, unit), label in keys.items():
        _put_text(ws, row, 1, label)
        _put_text(ws, row, 2, unit)
        for c, p in enumerate(plans, start=3):
            hit = next((k for k in p.kpis if (k.code, k.unit) == (code, unit)), None)
            _put_number(ws, row, c, hit.value if hit else None)
        row += 1
    for message in consistency_warnings(req):
        row += 1
        cell = _put_text(ws, row, 1, "입력 불일치 경고: " + message)
        cell.font = Font(bold=True, color="C00000")


def _production(ws: Worksheet, req: PlanExportRequest) -> None:
    row = _start_sheet(ws, req, PRODUCTION_COLUMNS)
    basis = _basis_label(req)
    for p in _ordered(req):
        for x in p.production:
            for c, v in enumerate((PLAN_LABELS[p.plan_type], x.product_id, x.product_name, x.period), start=1):
                _put_text(ws, row, c, v)
            _put_number(ws, row, 5, x.planned_qty)
            _put_text(ws, row, 6, x.unit)
            _put_number(ws, row, 7, x.demand_qty)
            _put_text(ws, row, 8, basis)
            _put_text(ws, row, 9, x.production_mode)
            _put_number(ws, row, 10, x.ending_inventory_qty)
            _put_text(ws, row, 11, x.note)
            row += 1


def _materials(ws: Worksheet, req: PlanExportRequest) -> None:
    row = _start_sheet(ws, req, MATERIAL_COLUMNS)
    for p in _ordered(req):
        for x in p.materials:
            for c, v in enumerate((PLAN_LABELS[p.plan_type], x.material_id, x.material_name, x.period), start=1):
                _put_text(ws, row, c, v)
            _put_number(ws, row, 5, x.required_qty)
            _put_number(ws, row, 6, x.available_qty)
            _put_number(ws, row, 7, x.shortage_qty)              # 소요량 - 가용량 으로 계산하지 않는다
            _put_text(ws, row, 8, x.unit)
            _put_text(ws, row, 9, x.note)
            row += 1


def _constraints(ws: Worksheet, req: PlanExportRequest) -> None:
    row = _start_sheet(ws, req, CONSTRAINT_COLUMNS)
    for p in _ordered(req):
        for x in p.constraints:
            for c, v in enumerate((PLAN_LABELS[p.plan_type], x.constraint_id, x.label, x.dimension, x.strength, x.status, x.resource_id, x.period), start=1):
                _put_text(ws, row, c, v)
            _put_number(ws, row, 9, x.limit_value)
            _put_number(ws, row, 10, x.used_value)
            _put_text(ws, row, 11, x.unit)
            _put_text(ws, row, 12, x.source)
            _put_text(ws, row, 13, x.note)
            row += 1


def _evidence(ws: Worksheet, req: PlanExportRequest) -> None:
    row = _start_sheet(ws, req, ("항목", "값"))
    fb = req.forecast_basis
    meta = [
        ("출력 등급", EXPORT_STATUS), ("입력 계약 버전", EXPORT_CONTRACT_VERSION), ("데이터 구분", req.data_classification),
        ("생성 엔진", req.engine), ("생성 시각", req.generated_at.isoformat() if req.generated_at else None),
        ("계획 기간", f"{req.horizon.period_from} ~ {req.horizon.period_to}"),
        ("예측 분위수", fb.quantile if fb else None), ("P50 방식", fb.p50_method if fb else None),
        ("분위수 모델 버전", fb.quantile_model_version if fb else None),
        ("학습 데이터 기준일", fb.trained_until.isoformat() if fb and fb.trained_until else None),
    ]
    for name, value in meta:
        _put_text(ws, row, 1, name)
        _put_text(ws, row, 2, value)
        row += 1
    row += 1
    for p in _ordered(req):
        for text in p.assumptions:
            _put_text(ws, row, 1, f"가정 ({PLAN_LABELS[p.plan_type]})")
            _put_text(ws, row, 2, text)
            row += 1
    row += 1
    for c, name in enumerate(EVIDENCE_COLUMNS, start=1):
        cell = ws.cell(row=row, column=c, value=name)
        cell.font = Font(bold=True)
        cell.fill = _HEADER_FILL
    row += 1
    for e in req.evidence:
        for c, v in enumerate((e.source_type, e.name, e.batch_id, e.as_of, e.description), start=1):
            _put_text(ws, row, c, v)
        row += 1
    ws.column_dimensions["A"].width = 24
    ws.column_dimensions["B"].width = 40


def row_count(req: PlanExportRequest) -> int:
    return sum(len(p.production) + len(p.materials) + len(p.constraints) for p in req.plans) + len(req.evidence)


def build_workbook(req: PlanExportRequest) -> bytes:
    rows = row_count(req)
    if rows > MAX_ROWS:
        raise ExportTooLarge(rows, MAX_ROWS)
    wb = Workbook()
    wb.remove(wb.active)
    for name, fill in zip(SHEETS, (_comparison, _production, _materials, _constraints, _evidence)):
        fill(wb.create_sheet(name), req)
    wb.properties.title = "DEMO / UNVERIFIED plan export"
    wb.properties.creator = "decision-supporter (DEMO/UNVERIFIED)"
    wb.properties.description = BANNER
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _stem(name: str | None) -> str:
    """사용자 이름에서 문자·숫자·밑줄·하이픈만 남긴다 (경로, 따옴표, 줄바꿈, 확장자 위장 제거)."""
    text = unicodedata.normalize("NFKC", name or "")
    cleaned = re.sub(r"[^\w\-]+", "_", text, flags=re.UNICODE).strip("_-")
    return cleaned[:80] or "plan_export"


def export_filename(name: str | None) -> tuple[str, str]:
    """→ (UTF-8 파일명, ASCII 대체 파일명). 접두사 DEMO_UNVERIFIED_ 는 제거할 수 없다."""
    stem = _stem(name)
    ascii_stem = re.sub(r"[^A-Za-z0-9_\-]+", "_", stem).strip("_-") or "plan_export"
    return f"DEMO_UNVERIFIED_{stem}.xlsx", f"DEMO_UNVERIFIED_{ascii_stem}.xlsx"


def content_disposition(name: str | None) -> str:
    utf8_name, ascii_name = export_filename(name)
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(utf8_name, safe='')}"
