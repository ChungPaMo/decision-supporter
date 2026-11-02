"""BOM·재고 시트 검증. 공통 규칙(코드 보존, 수량 형식, 단위, 셀 위치)은 validation.py 의 함수를 그대로 쓴다.

참조 검증(제품·자재 등록, 기존 BOM 충돌, 기준 단위)은 master 가 있을 때만 한다 (저장 없는 /ingest/preview 는 master 없음).
PoC 재사용: E05 B01(BOM 순환), B02(활성 버전 기간 겹침), B03(활성은 APPROVED 만), B05(유효기간), V02(할당 초과) 규칙의 개념.
"""

from __future__ import annotations

import datetime as dt
import re
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal

from pydantic import ValidationError

from app.contracts import BomHeaderRow, BomLineRow, BomStatus, ComponentType, InventoryRow, ItemType
from app.ingest.mapping import SheetMapping
from app.ingest.parsing import Cell, ParsedSheet
from app.ingest.validation import (
    MAX_ISSUES_PER_CODE, UNITS, Location, MasterData, SheetValidation, _collector, _read_code, normalize_unit, parse_qty,
)

_DATE_FORMS = [re.compile(r"^([0-9]{4})[-/.]([0-9]{1,2})[-/.]([0-9]{1,2})$"), re.compile(r"^([0-9]{4})([0-9]{2})([0-9]{2})$")]
BOM_STATUS_ALIASES = {"approved": BomStatus.APPROVED.value, "승인": BomStatus.APPROVED.value, "확정": BomStatus.APPROVED.value,
                      "draft": BomStatus.DRAFT.value, "초안": BomStatus.DRAFT.value, "임시": BomStatus.DRAFT.value}
ACTIVE_ALIASES = {"y": True, "yes": True, "true": True, "1": True, "예": True, "활성": True, "사용": True,
                  "n": False, "no": False, "false": False, "0": False, "아니오": False, "비활성": False, "미사용": False}
COMPONENT_ALIASES = {"material": ComponentType.MATERIAL.value, "자재": ComponentType.MATERIAL.value, "원자재": ComponentType.MATERIAL.value,
                     "product": ComponentType.PRODUCT.value, "제품": ComponentType.PRODUCT.value, "반제품": ComponentType.PRODUCT.value}
ITEM_TYPE_ALIASES = {"product": ItemType.PRODUCT.value, "제품": ItemType.PRODUCT.value, "완제품": ItemType.PRODUCT.value, "반제품": ItemType.PRODUCT.value,
                     "material": ItemType.MATERIAL.value, "자재": ItemType.MATERIAL.value, "원자재": ItemType.MATERIAL.value}


@dataclass
class BomLineRecord:
    bom_id: str
    product_id: str
    bom_version: str
    status: str
    is_active: bool
    valid_from: dt.date
    valid_to: dt.date | None
    base_qty: Decimal
    line_no: int
    component_type: str
    component_id: str
    qty_per: Decimal
    unit: str
    scrap_rate: Decimal
    location: Location
    review_required: bool = False


@dataclass
class InventoryRecord:
    item_type: str
    item_id: str
    as_of_date: dt.date
    on_hand_qty: Decimal
    allocated_qty: Decimal
    available_qty: Decimal         # 파일 값이 아니라 항상 on_hand - allocated 로 계산
    unit: str
    location: Location
    review_required: bool = False


def parse_date(cell: Cell) -> tuple[dt.date | None, tuple[str, str] | None]:
    """→ (날짜 | None, (오류코드, 메시지) | None). 존재하지 않는 날짜는 보정하지 않고 오류."""
    s = (cell.value or "").strip()
    if not s:
        return None, ("DATE_MISSING", "날짜가 비어 있습니다.")
    if cell.kind == "number" and "." in s:
        return None, ("DATE_INVALID", f"소수점이 있는 숫자 '{s}' 는 날짜로 해석할 수 없습니다. 'YYYY-MM-DD' 로 입력하세요.")
    for rx in _DATE_FORMS:
        m = rx.match(s)
        if m:
            try:
                return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3))), None
            except ValueError:
                return None, ("DATE_INVALID", f"존재하지 않는 날짜입니다: '{s}'")
    return None, ("DATE_INVALID", f"날짜 형식이 아닙니다: '{s}' (YYYY-MM-DD, YYYY/MM/DD, YYYY.MM.DD, YYYYMMDD 허용)")


def _overlap(a_from, a_to, b_from, b_to) -> bool:
    """양 끝 포함 기간 겹침 (DB EXCLUDE 의 daterange '[]' 와 같은 의미). 종료일 None 은 무기한."""
    return (b_to is None or a_from <= b_to) and (a_to is None or b_from <= a_to)


def _check_item_unit(add, unit, base_unit, label, fld, row, col) -> None:
    """파일 단위와 품목 기준 단위 대조. 다른 종류는 ERROR, 같은 종류의 다른 단위는 WARNING (월별 수요와 같은 규칙)."""
    if not unit or not base_unit or unit == base_unit:
        return
    same_dim = UNITS.get(unit) == UNITS.get(base_unit)
    add("WARNING" if same_dim else "ERROR", "UNIT_DIFFERS_FROM_BASE" if same_dim else "UNIT_DIMENSION_MISMATCH",
        f"{label} 의 기준 단위는 '{base_unit}' 인데 파일 단위는 '{unit}' 입니다." + (" (저장 전 환산 필요)" if same_dim else ""),
        fld, row, col, unit, base_unit=base_unit)


def _unit_cell(add, cell: Cell, fld: str, row: int, col: int) -> str | None:
    raw = (cell.value or "").strip()
    if not raw:
        add("ERROR", "UNIT_MISSING", "단위가 비어 있습니다.", fld, row, col)
        return None
    unit = normalize_unit(raw)
    if unit is None:
        add("ERROR", "UNIT_UNKNOWN", f"알 수 없는 단위: '{raw}' (허용: {', '.join(UNITS)}, 개·킬로그램·톤 등)", fld, row, col, raw)
    return unit


def _enum(add, cell: Cell, aliases: dict, code: str, label: str, fld: str, row: int, col: int):
    raw = (cell.value or "").strip()
    if not raw or raw.casefold() not in aliases:
        add("ERROR", code, f"{label} 값이 올바르지 않습니다: '{raw}'", fld, row, col, raw or None)
        return None
    return aliases[raw.casefold()]


def _positive(add, cell: Cell, label: str, fld: str, row: int, col: int, scale: int) -> Decimal | None:
    """0 보다 큰 수량. 형식 오류는 공통 QTY_* 코드, 0 은 QTY_NOT_POSITIVE."""
    d, err = parse_qty(cell, scale)
    if err:
        add("ERROR", err[0], f"{label}: {err[1]}", fld, row, col, cell.value)
        return None
    if d == 0:
        add("ERROR", "QTY_NOT_POSITIVE", f"{label}는 0 보다 커야 합니다.", fld, row, col, cell.value)
        return None
    return d


# ---------------------------------------------------------------- BOM
def validate_bom(sheet: ParsedSheet, mapping: SheetMapping, default_unit: str | None = None, master: MasterData | None = None) -> SheetValidation:
    """한 행 = 구성품 1줄. BOM 헤더 값(제품, 버전, 상태, 활성, 유효기간, 기준수량)은 같은 bom_id 의 행마다 반복된다."""
    out = SheetValidation(data_rows=len(sheet.rows))
    f = mapping.fields
    sname = sheet.name
    bad_rows: set[int] = set()
    add = _collector(out, sname, bad_rows)
    if default_unit:
        add("WARNING", "DEFAULT_UNIT_IGNORED", "BOM 에서는 단위가 필수 열이라 default_unit 을 쓰지 않습니다.")
    review_all = any(m.review_required for m in f.values())

    rows = []
    for r in sheet.rows:
        def cell(fld):
            m = f.get(fld)
            return r.cells[m.col - 1] if m and m.col - 1 < len(r.cells) else Cell(None, "empty")

        def col(fld):
            return f[fld].col if fld in f else None

        if len(r.cells) != sheet.width:
            add("ERROR", "ROW_COLUMN_COUNT", f"열 개수가 헤더({sheet.width})와 다릅니다 ({len(r.cells)}). 구분자나 따옴표 문제일 수 있습니다.", row=r.row)
            continue
        bom_id, rv1 = _read_code(cell("bom_id"), r.row, col("bom_id"), sname, out, add, "bom_id", "BOM 코드")
        product_id, rv2 = _read_code(cell("product_id"), r.row, col("product_id"), sname, out, add)
        version, rv3 = _read_code(cell("bom_version"), r.row, col("bom_version"), sname, out, add, "bom_version", "BOM 버전")
        component_id, rv4 = _read_code(cell("component_id"), r.row, col("component_id"), sname, out, add, "component_id", "구성품 코드")
        status = _enum(add, cell("status"), BOM_STATUS_ALIASES, "BOM_STATUS_INVALID", "BOM 상태(APPROVED, DRAFT)", "status", r.row, col("status"))
        active = _enum(add, cell("is_active"), ACTIVE_ALIASES, "BOM_ACTIVE_INVALID", "활성 여부(Y, N)", "is_active", r.row, col("is_active"))
        ctype = _enum(add, cell("component_type"), COMPONENT_ALIASES, "BOM_COMPONENT_TYPE_INVALID", "구성품 유형(MATERIAL, PRODUCT)",
                      "component_type", r.row, col("component_type"))
        valid_from, derr = parse_date(cell("valid_from"))
        if derr:
            add("ERROR", derr[0], "유효 시작일: " + derr[1], "valid_from", r.row, col("valid_from"), cell("valid_from").value)
        valid_to = None
        if (cell("valid_to").value or "").strip():
            valid_to, derr = parse_date(cell("valid_to"))
            if derr:
                add("ERROR", derr[0], "유효 종료일: " + derr[1], "valid_to", r.row, col("valid_to"), cell("valid_to").value)
        base_qty = Decimal(1)
        if (cell("base_qty").value or "").strip():
            base_qty = _positive(add, cell("base_qty"), "기준 수량", "base_qty", r.row, col("base_qty"), 4)
        raw_line = (cell("line_no").value or "").strip()
        line_no = int(raw_line) if re.fullmatch(r"[0-9]{1,6}", raw_line) and int(raw_line) >= 1 else None
        if line_no is None:
            add("ERROR", "BOM_LINE_NO_INVALID", f"라인 번호는 1 이상의 정수여야 합니다: '{raw_line}'", "line_no", r.row, col("line_no"), raw_line or None)
        qty_per = _positive(add, cell("qty_per"), "소요량", "qty_per", r.row, col("qty_per"), 6)
        unit = _unit_cell(add, cell("unit"), "unit", r.row, col("unit"))
        scrap = Decimal(0)
        if (cell("scrap_rate").value or "").strip():
            scrap, serr = parse_qty(cell("scrap_rate"), 6)
            if serr:
                add("ERROR", serr[0], "스크랩 비율(0 이상 1 미만 소수, 예: 0.05): " + serr[1], "scrap_rate", r.row, col("scrap_rate"), cell("scrap_rate").value)
            elif scrap >= 1:
                add("ERROR", "SCRAP_RATE_RANGE", f"스크랩 비율은 0 이상 1 미만이어야 합니다: '{cell('scrap_rate').value}' (5% 는 0.05)",
                    "scrap_rate", r.row, col("scrap_rate"), cell("scrap_rate").value)
                scrap = None
        header = (product_id, version, status, active, valid_from, valid_to, base_qty)
        rows.append((r, bom_id, header, line_no, ctype, component_id, qty_per, unit, scrap, rv1 or rv2 or rv3 or rv4 or review_all))

    headers = _check_bom_structure(rows, f, add)
    if master is not None:
        _check_bom_against_master(rows, headers, f, master, add)

    for r, bom_id, h, line_no, ctype, comp, qty, unit, scrap, review in rows:
        if r.row in bad_rows:
            continue
        product_id, version, status, active, valid_from, valid_to, base_qty = h
        try:
            BomHeaderRow(bom_id=bom_id, product_id=product_id, bom_version=version, status=status, is_active=active,
                         valid_from=valid_from, valid_to=valid_to, base_qty=base_qty)
            BomLineRow(bom_id=bom_id, line_no=line_no, component_type=ctype, component_id=comp, qty_per=qty, unit=unit, scrap_rate=scrap)
        except ValidationError as exc:
            add("ERROR", "CONTRACT_VIOLATION", f"데이터 계약 위반: {exc.errors()[0]['msg']}", row=r.row)
            continue
        out.records.append(BomLineRecord(bom_id, product_id, version, status, active, valid_from, valid_to, base_qty, line_no, ctype, comp, qty, unit, scrap,
                                         Location(sname, r.row), review))
    out.error_rows = len(bad_rows)
    return out


_HEADER_FIELDS = ("product_id", "bom_version", "status", "is_active", "valid_from", "valid_to", "base_qty")


def _check_bom_structure(rows, f, add) -> dict:
    """파일 안의 BOM 규칙. 반환: {bom_id: (첫 행 번호, 헤더 값)}."""
    headers: dict[str, tuple[int, tuple]] = {}
    lines: dict[tuple[str, int], list[int]] = defaultdict(list)
    comps: dict[tuple[str, str, str], list[int]] = defaultdict(list)
    for r, bom_id, h, line_no, ctype, comp, *_ in rows:
        if not bom_id:
            continue
        if bom_id not in headers:
            headers[bom_id] = (r.row, h)
        else:
            first_row, first = headers[bom_id]
            for fld, a, b in zip(_HEADER_FIELDS, first, h):
                if a is not None and b is not None and a != b:
                    add("ERROR", "BOM_HEADER_INCONSISTENT", f"BOM '{bom_id}' 의 {fld} 값이 {first_row}행과 다릅니다 ({a} vs {b}).", fld,
                        r.row, f[fld].col if fld in f else None, str(b), first_row=first_row)
        if line_no:
            lines[(bom_id, line_no)].append(r.row)
        if ctype and comp:
            comps[(bom_id, ctype, comp)].append(r.row)
        product_id = h[0]
        if ctype == ComponentType.PRODUCT.value and comp and comp == product_id:
            add("ERROR", "BOM_SELF_REFERENCE", f"제품 '{comp}' 가 자기 자신의 구성품입니다.", "component_id", r.row, f["component_id"].col, comp)

    for (bom_id, line_no), line_rows in lines.items():
        if len(line_rows) > 1:
            for row in line_rows[:MAX_ISSUES_PER_CODE]:
                add("ERROR", "BOM_LINE_DUPLICATE", f"BOM '{bom_id}' 의 라인 {line_no} 이 {line_rows[:10]} 행에 중복됩니다.", "line_no", row, f["line_no"].col, str(line_no))
    for (bom_id, _t, comp), comp_rows in comps.items():
        if len(comp_rows) > 1:
            for row in comp_rows[:MAX_ISSUES_PER_CODE]:
                add("WARNING", "BOM_COMPONENT_REPEATED", f"BOM '{bom_id}' 에 구성품 '{comp}' 가 여러 라인에 있습니다: {comp_rows[:10]}행. 의도한 것인지 확인하세요.",
                    "component_id", row, f["component_id"].col, comp)

    by_version: dict[tuple[str, str], list[str]] = defaultdict(list)
    for bom_id, (first_row, h) in headers.items():
        product_id, version, status, active, valid_from, valid_to, _b = h
        if valid_from and valid_to and valid_to < valid_from:
            add("ERROR", "BOM_PERIOD_INVALID", f"BOM '{bom_id}' 의 유효 종료일({valid_to})이 시작일({valid_from})보다 빠릅니다.", "valid_to", first_row, f["valid_to"].col, str(valid_to))
        if active and status == BomStatus.DRAFT.value:
            add("ERROR", "BOM_ACTIVE_NOT_APPROVED", f"BOM '{bom_id}' 는 DRAFT 라 활성화할 수 없습니다.", "is_active", first_row, f["is_active"].col, "Y")
        if product_id and version:
            by_version[(product_id, version)].append(bom_id)
    for (product_id, version), ids in by_version.items():
        if len(ids) > 1:
            for bom_id in ids:
                add("ERROR", "BOM_VERSION_CONFLICT", f"제품 '{product_id}' 의 버전 '{version}' 이 여러 BOM 에 있습니다: {sorted(ids)}", "bom_version",
                    headers[bom_id][0], f["bom_version"].col, version, bom_ids=sorted(ids))

    actives = sorted((b, row, h) for b, (row, h) in headers.items() if h[3] and h[0] and h[4])
    for i, (a, a_row, ah) in enumerate(actives):
        for b, b_row, bh in actives[i + 1:]:
            if ah[0] == bh[0] and _overlap(ah[4], ah[5], bh[4], bh[5]):
                for bom_id, row in ((a, a_row), (b, b_row)):
                    add("ERROR", "BOM_ACTIVE_OVERLAP", f"제품 '{ah[0]}' 의 활성 BOM '{a}' 와 '{b}' 의 유효기간이 겹칩니다.", "valid_from", row, f["valid_from"].col, bom_id)
    return headers


def _check_bom_against_master(rows, headers, f, master: MasterData, add) -> None:
    """참조·기존 BOM 대조. 등록되지 않은 제품·자재를 만들지 않고 ERROR 로 막는다 (승인 후 재검증하면 풀린다)."""
    for r, bom_id, h, _line, ctype, comp, _q, unit, _s, _rv in rows:
        product_id = h[0]
        if product_id and product_id not in master.products:
            variants = master.code_variants(product_id)
            add("ERROR", "PRODUCT_NOT_REGISTERED", f"제품 마스터에 없는 제품코드입니다: '{product_id}'. 제품 마스터를 먼저 승인한 뒤 다시 검증하세요.",
                "product_id", r.row, f["product_id"].col, product_id, similar_codes=variants)
        if not (ctype and comp):
            continue
        known = master.materials if ctype == ComponentType.MATERIAL.value else master.products
        item = known.get(comp)
        if item is None:
            what, code = ("자재", "MATERIAL_NOT_REGISTERED") if ctype == ComponentType.MATERIAL.value else ("제품", "PRODUCT_NOT_REGISTERED")
            add("ERROR", code, f"{what} 마스터에 없는 구성품 코드입니다: '{comp}'. {what} 마스터를 먼저 승인한 뒤 다시 검증하세요.",
                "component_id", r.row, f["component_id"].col, comp, similar_codes=master.code_variants(comp, known))
        else:
            _check_item_unit(add, unit, item.base_unit, f"구성품 '{comp}'", "unit", r.row, f["unit"].col)

    existing_ids = {b.bom_id for b in master.boms}
    for bom_id, (row, h) in headers.items():
        product_id, version, _st, active, valid_from, valid_to, _b = h
        if bom_id in existing_ids:
            add("ERROR", "EXISTING_BOM_CONFLICT", f"BOM '{bom_id}' 는 이미 승인되어 있습니다. BOM 은 수정하지 않고 새 버전으로 등록합니다.", "bom_id", row, f["bom_id"].col, bom_id)
        for known in master.boms:
            if known.product_id != product_id or known.bom_id == bom_id:
                continue
            if known.bom_version == version:
                add("ERROR", "BOM_VERSION_CONFLICT", f"제품 '{product_id}' 의 버전 '{version}' 은 BOM '{known.bom_id}' 로 이미 승인되어 있습니다.",
                    "bom_version", row, f["bom_version"].col, version, bom_ids=[known.bom_id])
            if active and valid_from and known.is_active and _overlap(valid_from, valid_to, known.valid_from, known.valid_to):
                add("ERROR", "BOM_ACTIVE_OVERLAP", f"제품 '{product_id}' 의 활성 BOM '{known.bom_id}' ({known.valid_from} ~ {known.valid_to or '무기한'})와 유효기간이 겹칩니다.",
                    "valid_from", row, f["valid_from"].col, str(valid_from), existing_bom_id=known.bom_id)

    # BOM 순환: 승인된 BOM 과 이 파일을 합친 제품 → 하위 제품 관계에서 자기 자신으로 돌아오면 소요량 전개가 끝나지 않는다
    edges: dict[str, set[str]] = defaultdict(set)
    for b in master.boms:
        edges[b.product_id].update(b.component_products)
    for _r, _bom, h, _line, ctype, comp, *_ in rows:
        if h[0] and comp and ctype == ComponentType.PRODUCT.value and comp != h[0]:
            edges[h[0]].add(comp)
    for bom_id, (row, h) in headers.items():
        start = h[0]
        if not start:
            continue
        seen, stack = set(), list(edges.get(start, ()))
        while stack:
            node = stack.pop()
            if node == start:
                add("ERROR", "BOM_CYCLE", f"제품 '{start}' 의 BOM 구성이 다시 '{start}' 로 돌아옵니다 (순환).", "component_id", row, f["component_id"].col, start)
                break
            if node not in seen:
                seen.add(node)
                stack.extend(edges.get(node, ()))


# ---------------------------------------------------------------- 재고
def validate_inventory(sheet: ParsedSheet, mapping: SheetMapping, default_unit: str | None = None, master: MasterData | None = None) -> SheetValidation:
    """한 행 = (품목 구분, 품목, 기준일) 재고 1건. 가용재고는 저장하지 않고 현재고 - 할당재고 로 계산하며, 파일 값이 있으면 대조만 한다."""
    out = SheetValidation(data_rows=len(sheet.rows))
    f = mapping.fields
    sname = sheet.name
    bad_rows: set[int] = set()
    add = _collector(out, sname, bad_rows)
    if default_unit:
        add("WARNING", "DEFAULT_UNIT_IGNORED", "재고에서는 단위가 필수 열이라 default_unit 을 쓰지 않습니다.")
    if "allocated_qty" not in f:
        add("WARNING", "ALLOCATED_DEFAULTED", "할당재고 열이 없어 모든 행을 0 으로 처리합니다. 확인하세요.")
    review_all = any(m.review_required for m in f.values())

    parsed = []
    for r in sheet.rows:
        def cell(fld):
            m = f.get(fld)
            return r.cells[m.col - 1] if m and m.col - 1 < len(r.cells) else Cell(None, "empty")

        if len(r.cells) != sheet.width:
            add("ERROR", "ROW_COLUMN_COUNT", f"열 개수가 헤더({sheet.width})와 다릅니다 ({len(r.cells)}). 구분자나 따옴표 문제일 수 있습니다.", row=r.row)
            continue
        item_type = _enum(add, cell("item_type"), ITEM_TYPE_ALIASES, "ITEM_TYPE_INVALID", "품목 구분(PRODUCT, MATERIAL)", "item_type", r.row, f["item_type"].col)
        item_id, rv = _read_code(cell("item_id"), r.row, f["item_id"].col, sname, out, add, "item_id", "품목 코드")
        as_of, derr = parse_date(cell("as_of_date"))
        if derr:
            add("ERROR", derr[0], "기준일: " + derr[1], "as_of_date", r.row, f["as_of_date"].col, cell("as_of_date").value)
        on_hand, qerr = parse_qty(cell("on_hand_qty"))
        if qerr:
            add("ERROR", qerr[0], "현재고: " + qerr[1], "on_hand_qty", r.row, f["on_hand_qty"].col, cell("on_hand_qty").value)
        allocated = Decimal(0)
        if "allocated_qty" in f:
            if (cell("allocated_qty").value or "").strip():
                allocated, aerr = parse_qty(cell("allocated_qty"))
                if aerr:
                    add("ERROR", aerr[0], "할당재고: " + aerr[1], "allocated_qty", r.row, f["allocated_qty"].col, cell("allocated_qty").value)
            else:
                add("WARNING", "ALLOCATED_DEFAULTED", "할당재고가 비어 있어 0 으로 처리합니다. 확인하세요.", "allocated_qty", r.row, f["allocated_qty"].col)
        if on_hand is not None and allocated is not None and allocated > on_hand:
            add("ERROR", "ALLOCATED_EXCEEDS_ON_HAND", f"할당재고({allocated})가 현재고({on_hand})보다 큽니다.", "allocated_qty", r.row,
                f["allocated_qty"].col, format(allocated, "f"))
        if "available_qty" in f and (cell("available_qty").value or "").strip():
            stated, verr = parse_qty(cell("available_qty"))
            if verr:
                add("ERROR", verr[0], "가용재고: " + verr[1], "available_qty", r.row, f["available_qty"].col, cell("available_qty").value)
            elif on_hand is not None and allocated is not None and stated != on_hand - allocated:
                add("ERROR", "AVAILABLE_MISMATCH", f"가용재고({stated})가 현재고 - 할당재고({on_hand - allocated})와 다릅니다.", "available_qty", r.row,
                    f["available_qty"].col, format(stated, "f"), expected=format(on_hand - allocated, "f"))
        unit = _unit_cell(add, cell("unit"), "unit", r.row, f["unit"].col)
        parsed.append((r, item_type, item_id, as_of, on_hand, allocated, unit, rv or review_all))

    # 같은 품목·기준일 중복: 값이 같아도 어느 행이 맞는지 고르지 않는다
    by_key: dict[tuple, list[int]] = defaultdict(list)
    for r, item_type, item_id, as_of, *_ in parsed:
        if item_type and item_id and as_of:
            by_key[(item_type, item_id, as_of)].append(r.row)
    for (item_type, item_id, as_of), key_rows in by_key.items():
        if len(key_rows) > 1:
            for row in key_rows[:MAX_ISSUES_PER_CODE]:
                add("ERROR", "INVENTORY_DUPLICATE", f"같은 품목·기준일이 {key_rows[:10]} 행에 있습니다: {item_type} {item_id} {as_of}.", "item_id", row, f["item_id"].col, item_id,
                    rows=key_rows[:10])

    if master is not None:
        for r, item_type, item_id, _d, _o, _a, unit, _rv in parsed:
            if not (item_type and item_id):
                continue
            is_product = item_type == ItemType.PRODUCT.value
            known = master.products if is_product else master.materials
            item = known.get(item_id)
            if item is None:
                what, code = ("제품", "PRODUCT_NOT_REGISTERED") if is_product else ("자재", "MATERIAL_NOT_REGISTERED")
                add("ERROR", code, f"{what} 마스터에 없는 품목입니다: '{item_id}'. {what} 마스터를 먼저 승인한 뒤 다시 검증하세요.", "item_id", r.row, f["item_id"].col, item_id,
                    similar_codes=master.code_variants(item_id, known))
            else:
                _check_item_unit(add, unit, item.base_unit, f"품목 '{item_id}'", "unit", r.row, f["unit"].col)

    for r, item_type, item_id, as_of, on_hand, allocated, unit, review in parsed:
        if r.row in bad_rows:
            continue
        try:
            InventoryRow(item_type=item_type, item_id=item_id, as_of_date=as_of, on_hand_qty=on_hand, allocated_qty=allocated, unit=unit)
        except ValidationError as exc:
            add("ERROR", "CONTRACT_VIOLATION", f"데이터 계약 위반: {exc.errors()[0]['msg']}", row=r.row)
            continue
        out.records.append(InventoryRecord(item_type, item_id, as_of, on_hand, allocated, on_hand - allocated, unit, Location(sname, r.row), review))
    out.error_rows = len(bad_rows)
    return out
