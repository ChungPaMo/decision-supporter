"""월별 수요 시트 검증·정규화. 모든 문제는 원본 시트·행·열(셀 주소)과 함께 보고한다.

PoC 재사용
- src/normalization/values.py: 숫자 정규식(쉼표 천 단위 허용, 지수·NaN 거부). \\d 대신 [0-9] 로 전각 숫자를 막는다(E05 교훈).
- scripts/e10_mock_erp.py M-24(+M-20): 같은 코드·다른 값은 덮어쓰지 않고 ERROR 로 남긴다. 코드↔품명 충돌, 같은 키 다른 값 충돌에 적용.
- E10 M13 교훈: 제품코드와 품명이 서로 다른 제품을 가리키면 어느 쪽도 고르지 않는다.
"""

from __future__ import annotations

import calendar
import re
import unicodedata
from dataclasses import dataclass, field as dc_field
from decimal import Decimal, InvalidOperation

from pydantic import ValidationError

from app.contracts import CODE_PATTERN, MonthlyDemandRow
from app.ingest.mapping import SheetMapping
from app.ingest.parsing import Cell, ParsedSheet, col_letter

# unit 테이블 시드(migrations 0001)와 같은 값. 단위가 늘면 두 곳을 함께 고친다(tests/test_ingest_units.py 가 대조).
UNITS = {"EA": "COUNT", "kg": "MASS", "g": "MASS", "t": "MASS", "h": "TIME", "min": "TIME"}
UNIT_ALIASES = {
    "ea": "EA", "개": "EA", "pcs": "EA", "pc": "EA", "each": "EA", "개수": "EA",
    "kg": "kg", "킬로그램": "kg", "키로": "kg", "g": "g", "그램": "g", "t": "t", "ton": "t", "톤": "t",
    "h": "h", "hr": "h", "시간": "h", "min": "min", "분": "min",
}
_PLAIN = re.compile(r"^-?[0-9]+(\.[0-9]+)?$")
_THOUSANDS = re.compile(r"^-?[0-9]{1,3}(,[0-9]{3})+(\.[0-9]+)?$")
_CODE = re.compile(CODE_PATTERN)
_PERIOD_FORMS = [
    (re.compile(r"^([0-9]{4})[-/.]([0-9]{1,2})$"), False),
    (re.compile(r"^([0-9]{4})([0-9]{2})$"), False),
    (re.compile(r"^([0-9]{4})\s*년\s*([0-9]{1,2})\s*월$"), False),
    (re.compile(r"^([0-9]{4})[-/.]([0-9]{1,2})[-/.]([0-9]{1,2})$"), True),
]
MAX_ISSUES_PER_CODE = 200


@dataclass
class Location:
    sheet: str
    row: int
    col: int | None = None

    @property
    def cell_ref(self) -> str | None:
        return f"{col_letter(self.col)}{self.row}" if self.col else None


@dataclass
class Issue:
    severity: str                # ERROR | WARNING
    code: str
    message: str
    field: str | None = None
    location: Location | None = None
    raw_value: str | None = None
    evidence: dict = dc_field(default_factory=dict)


@dataclass
class Record:
    product_id: str
    product_name: str | None
    period: str
    demand_qty: Decimal
    unit: str
    location: Location          # 값이 있던 행 (열은 None)
    review_required: bool = False


@dataclass
class SheetValidation:
    issues: list[Issue] = dc_field(default_factory=list)
    records: list[Record] = dc_field(default_factory=list)
    data_rows: int = 0
    error_rows: int = 0


def norm_name(s: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", s)).strip().casefold()


def parse_period(cell: Cell) -> tuple[str | None, tuple[str, str] | None]:
    """→ (YYYY-MM | None, (오류코드, 메시지) | None)"""
    s = (cell.value or "").strip()
    if not s:
        return None, ("PERIOD_MISSING", "기간이 비어 있습니다.")
    if cell.kind == "number" and "." in s:
        return None, ("PERIOD_AMBIGUOUS", f"소수점이 있는 숫자 '{s}' 는 연월로 해석할 수 없습니다 (2026.1 과 2026.10 이 구분되지 않음). 텍스트 'YYYY-MM' 로 입력하세요.")
    for rx, has_day in _PERIOD_FORMS:
        m = rx.match(s)
        if not m:
            continue
        y, mo = int(m.group(1)), int(m.group(2))
        if not (1 <= mo <= 12) or not (1990 <= y <= 2100):
            return None, ("PERIOD_INVALID", f"존재하지 않는 연월입니다: '{s}'")
        if has_day:
            d = int(m.group(3))
            if not (1 <= d <= calendar.monthrange(y, mo)[1]):
                return None, ("PERIOD_INVALID", f"존재하지 않는 날짜입니다: '{s}'")
            if d != 1:
                return None, ("PERIOD_NOT_MONTH_START", f"월 단위 데이터인데 1일이 아닌 날짜입니다: '{s}' (일 단위 데이터는 월 합계로 바꿔 입력하세요).")
        return f"{y:04d}-{mo:02d}", None
    return None, ("PERIOD_INVALID", f"연월 형식이 아닙니다: '{s}' (YYYY-MM, YYYY/MM, YYYYMM, YYYY년 M월 허용)")


def parse_qty(cell: Cell) -> tuple[Decimal | None, tuple[str, str] | None]:
    s = (cell.value or "").strip()
    if not s:
        return None, ("QTY_MISSING", "수량이 비어 있습니다.")
    if not (_PLAIN.match(s) or _THOUSANDS.match(s)):
        return None, ("QTY_INVALID", f"수량 형식 오류: '{s}' (숫자만 허용, 쉼표 천 단위 가능; 지수·NaN·단위 문자 불가)")
    try:
        d = Decimal(s.replace(",", ""))
    except InvalidOperation:                                  # 정규식 통과 후에는 발생하지 않아야 함
        return None, ("QTY_INVALID", f"수량 변환 실패: '{s}'")
    if d < 0:
        return None, ("QTY_NEGATIVE", f"수량이 음수입니다: '{s}'")
    if -d.as_tuple().exponent > 4 or len(d.as_tuple().digits) - max(0, -d.as_tuple().exponent) > 14:
        return None, ("QTY_RANGE", f"수량의 자릿수가 범위를 넘습니다 (정수 14자리, 소수 4자리): '{s}'")
    return d, None


def restore_code(cell: Cell) -> tuple[str | None, Issue | None]:
    """제품코드 보존. 텍스트는 그대로(앞뒤 공백만 제거). 숫자 셀은 선행 0 이 이미 사라졌을 수 있다."""
    s = (cell.value or "").strip()
    if not s:
        return None, None
    if cell.kind == "number":
        fmt = cell.number_format or ""
        if re.fullmatch(r"0+", fmt) and len(s) < len(fmt) and s.isdigit():
            return s.zfill(len(fmt)), Issue("WARNING", "CODE_RESTORED_FROM_FORMAT",
                                           f"제품코드가 숫자 셀입니다. 표시 서식({fmt})에 맞춰 '{s.zfill(len(fmt))}' 로 복원했습니다. 확인하세요.",
                                           "product_id", raw_value=s)
        return s, Issue("WARNING", "CODE_NUMERIC_CELL",
                        f"제품코드 '{s}' 가 숫자 셀입니다. 선행 0 이 이미 사라졌을 수 있습니다. 원본에서 텍스트 서식을 확인하세요.",
                        "product_id", raw_value=s)
    return s, None


def normalize_unit(raw: str) -> str | None:
    s = raw.strip()
    return UNIT_ALIASES.get(s.casefold()) or (s if s in UNITS else None)


def validate_sheet(sheet: ParsedSheet, mapping: SheetMapping, default_unit: str | None = None) -> SheetValidation:
    out = SheetValidation(data_rows=len(sheet.rows))
    f = mapping.fields
    sname = sheet.name
    bad_rows: set[int] = set()

    def add(sev, code, msg, fld=None, row=None, col=None, raw=None, **ev):
        loc = Location(sname, row, col) if row is not None else None
        out.issues.append(Issue(sev, code, msg, fld, loc, raw, ev))
        if sev == "ERROR" and row is not None:
            bad_rows.add(row)

    # --- 시트 수준: 단위 열 ---
    unit_default = None
    if "unit" not in f:
        if default_unit:
            unit_default = normalize_unit(default_unit)
            if unit_default is None:
                add("ERROR", "UNIT_UNKNOWN", f"default_unit '{default_unit}' 은 알 수 없는 단위입니다.")
            else:
                add("WARNING", "UNIT_COLUMN_MISSING", f"단위 열이 없어 모든 행에 기본 단위 '{unit_default}' 를 적용했습니다. 확인하세요.")
        else:
            add("ERROR", "UNIT_COLUMN_MISSING", "단위 열이 없습니다. 파일에 단위 열을 추가하거나 default_unit 을 지정하세요.")

    parsed = []     # (RawRow, code, code_cell, name, period, qty, unit, review)
    for r in sheet.rows:
        def cell(fld):
            m = f.get(fld)
            return r.cells[m.col - 1] if m and m.col - 1 < len(r.cells) else Cell(None, "empty")

        if len(r.cells) != sheet.width:
            add("ERROR", "ROW_COLUMN_COUNT", f"열 개수가 헤더({sheet.width})와 다릅니다 ({len(r.cells)}). 구분자나 따옴표 문제일 수 있습니다.", row=r.row)
            continue
        review = False
        code, code_issue = restore_code(cell("product_id"))
        ccol = f["product_id"].col
        if code is None:
            add("ERROR", "CODE_MISSING", "제품코드가 비어 있습니다.", "product_id", r.row, ccol)
        elif not _CODE.match(code) or len(code) > 40:
            add("ERROR", "CODE_INVALID", f"제품코드 형식 오류: '{code}' (영문·숫자로 시작, 영문·숫자·_ . - 만 허용, 40자 이하)", "product_id", r.row, ccol, code)
            code = None
        if code_issue and code:
            out.issues.append(Issue(code_issue.severity, code_issue.code, code_issue.message, code_issue.field, Location(sname, r.row, ccol), code_issue.raw_value))
            review = True
        name = None
        if "product_name" in f:
            name = (cell("product_name").value or "").strip() or None
        period, perr = parse_period(cell("period"))
        pcol = f["period"].col
        if perr:
            add("ERROR", perr[0], perr[1], "period", r.row, pcol, cell("period").value)
        qty, qerr = parse_qty(cell("demand_qty"))
        qcol = f["demand_qty"].col
        if qerr:
            add("ERROR", qerr[0], qerr[1], "demand_qty", r.row, qcol, cell("demand_qty").value)
        unit = unit_default
        if "unit" in f:
            raw_u = (cell("unit").value or "").strip()
            ucol = f["unit"].col
            if not raw_u:
                add("ERROR", "UNIT_MISSING", "단위가 비어 있습니다.", "unit", r.row, ucol)
            else:
                unit = normalize_unit(raw_u)
                if unit is None:
                    add("ERROR", "UNIT_UNKNOWN", f"알 수 없는 단위: '{raw_u}' (허용: {', '.join(UNITS)}, 개·킬로그램·톤 등)", "unit", r.row, ucol, raw_u)
        if any(m.review_required for m in f.values()):
            review = True
        parsed.append((r, code, name, period, qty, unit, review))

    _check_conflicts(parsed, f, sname, add)

    # --- 제품별 단위 일관성 ---
    units_by_code: dict[str, dict[str, list[int]]] = {}
    for r, code, _n, _p, _q, unit, _rv in parsed:
        if code and unit:
            units_by_code.setdefault(code, {}).setdefault(unit, []).append(r.row)
    for code, us in units_by_code.items():
        if len(us) < 2:
            continue
        dims = {UNITS[u] for u in us}
        rows_ev = {u: rows[:10] for u, rows in us.items()}
        sev, c, msg = (("ERROR", "UNIT_DIMENSION_MISMATCH", f"제품 '{code}' 의 단위 종류가 섞여 있습니다: {sorted(us)}")
                       if len(dims) > 1 else
                       ("WARNING", "UNIT_MIXED", f"제품 '{code}' 에 같은 종류의 다른 단위가 섞여 있습니다: {sorted(us)} (저장 전 환산 필요)"))
        for u, rows in us.items():
            for row in rows[:MAX_ISSUES_PER_CODE]:
                add(sev, c, msg, "unit", row, f["unit"].col if "unit" in f else None, u, units=rows_ev)

    # --- 중복 키 ---
    seen: dict[tuple[str, str], tuple[int, Decimal | None]] = {}
    dropped: set[int] = set()
    for r, code, _n, period, qty, _u, _rv in parsed:
        if not (code and period) or r.row in bad_rows:
            continue
        key = (code, period)
        if key not in seen:
            seen[key] = (r.row, qty)
            continue
        first_row, first_qty = seen[key]
        if first_qty == qty:
            add("WARNING", "DUPLICATE_ROW_IDENTICAL", f"같은 제품·월·수량이 {first_row}행에도 있습니다. 첫 행만 사용합니다.", "product_id", r.row, f["product_id"].col,
                key=f"{code}/{period}", first_row=first_row)
            dropped.add(r.row)
        else:
            add("ERROR", "DUPLICATE_KEY_CONFLICT", f"같은 제품·월이 {first_row}행에도 있으며 수량이 다릅니다 ({first_qty} vs {qty}). 합산·선택하지 않습니다.",
                "demand_qty", r.row, f["demand_qty"].col, key=f"{code}/{period}", first_row=first_row)
            add("ERROR", "DUPLICATE_KEY_CONFLICT", f"같은 제품·월이 {r.row}행에도 있으며 수량이 다릅니다 ({first_qty} vs {qty}).",
                "demand_qty", first_row, f["demand_qty"].col, key=f"{code}/{period}", other_row=r.row)

    # --- 레코드 생성 (오류 없는 행만, 계약 모델로 최종 확인) ---
    for r, code, name, period, qty, unit, review in parsed:
        if r.row in bad_rows or r.row in dropped or unit is None:      # unit None: 시트 수준 오류로 이미 보고됨
            continue
        try:
            MonthlyDemandRow(product_id=code, period=period, demand_qty=qty, unit=unit)
        except ValidationError as exc:
            add("ERROR", "CONTRACT_VIOLATION", f"데이터 계약 위반: {exc.errors()[0]['msg']}", row=r.row)
            continue
        out.records.append(Record(code, name, period, qty, unit, Location(sname, r.row), review))
    out.error_rows = len(bad_rows)
    return out


def _check_conflicts(parsed, f, sname, add) -> None:
    """제품코드↔품명 충돌과 코드 표기 변형. 덮어쓰거나 한쪽을 고르지 않고 관련 행 전부에 ERROR/WARNING 을 남긴다."""
    if "product_name" in f:
        by_code: dict[str, dict[str, list[int]]] = {}
        by_name: dict[str, dict[str, list[int]]] = {}
        for r, code, name, *_ in parsed:
            if code and name:
                by_code.setdefault(code, {}).setdefault(norm_name(name), []).append(r.row)
                by_name.setdefault(norm_name(name), {}).setdefault(code, []).append(r.row)
        for code, names in by_code.items():
            if len(names) > 1:
                for rows in names.values():
                    for row in rows[:MAX_ISSUES_PER_CODE]:
                        add("ERROR", "CODE_NAME_CONFLICT", f"제품코드 '{code}' 에 서로 다른 품명이 있습니다: {sorted(names)}", "product_name", row, f["product_name"].col,
                            names={n: v[:10] for n, v in names.items()})
        for name, codes in by_name.items():
            if len(codes) > 1:
                for code, rows in codes.items():
                    for row in rows[:MAX_ISSUES_PER_CODE]:
                        add("ERROR", "NAME_CODE_CONFLICT", f"품명 '{name}' 에 서로 다른 제품코드가 있습니다: {sorted(codes)}", "product_id", row, f["product_id"].col,
                            codes={c: v[:10] for c, v in codes.items()})
    variants: dict[str, dict[str, list[int]]] = {}
    for r, code, *_ in parsed:
        if code:
            variants.setdefault(code.casefold().lstrip("0"), {}).setdefault(code, []).append(r.row)
    for key, codes in variants.items():
        if len(codes) > 1:
            for code, rows in codes.items():
                for row in rows[:MAX_ISSUES_PER_CODE]:
                    add("WARNING", "CODE_VARIANT", f"선행 0·대소문자만 다른 제품코드가 함께 있습니다: {sorted(codes)}. 같은 제품이면 통일하세요 (자동으로 합치지 않음).",
                        "product_id", row, f["product_id"].col, code)
