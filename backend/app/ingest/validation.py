"""월별 수요·제품 마스터 시트 검증·정규화. 모든 문제는 원본 시트·행·열(셀 주소)과 함께 보고한다.

PoC 재사용
- src/normalization/values.py: 숫자 정규식(쉼표 천 단위 허용, 지수·NaN 거부). \\d 대신 [0-9] 로 전각 숫자를 막는다(E05 교훈).
- scripts/e10_mock_erp.py M-24(+M-20): 같은 코드·다른 값은 덮어쓰지 않고 ERROR 로 남긴다. 코드↔품명 충돌, 같은 키 다른 값 충돌에 적용.
- E10 M13 교훈: 제품코드와 품명이 서로 다른 제품을 가리키면 어느 쪽도 고르지 않는다.
"""

from __future__ import annotations

import calendar
import datetime as dt
import re
import unicodedata
from dataclasses import dataclass, field as dc_field
from decimal import Decimal, InvalidOperation

from pydantic import ValidationError

from app.contracts import CODE_PATTERN, MaterialRow, MonthlyDemandRow, ProductRow, ProductType
from app.ingest.mapping import SheetMapping, field_for_header
from app.ingest.parsing import Cell, ParsedSheet, col_letter

# unit 테이블 시드(migrations 0001)와 같은 값. 단위가 늘면 두 곳을 함께 고친다(tests/test_ingest.py::test_units_match_migration_seed 가 대조).
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
MAX_NAME_LEN = 200
PRODUCT_TYPE_ALIASES = {
    "finished": ProductType.FINISHED.value, "fg": ProductType.FINISHED.value, "완제품": ProductType.FINISHED.value,
    "semi_finished": ProductType.SEMI_FINISHED.value, "semi-finished": ProductType.SEMI_FINISHED.value, "sf": ProductType.SEMI_FINISHED.value,
    "반제품": ProductType.SEMI_FINISHED.value,
}
_EXCEL_ERROR = re.compile(r"^#(REF!|N/A|VALUE!|NAME\?|DIV/0!|NULL!|NUM!)$")


@dataclass(frozen=True)
class KnownProduct:
    """승인되어 제품 마스터에 있는 제품 (검증 기준 데이터)."""
    product_id: str
    product_name: str
    base_unit: str
    product_type: str | None = None
    safety_stock_qty: Decimal = Decimal(0)


@dataclass(frozen=True)
class KnownMaterial:
    """승인되어 자재 마스터에 있는 자재."""
    material_id: str
    material_name: str
    base_unit: str
    safety_stock_qty: Decimal = Decimal(0)


@dataclass(frozen=True)
class KnownBom:
    """승인된 BOM 헤더 + 하위 제품 코드 (버전·기간 충돌과 순환 검사용)."""
    bom_id: str
    product_id: str
    bom_version: str
    is_active: bool
    valid_from: dt.date
    valid_to: dt.date | None = None
    component_products: tuple[str, ...] = ()


@dataclass
class MasterData:
    """검증에 쓰는 기준 데이터. run_preview 에 넘기지 않으면(저장 없는 /ingest/preview) 기존 데이터 대조를 하지 않는다."""
    products: dict[str, KnownProduct] = dc_field(default_factory=dict)
    materials: dict[str, KnownMaterial] = dc_field(default_factory=dict)
    boms: list[KnownBom] = dc_field(default_factory=list)

    def code_variants(self, code: str, known: dict | None = None) -> list[str]:
        """선행 0·대소문자만 다른 등록 코드 (자동으로 합치지 않고 안내만 한다)."""
        key = _variant_key(code)
        return sorted(p for p in (self.products if known is None else known) if p != code and _variant_key(p) == key)


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
class ProductRecord:
    product_id: str
    product_name: str
    base_unit: str
    product_type: str | None
    safety_stock_qty: Decimal
    location: Location
    review_required: bool = False


@dataclass
class MaterialRecord:
    material_id: str
    material_name: str
    base_unit: str
    safety_stock_qty: Decimal
    location: Location
    review_required: bool = False


@dataclass
class SheetValidation:
    issues: list[Issue] = dc_field(default_factory=list)
    records: list = dc_field(default_factory=list)            # entity 별 레코드
    data_rows: int = 0
    error_rows: int = 0


def _variant_key(code: str) -> str:
    return code.casefold().lstrip("0")


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


def parse_qty(cell: Cell, scale: int = 4) -> tuple[Decimal | None, tuple[str, str] | None]:
    """scale: 허용 소수 자릿수 (재고·수요 4, BOM 소요량·스크랩 6)."""
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
    if -d.as_tuple().exponent > scale or len(d.as_tuple().digits) - max(0, -d.as_tuple().exponent) > 18 - scale:
        return None, ("QTY_RANGE", f"수량의 자릿수가 범위를 넘습니다 (정수 {18 - scale}자리, 소수 {scale}자리): '{s}'")
    return d, None


def restore_code(cell: Cell, fld: str = "product_id", label: str = "제품코드") -> tuple[str | None, Issue | None]:
    """코드 보존. 텍스트는 그대로(앞뒤 공백만 제거). 숫자 셀은 선행 0 이 이미 사라졌을 수 있다."""
    s = (cell.value or "").strip()
    if not s:
        return None, None
    if cell.kind == "number":
        fmt = cell.number_format or ""
        if re.fullmatch(r"0+", fmt) and len(s) < len(fmt) and s.isdigit():
            return s.zfill(len(fmt)), Issue("WARNING", "CODE_RESTORED_FROM_FORMAT",
                                           f"{label}가 숫자 셀입니다. 표시 서식({fmt})에 맞춰 '{s.zfill(len(fmt))}' 로 복원했습니다. 확인하세요.",
                                           fld, raw_value=s)
        return s, Issue("WARNING", "CODE_NUMERIC_CELL",
                        f"{label} '{s}' 가 숫자 셀입니다. 선행 0 이 이미 사라졌을 수 있습니다. 원본에서 텍스트 서식을 확인하세요.",
                        fld, raw_value=s)
    return s, None


def normalize_unit(raw: str) -> str | None:
    s = raw.strip()
    return UNIT_ALIASES.get(s.casefold()) or (s if s in UNITS else None)


def _collector(out: SheetValidation, sname: str, bad_rows: set[int]):
    def add(sev, code, msg, fld=None, row=None, col=None, raw=None, **ev):
        loc = Location(sname, row, col) if row is not None else None
        out.issues.append(Issue(sev, code, msg, fld, loc, raw, ev))
        if sev == "ERROR" and row is not None:
            bad_rows.add(row)
    return add


def _read_code(cell: Cell, row: int, col: int, sname: str, out: SheetValidation, add, fld: str = "product_id",
               label: str = "제품코드") -> tuple[str | None, bool]:
    """코드 셀 → (코드 | None, 검토 필요). 형식 오류는 ERROR 로 남기고 None."""
    code, issue = restore_code(cell, fld, label)
    if code is None:
        add("ERROR", "CODE_MISSING", f"{label}가 비어 있습니다.", fld, row, col)
        return None, False
    if not _CODE.match(code) or len(code) > 40:
        add("ERROR", "CODE_INVALID", f"{label} 형식 오류: '{code}' (영문·숫자로 시작, 영문·숫자·_ . - 만 허용, 40자 이하)", fld, row, col, code)
        return None, False
    if issue:
        out.issues.append(Issue(issue.severity, issue.code, issue.message, issue.field, Location(sname, row, col), issue.raw_value))
    return code, issue is not None


def validate_sheet(sheet: ParsedSheet, mapping: SheetMapping, default_unit: str | None = None, master: MasterData | None = None) -> SheetValidation:
    out = SheetValidation(data_rows=len(sheet.rows))
    f = mapping.fields
    sname = sheet.name
    bad_rows: set[int] = set()
    add = _collector(out, sname, bad_rows)

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
        code, review = _read_code(cell("product_id"), r.row, f["product_id"].col, sname, out, add)
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
    if master is not None:
        _check_against_master(parsed, f, master, add)

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


def _check_against_master(parsed, f, master: MasterData, add) -> None:
    """월별 수요 행을 승인된 제품 마스터와 대조. 없는 제품은 등록하지 않고 ERROR (제품 승인 후 재검증하면 풀린다)."""
    for r, code, name, _p, _q, unit, _rv in parsed:
        if not code:
            continue
        p = master.products.get(code)
        if p is None:
            variants = master.code_variants(code)
            hint = f" 비슷한 등록 코드: {variants}." if variants else ""
            add("ERROR", "PRODUCT_NOT_REGISTERED", f"제품 마스터에 없는 제품코드입니다: '{code}'.{hint} 제품 마스터를 먼저 승인한 뒤 다시 검증하세요.",
                "product_id", r.row, f["product_id"].col, code, similar_codes=variants)
            continue
        if name and norm_name(name) != norm_name(p.product_name):
            add("ERROR", "EXISTING_PRODUCT_CONFLICT", f"제품코드 '{code}' 의 등록 품명은 '{p.product_name}' 인데 파일에는 '{name}' 입니다.",
                "product_name", r.row, f["product_name"].col, name, existing=p.product_name)
        if unit and unit != p.base_unit:
            same_dim = UNITS.get(unit) == UNITS.get(p.base_unit)
            add("WARNING" if same_dim else "ERROR", "UNIT_DIFFERS_FROM_BASE" if same_dim else "UNIT_DIMENSION_MISMATCH",
                f"제품 '{code}' 의 기준 단위는 '{p.base_unit}' 인데 파일 단위는 '{unit}' 입니다." + (" (저장 전 환산 필요)" if same_dim else ""),
                "unit", r.row, f["unit"].col if "unit" in f else None, unit, base_unit=p.base_unit)


def _invalid_name(name: str) -> str | None:
    """사람이 입력한 품명으로 볼 수 없는 값의 사유. 정상이면 None."""
    if len(name) > MAX_NAME_LEN:
        return f"{MAX_NAME_LEN}자를 넘습니다"
    if _EXCEL_ERROR.match(name):
        return "Excel 오류 값입니다"
    if any(unicodedata.category(c) in ("Cc", "Cf", "Cs", "Co", "Cn") or c == "\ufffd" for c in name):
        return "제어 문자 또는 깨진 문자가 있습니다"
    return None


@dataclass(frozen=True)
class ItemSpec:
    """품목 마스터(제품·자재) 공통 검증의 entity 별 차이."""
    id_field: str
    name_field: str
    label: str                  # 제품 | 자재
    prefix: str                 # 오류 코드: {prefix}_ALREADY_REGISTERED, DUPLICATE_{prefix}_ID, EXISTING_{prefix}_CONFLICT
    has_type: bool


PRODUCT_SPEC = ItemSpec("product_id", "product_name", "제품", "PRODUCT", True)
MATERIAL_SPEC = ItemSpec("material_id", "material_name", "자재", "MATERIAL", False)


def validate_products(sheet: ParsedSheet, mapping: SheetMapping, default_unit: str | None = None, master: MasterData | None = None) -> SheetValidation:
    return _validate_items(sheet, mapping, default_unit, master, PRODUCT_SPEC)


def validate_materials(sheet: ParsedSheet, mapping: SheetMapping, default_unit: str | None = None, master: MasterData | None = None) -> SheetValidation:
    return _validate_items(sheet, mapping, default_unit, master, MATERIAL_SPEC)


def _validate_items(sheet: ParsedSheet, mapping: SheetMapping, default_unit: str | None, master: MasterData | None, spec: ItemSpec) -> SheetValidation:
    """품목 마스터 시트 검증. 파일 안의 규칙은 항상, 기존 품목과의 대조는 master 가 있을 때만 한다."""
    out = SheetValidation(data_rows=len(sheet.rows))
    f = mapping.fields
    sname = sheet.name
    bad_rows: set[int] = set()
    add = _collector(out, sname, bad_rows)
    id_label = f"{spec.label}코드"

    if default_unit:
        add("WARNING", "DEFAULT_UNIT_IGNORED", f"{spec.label} 마스터에서는 기준 단위가 필수 열이라 default_unit 을 쓰지 않습니다.")
    if "safety_stock_qty" not in f:
        add("WARNING", "SAFETY_STOCK_DEFAULTED", f"안전재고 열이 없어 모든 {spec.label}을 0 으로 등록합니다. 확인하세요.")
    demand_like = [h for h in mapping.unmapped_headers if field_for_header(h, "monthly_demand") in ("period", "demand_qty")]
    if demand_like:
        add("WARNING", "LOOKS_LIKE_OTHER_ENTITY", f"월별 수요 파일의 열이 있습니다: {demand_like}. {spec.label} 마스터가 맞는지 확인하세요.", headers=demand_like)

    parsed = []     # (RawRow, code, name, unit, ptype, stock, review)
    for r in sheet.rows:
        def cell(fld):
            m = f.get(fld)
            return r.cells[m.col - 1] if m and m.col - 1 < len(r.cells) else Cell(None, "empty")

        if len(r.cells) != sheet.width:
            add("ERROR", "ROW_COLUMN_COUNT", f"열 개수가 헤더({sheet.width})와 다릅니다 ({len(r.cells)}). 구분자나 따옴표 문제일 수 있습니다.", row=r.row)
            continue
        code, review = _read_code(cell(spec.id_field), r.row, f[spec.id_field].col, sname, out, add, spec.id_field, id_label)

        name = (cell(spec.name_field).value or "").strip() or None
        ncol = f[spec.name_field].col
        if name is None:
            add("ERROR", "NAME_MISSING", f"{spec.label}명이 비어 있습니다.", spec.name_field, r.row, ncol)
        elif (why := _invalid_name(name)) is not None:
            add("ERROR", "NAME_INVALID", f"{spec.label}명이 올바르지 않습니다: {why}.", spec.name_field, r.row, ncol, name[:100])
            name = None

        raw_u = (cell("base_unit").value or "").strip()
        ucol = f["base_unit"].col
        unit = None
        if not raw_u:
            add("ERROR", "UNIT_MISSING", "기준 단위가 비어 있습니다.", "base_unit", r.row, ucol)
        else:
            unit = normalize_unit(raw_u)
            if unit is None:
                add("ERROR", "UNIT_UNKNOWN", f"알 수 없는 단위: '{raw_u}' (허용: {', '.join(UNITS)}, 개·킬로그램·톤 등)", "base_unit", r.row, ucol, raw_u)

        ptype = None
        raw_t = (cell("product_type").value or "").strip() if spec.has_type else ""
        if raw_t:
            ptype = PRODUCT_TYPE_ALIASES.get(raw_t.casefold())
            if ptype is None:
                add("ERROR", "PRODUCT_TYPE_UNKNOWN", f"알 수 없는 제품 유형: '{raw_t}' (허용: FINISHED, SEMI_FINISHED, 완제품, 반제품)",
                    "product_type", r.row, f["product_type"].col, raw_t)

        stock = None
        if "safety_stock_qty" in f and (cell("safety_stock_qty").value or "").strip():
            stock, serr = parse_qty(cell("safety_stock_qty"))
            if serr:
                add("ERROR", serr[0], "안전재고: " + serr[1], "safety_stock_qty", r.row, f["safety_stock_qty"].col, cell("safety_stock_qty").value)
        elif "safety_stock_qty" in f:
            add("WARNING", "SAFETY_STOCK_DEFAULTED", "안전재고가 비어 있어 0 으로 등록합니다. 확인하세요.", "safety_stock_qty", r.row, f["safety_stock_qty"].col)
        parsed.append((r, code, name, unit, ptype, stock, review or any(m.review_required for m in f.values())))

    # 코드↔품명 충돌·코드 변형은 월별 수요와 같은 규칙 (이름은 이 entity 에서 필수라 항상 매핑되어 있다)
    _check_conflicts(parsed, f, sname, add, spec.id_field, spec.name_field, spec.label)

    # --- 코드 중복: 마스터는 품목당 한 행. 값이 같아도 첫 행을 고르지 않고 원본 수정을 요구한다 ---
    rows_by_code: dict[str, list[int]] = {}
    for r, code, *_ in parsed:
        if code:
            rows_by_code.setdefault(code, []).append(r.row)
    for code, rows in rows_by_code.items():
        if len(rows) > 1:
            for row in rows[:MAX_ISSUES_PER_CODE]:
                add("ERROR", f"DUPLICATE_{spec.prefix}_ID", f"{id_label} '{code}' 가 {len(rows)}행에 있습니다: {rows[:10]}. {spec.label}당 한 행만 허용합니다.",
                    spec.id_field, row, f[spec.id_field].col, code, rows=rows[:10])

    already: set[int] = set()
    if master is not None:
        already = _check_items_against_master(parsed, f, master, add, spec)

    # --- 레코드 생성 (오류 없는 행만, 이미 같은 값으로 등록된 품목은 제외, 계약 모델로 최종 확인) ---
    for r, code, name, unit, ptype, stock, review in parsed:
        if r.row in bad_rows or r.row in already:
            continue
        qty = stock if stock is not None else Decimal(0)
        try:
            if spec.has_type:
                ProductRow(product_id=code, product_name=name, product_type=ptype, base_unit=unit, safety_stock_qty=qty)
            else:
                MaterialRow(material_id=code, material_name=name, base_unit=unit, safety_stock_qty=qty)
        except ValidationError as exc:
            add("ERROR", "CONTRACT_VIOLATION", f"데이터 계약 위반: {exc.errors()[0]['msg']}", row=r.row)
            continue
        loc = Location(sname, r.row)
        out.records.append(ProductRecord(code, name, unit, ptype, qty, loc, review) if spec.has_type else MaterialRecord(code, name, unit, qty, loc, review))
    out.error_rows = len(bad_rows)
    return out


def _check_items_against_master(parsed, f, master: MasterData, add, spec: ItemSpec) -> set[int]:
    """기존 품목과 대조. 값이 다르면 덮어쓰지 않고 ERROR, 모두 같으면 경고 후 등록 대상에서 제외한다(반환: 제외할 행)."""
    known = master.products if spec.has_type else master.materials
    # 정렬: 같은 품명이 여럿이어도 결과(→ result_digest)가 조회 순서에 흔들리지 않게 한다
    by_name = {norm_name(getattr(p, spec.name_field)): code for code, p in sorted(known.items(), reverse=True)}
    id_label = f"{spec.label}코드"
    same: set[int] = set()
    for r, code, name, unit, ptype, stock, _rv in parsed:
        if not code:
            continue
        p = known.get(code)
        idcol = f[spec.id_field].col
        if p is None:
            owner = by_name.get(norm_name(name)) if name else None
            if owner is not None:
                add("ERROR", "EXISTING_NAME_CONFLICT", f"품명 '{name}' 은 이미 {id_label} '{owner}' 로 등록되어 있습니다.", spec.id_field, r.row, idcol, code,
                    **{f"existing_{spec.id_field}": owner})
            variants = master.code_variants(code, known)
            if variants:
                add("WARNING", "CODE_VARIANT", f"선행 0·대소문자만 다른 등록 코드가 있습니다: {variants}. 같은 {spec.label}이면 코드를 맞추세요 (자동으로 합치지 않음).",
                    spec.id_field, r.row, idcol, code, similar_codes=variants)
            continue
        diffs = []
        old_name = getattr(p, spec.name_field)
        if name and norm_name(name) != norm_name(old_name):
            diffs.append((spec.name_field, name, old_name))
        if unit and unit != p.base_unit:
            diffs.append(("base_unit", unit, p.base_unit))
        if spec.has_type and ptype and ptype != p.product_type:
            diffs.append(("product_type", ptype, p.product_type))
        if stock is not None and stock != p.safety_stock_qty:
            diffs.append(("safety_stock_qty", format(stock, "f"), format(p.safety_stock_qty, "f")))
        for fld, new, old in diffs:
            add("ERROR", f"EXISTING_{spec.prefix}_CONFLICT", f"{id_label} '{code}' 는 이미 등록되어 있고 {fld} 값이 다릅니다 (등록: {old}, 파일: {new}). 덮어쓰지 않습니다.",
                fld, r.row, f[fld].col, new, existing=old)
        if not diffs:
            add("WARNING", f"{spec.prefix}_ALREADY_REGISTERED", f"{id_label} '{code}' 는 같은 값으로 이미 등록되어 있어 다시 반영하지 않습니다.", spec.id_field, r.row, idcol, code)
            same.add(r.row)
    return same


def _check_conflicts(parsed, f, sname, add, id_field: str = "product_id", name_field: str = "product_name", label: str = "제품") -> None:
    """코드↔품명 충돌과 코드 표기 변형. 덮어쓰거나 한쪽을 고르지 않고 관련 행 전부에 ERROR/WARNING 을 남긴다."""
    if name_field in f:
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
                        add("ERROR", "CODE_NAME_CONFLICT", f"{label}코드 '{code}' 에 서로 다른 품명이 있습니다: {sorted(names)}", name_field, row, f[name_field].col,
                            names={n: v[:10] for n, v in names.items()})
        for name, codes in by_name.items():
            if len(codes) > 1:
                for code, rows in codes.items():
                    for row in rows[:MAX_ISSUES_PER_CODE]:
                        add("ERROR", "NAME_CODE_CONFLICT", f"품명 '{name}' 에 서로 다른 {label}코드가 있습니다: {sorted(codes)}", id_field, row, f[id_field].col,
                            codes={c: v[:10] for c, v in codes.items()})
    variants: dict[str, dict[str, list[int]]] = {}
    for r, code, *_ in parsed:
        if code:
            variants.setdefault(_variant_key(code), {}).setdefault(code, []).append(r.row)
    for key, codes in variants.items():
        if len(codes) > 1:
            for code, rows in codes.items():
                for row in rows[:MAX_ISSUES_PER_CODE]:
                    add("WARNING", "CODE_VARIANT", f"선행 0·대소문자만 다른 {label}코드가 함께 있습니다: {sorted(codes)}. 같은 {label}이면 통일하세요 (자동으로 합치지 않음).",
                        id_field, row, f[id_field].col, code)
