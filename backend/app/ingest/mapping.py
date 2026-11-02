"""별칭 기반 기본 필드 매핑. 검색기·임베딩·LLM 이 아니라 '정확히 정규화해서 일치하는 별칭'만 쓴다.

원칙 (PoC E10): 점수를 지어내지 않는다. 방식(method)만 기록하고, 근거가 약하거나 애매하면 HOLD 로 둬서 사람이 지정하게 한다.
어떤 경우에도 자동 승인은 없다 (approval_required 는 항상 True, 이 모듈은 승인에 관여하지 않는다).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field as dc_field

from app.ingest.parsing import ParsedSheet, col_letter

_PRODUCT_ID = {"strong": ["product_id", "productid", "product_code", "productcode", "제품코드", "품목코드", "제품id", "품번", "itemcode", "item_code", "sku"],
               "weak": ["code", "코드", "id", "제품", "품목"]}
_PRODUCT_NAME = {"strong": ["product_name", "productname", "제품명", "품목명", "품명", "itemname"], "weak": ["name", "이름", "명칭"]}

_BASE_UNIT = {"required": True, "strong": ["base_unit", "baseunit", "기준단위", "기본단위", "unit", "uom", "단위", "수량단위"], "weak": ["단위명"]}
_SAFETY_STOCK = {"required": False, "strong": ["safety_stock_qty", "safety_stock", "safetystock", "안전재고", "안전재고량", "안전재고수량"], "weak": []}

# entity 별 필드: 필수 여부, 강한 별칭(그 열 이름이면 거의 확실), 약한 별칭(흔한 일반 단어 — 매핑은 하되 검토 표시)
ENTITY_FIELDS: dict[str, dict[str, dict]] = {
    "monthly_demand": {
        "product_id": {"required": True, **_PRODUCT_ID},
        "product_name": {"required": False, **_PRODUCT_NAME},
        "period": {"required": True,
                   "strong": ["period", "yyyymm", "년월", "수요월", "기준월", "주문월", "target_month", "기간"],
                   "weak": ["month", "월", "date", "날짜", "일자"]},
        "demand_qty": {"required": True,
                       "strong": ["demand_qty", "demandqty", "수요량", "월수요량", "월별수요량", "monthly_demand"],
                       "weak": ["demand", "수요", "qty", "quantity", "수량", "주문수량", "판매량", "출고량"]},
        "unit": {"required": False, "strong": ["unit", "uom", "단위", "수량단위"], "weak": ["단위명"]},
    },
    "product": {
        "product_id": {"required": True, **_PRODUCT_ID},
        "product_name": {"required": True, **_PRODUCT_NAME},
        "base_unit": _BASE_UNIT,
        "product_type": {"required": False, "strong": ["product_type", "producttype", "제품유형", "제품구분", "품목유형", "품목구분"],
                         "weak": ["type", "유형", "구분"]},
        "safety_stock_qty": _SAFETY_STOCK,
    },
    "material": {
        "material_id": {"required": True, "strong": ["material_id", "material_code", "자재코드", "원자재코드", "자재id"], "weak": ["code", "코드", "id", "자재"]},
        "material_name": {"required": True, "strong": ["material_name", "자재명", "원자재명"], "weak": ["name", "이름", "명칭", "품명"]},
        "base_unit": _BASE_UNIT,
        "safety_stock_qty": _SAFETY_STOCK,
    },
    "bom": {
        "bom_id": {"required": True, "strong": ["bom_id", "bom_code", "bom코드", "bom번호"], "weak": ["bom"]},
        "product_id": {"required": True, "strong": _PRODUCT_ID["strong"] + ["상위품목코드", "모품목코드", "parent_item"], "weak": ["제품", "모품목"]},
        "bom_version": {"required": True, "strong": ["bom_version", "version", "버전", "bom버전", "리비전", "revision"], "weak": ["개정", "rev"]},
        "status": {"required": True, "strong": ["status", "bom_status", "bom상태", "승인상태"], "weak": ["상태"]},
        "is_active": {"required": True, "strong": ["is_active", "active", "활성", "활성여부", "사용여부"], "weak": []},
        "valid_from": {"required": True, "strong": ["valid_from", "유효시작일", "적용시작일"], "weak": ["시작일"]},
        "valid_to": {"required": False, "strong": ["valid_to", "유효종료일", "적용종료일"], "weak": ["종료일"]},
        "base_qty": {"required": False, "strong": ["base_qty", "기준수량", "기준생산량"], "weak": []},
        "line_no": {"required": True, "strong": ["line_no", "라인번호", "행번호", "순번", "항번"], "weak": []},
        "component_type": {"required": True, "strong": ["component_type", "구성유형", "구성품유형", "자재구분"], "weak": ["유형", "구분", "type"]},
        "component_id": {"required": True, "strong": ["component_id", "component_code", "구성품코드", "자재코드", "material_code", "하위품목코드"], "weak": ["구성품"]},
        "qty_per": {"required": True, "strong": ["qty_per", "소요량", "단위소요량", "구성수량"], "weak": ["수량", "qty", "quantity"]},
        "unit": {"required": True, "strong": ["unit", "uom", "단위", "수량단위"], "weak": ["단위명"]},
        "scrap_rate": {"required": False, "strong": ["scrap_rate", "scrap", "스크랩률", "스크랩비율", "불량률", "손실률"], "weak": []},
    },
    "inventory": {
        "item_type": {"required": True, "strong": ["item_type", "품목구분", "품목유형"], "weak": ["구분", "유형", "type"]},
        "item_id": {"required": True, "strong": ["item_id", "item_code", "품목코드", "품번", "제품코드", "자재코드", "재고코드"], "weak": ["code", "코드", "id", "품목"]},
        "as_of_date": {"required": True, "strong": ["as_of_date", "기준일", "재고기준일", "기준일자"], "weak": ["date", "날짜", "일자"]},
        "on_hand_qty": {"required": True, "strong": ["on_hand_qty", "on_hand", "현재고", "현재고량", "재고량", "보유재고", "보유수량"], "weak": ["재고", "stock", "qty", "수량"]},
        "allocated_qty": {"required": False, "strong": ["allocated_qty", "allocated", "할당재고", "할당수량", "할당량", "예약재고"], "weak": []},
        "available_qty": {"required": False, "strong": ["available_qty", "available", "가용재고", "가용수량"], "weak": []},
        "unit": {"required": True, "strong": ["unit", "uom", "단위", "수량단위"], "weak": ["단위명"]},
    },
}


def norm_header(s: str) -> str:
    s = re.sub(r"[(（\[][^)）\]]*[)）\]]", "", s)            # '수량(EA)' → '수량'
    return re.sub(r"[\s_\-./·]+", "", s).casefold()


def _build_index(fields: dict[str, dict]) -> dict[str, tuple[str, str]]:
    index: dict[str, tuple[str, str]] = {}
    for fname, spec in fields.items():
        for strength in ("strong", "weak"):
            for alias in spec[strength]:
                k = norm_header(alias)
                assert k not in index or index[k] == (fname, strength), f"별칭 충돌: {alias}"
                index[k] = (fname, strength)
    return index


_INDEX = {entity: _build_index(fields) for entity, fields in ENTITY_FIELDS.items()}


def field_for_header(header: str, entity: str) -> str | None:
    """이 헤더가 해당 entity 의 어떤 필드 별칭인지 (없으면 None)."""
    hit = _INDEX[entity].get(norm_header(header))
    return hit[0] if hit else None


@dataclass
class FieldMapping:
    field: str
    col: int
    header: str
    method: str                  # ALIAS_STRONG | ALIAS_WEAK | USER_OVERRIDE
    review_required: bool        # 약한 별칭이면 True


@dataclass
class MappingHold:
    code: str                    # REQUIRED_FIELD_MISSING | AMBIGUOUS_COLUMNS | OVERRIDE_NOT_FOUND | DUPLICATE_HEADER
    message: str
    field: str | None = None
    candidates: list[str] = dc_field(default_factory=list)


@dataclass
class SheetMapping:
    status: str                  # MAPPED | HOLD | IGNORED
    fields: dict[str, FieldMapping] = dc_field(default_factory=dict)
    unmapped_headers: list[str] = dc_field(default_factory=list)
    holds: list[MappingHold] = dc_field(default_factory=list)


def _find_override(sheet: ParsedSheet, ref: str):
    ref = ref.strip()
    for h in sheet.header:
        if ref == col_letter(h.col) or ref in h.labels or norm_header(ref) in {norm_header(x) for x in h.labels}:
            return h
    return None


def map_sheet(sheet: ParsedSheet, overrides: dict[str, str] | None = None, entity: str = "monthly_demand") -> SheetMapping:
    """overrides: {필드: 헤더 이름 또는 열 문자(C)}. 사용자가 HOLD 를 풀 때 쓴다."""
    overrides = overrides or {}
    fields, index = ENTITY_FIELDS[entity], _INDEX[entity]
    m = SheetMapping("MAPPED")
    cands: dict[str, list[tuple[str, object, str]]] = {}      # field → [(strength, header, label)]
    matched_cols: set[int] = set()
    for h in sheet.header:
        for label in h.labels:
            hit = index.get(norm_header(label))
            if hit:
                cands.setdefault(hit[0], []).append((hit[1], h, label))
                matched_cols.add(h.col)
                break
    seen: dict[str, int] = {}
    for h in sheet.header:
        if h.name:
            seen[norm_header(h.name)] = seen.get(norm_header(h.name), 0) + 1
    dup = sorted(k for k, n in seen.items() if n > 1 and k in index)
    for k in dup:
        m.holds.append(MappingHold("DUPLICATE_HEADER", f"같은 이름의 열이 여럿입니다: '{k}'", index[k][0]))

    for fname, spec in fields.items():
        if fname in overrides:
            h = _find_override(sheet, overrides[fname])
            if h is None:
                m.holds.append(MappingHold("OVERRIDE_NOT_FOUND", f"지정한 열 '{overrides[fname]}' 을 찾을 수 없습니다.", fname))
            else:
                m.fields[fname] = FieldMapping(fname, h.col, h.display, "USER_OVERRIDE", False)
                matched_cols.add(h.col)
            continue
        found = cands.get(fname, [])
        strong = [c for c in found if c[0] == "strong"]
        weak = [c for c in found if c[0] == "weak"]
        pick = strong if strong else weak
        if len(pick) > 1:
            m.holds.append(MappingHold("AMBIGUOUS_COLUMNS", f"'{fname}' 에 해당할 수 있는 열이 {len(pick)}개입니다. 열을 지정해 주세요.",
                                       fname, [c[1].display for c in pick]))
        elif len(pick) == 1:
            strength, h, _ = pick[0]
            m.fields[fname] = FieldMapping(fname, h.col, h.display, "ALIAS_STRONG" if strength == "strong" else "ALIAS_WEAK", strength == "weak")
        elif spec["required"]:
            m.holds.append(MappingHold("REQUIRED_FIELD_MISSING", f"필수 필드 '{fname}' 에 해당하는 열을 찾지 못했습니다.", fname))

    used = {f.col for f in m.fields.values()}
    m.unmapped_headers = [h.display for h in sheet.header if h.col not in used and (h.name or h.group)]
    required_hit = any(fields[f]["required"] for f in m.fields)
    if not m.holds:
        return m
    # 필수 필드를 하나도 못 찾았으면 이 시트는 대상이 아니다(설명 시트 등). 일부만 찾았으면 사람이 풀어야 하는 HOLD.
    m.status = "HOLD" if required_hit or any(h.code in ("AMBIGUOUS_COLUMNS", "OVERRIDE_NOT_FOUND", "DUPLICATE_HEADER") for h in m.holds) else "IGNORED"
    return m
