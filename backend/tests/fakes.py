"""테스트 전용 인메모리 저장소. 실제 저장소가 아니며 DB 동작(트리거·제약·잠금)을 증명하지 않는다.

서비스 로직 검증을 위해 DB 규칙 중 서비스가 의존하는 것만 흉내 낸다:
  - 트랜잭션 롤백: 블록이 예외로 끝나면 시작 시점 상태로 복원 (rollbacks 카운트)
  - 상태 전이: READY_FOR_APPROVAL 에서만 APPROVED, 최종 상태 불변
  - 업무 행 쓰기는 같은 트랜잭션에서 승인된, entity 가 일치하는 배치에만 허용, 키 중복 불가
  - 활성 BOM 기간 겹침 거부 (EXCLUDE 의 모형), BOM 구성품 줄을 approved_record_count 로 셈
장애 주입: fail_insert_after(N건 쓴 뒤 실패), unavailable(연결 불가)
"""

from __future__ import annotations

import copy
import datetime as dt
import uuid
from contextlib import contextmanager
from decimal import Decimal

from app.batches.repository import (
    ApprovedBom, ApprovedDemand, ApprovedInventory, ApprovedMaterial, ApprovedProduct, BatchRecord, BomHeaderInsert, BomLineInsert, DemandRow,
    DuplicateBatchError, InventoryInsert, MaterialInsert, ProductInsert, RepositoryError, RepositoryUnavailable, ValidationUpdate,
)
from app.ingest.validation import KnownBom, KnownMaterial, KnownProduct

FINAL = {"APPROVED", "REJECTED"}


class _State:
    def __init__(self) -> None:
        self.batches: dict[uuid.UUID, BatchRecord] = {}
        self.files: dict[uuid.UUID, bytes] = {}
        self.demand: dict[tuple[str, str], tuple[DemandRow, uuid.UUID]] = {}
        self.products: dict[str, tuple[ProductInsert, uuid.UUID | None]] = {}    # 시드 제품의 batch_id 는 None
        self.materials: dict[str, tuple[MaterialInsert, uuid.UUID | None]] = {}
        self.boms: dict[str, tuple[BomHeaderInsert, list[BomLineInsert], uuid.UUID]] = {}
        self.inventory: dict[tuple, tuple[InventoryInsert, uuid.UUID]] = {}


class FakeTx:
    def __init__(self, repo: "InMemoryBatchRepository") -> None:
        self.r = repo
        self.s = repo.state
        self.approved_in_tx: set[uuid.UUID] = set()
        self.written = 0

    def get_batch(self, batch_id, lock=False):
        b = self.s.batches.get(batch_id)
        return copy.deepcopy(b) if b else None

    def list_batches(self, status, limit, offset):
        items = sorted(self.s.batches.values(), key=lambda b: (b.created_at, str(b.batch_id)), reverse=True)
        if status:
            items = [b for b in items if b.status == status]
        return [copy.deepcopy(b) for b in items[offset:offset + limit]], len(items)

    def find_live_by_sha(self, sha256, entity):
        return next((copy.deepcopy(b) for b in self.s.batches.values()
                     if b.source_sha256 == sha256 and b.entity == entity and b.status != "REJECTED"), None)

    def insert_batch(self, rec, content):
        if rec.status in FINAL:
            raise RepositoryError("new batch cannot start in a final state")
        if any(b.source_sha256 == rec.source_sha256 and b.entity == rec.entity and b.status != "REJECTED" for b in self.s.batches.values()):
            raise DuplicateBatchError()
        self.s.batches[rec.batch_id] = copy.deepcopy(rec)
        self.s.files[rec.batch_id] = bytes(content)

    def get_file(self, batch_id):
        return self.s.files[batch_id]

    def _mutable(self, batch_id):
        b = self.s.batches[batch_id]
        if b.status in FINAL:
            raise RepositoryError(f"batch is {b.status} and immutable")
        return b

    def update_validation(self, batch_id, u: ValidationUpdate):
        b = self._mutable(batch_id)
        b.status, b.default_unit, b.column_overrides, b.sheet_filter = u.status, u.default_unit, dict(u.column_overrides), u.sheet_filter
        b.blocking_error_count, b.warning_count, b.result_digest = u.blocking_error_count, u.warning_count, u.result_digest
        b.validation_result = copy.deepcopy(u.validation_result)
        b.version += 1
        return copy.deepcopy(b)

    def mark_approved(self, batch_id, approved_by, record_count):
        b = self._mutable(batch_id)
        if b.status != "READY_FOR_APPROVAL":
            raise RepositoryError(f"only READY_FOR_APPROVAL can be approved (was {b.status})")
        b.status, b.approved_by, b.approved_record_count = "APPROVED", approved_by, record_count
        b.approved_at = self.r.now()
        b.version += 1
        self.approved_in_tx.add(batch_id)
        return copy.deepcopy(b)

    def mark_rejected(self, batch_id, rejected_by, reason):
        b = self._mutable(batch_id)
        b.status, b.rejected_by, b.reject_reason, b.rejected_at = "REJECTED", rejected_by, reason, self.r.now()
        b.version += 1
        return copy.deepcopy(b)

    def list_known_products(self):
        return [KnownProduct(p.product_id, p.product_name, p.base_unit, p.product_type, p.safety_stock_qty) for p, _ in self.s.products.values()]

    def list_known_materials(self):
        return [KnownMaterial(m.material_id, m.material_name, m.base_unit, m.safety_stock_qty) for m, _ in self.s.materials.values()]

    def list_known_boms(self):
        return [KnownBom(h.bom_id, h.product_id, h.bom_version, h.is_active, h.valid_from, h.valid_to,
                         tuple(x.component_id for x in lines if x.component_type == "PRODUCT")) for h, lines, _ in self.s.boms.values()]

    def _tick(self):
        if self.r.fail_insert_after is not None and self.written >= self.r.fail_insert_after:
            raise RepositoryError("injected failure")
        self.written += 1

    def insert_materials(self, batch_id, rows: list[MaterialInsert]):
        self._writable(batch_id, "material")
        for r in rows:
            self._tick()
            if r.material_id in self.s.materials:
                raise RepositoryError("duplicate key")
            self.s.materials[r.material_id] = (r, batch_id)
        return len(rows)

    def insert_bom(self, batch_id, headers, lines):
        self._writable(batch_id, "bom")
        for h in headers:
            if h.bom_id in self.s.boms or h.product_id not in self.s.products:
                raise RepositoryError("duplicate key or missing product")
            for other, _l, _b in self.s.boms.values():
                if (h.is_active and other.is_active and other.product_id == h.product_id
                        and (other.valid_to is None or h.valid_from <= other.valid_to) and (h.valid_to is None or other.valid_from <= h.valid_to)):
                    raise RepositoryError("active BOM overlap")
            self.s.boms[h.bom_id] = (h, [], batch_id)
        for x in lines:
            self._tick()
            self.s.boms[x.bom_id][1].append(x)
        return len(lines)

    def existing_inventory_keys(self, keys):
        return sorted(k for k in keys if k in self.s.inventory)

    def insert_inventory(self, batch_id, rows: list[InventoryInsert]):
        self._writable(batch_id, "inventory")
        for r in rows:
            self._tick()
            key = (r.item_type, r.item_id, r.as_of_date)
            known = self.s.products if r.item_type == "PRODUCT" else self.s.materials
            if key in self.s.inventory or r.item_id not in known:
                raise RepositoryError("duplicate key or missing item")
            self.s.inventory[key] = (r, batch_id)
        return len(rows)

    def _source(self, bid):
        return self.s.batches[bid].source_type if bid else "SYNTHETIC"

    def list_materials(self, material_id, q, limit, offset):
        out = [ApprovedMaterial(mid, m.material_name, m.base_unit, m.safety_stock_qty, self._source(bid), bid)
               for mid, (m, bid) in sorted(self.s.materials.items())
               if (not material_id or mid == material_id) and (not q or q.casefold() in m.material_name.casefold())]
        return out[offset:offset + limit], len(out)

    def list_boms(self, product_id, bom_id, active_only, limit, offset):
        items = sorted(self.s.boms.values(), key=lambda v: (v[0].product_id, v[0].bom_version))
        out = [ApprovedBom(h, sorted(lines, key=lambda x: x.line_no), self._source(bid), bid) for h, lines, bid in items
               if (not product_id or h.product_id == product_id) and (not bom_id or h.bom_id == bom_id) and (not active_only or h.is_active)]
        return copy.deepcopy(out[offset:offset + limit]), len(out)

    def list_inventory(self, item_type, item_id, as_of_date, limit, offset):
        out = [ApprovedInventory(r.item_type, r.item_id, r.as_of_date, r.on_hand_qty, r.allocated_qty, r.unit, self._source(bid), bid)
               for key, (r, bid) in sorted(self.s.inventory.items())
               if (not item_type or r.item_type == item_type) and (not item_id or r.item_id == item_id) and (not as_of_date or r.as_of_date == as_of_date)]
        return out[offset:offset + limit], len(out)

    def _writable(self, batch_id, entity):
        if batch_id not in self.approved_in_tx or self.s.batches[batch_id].entity != entity:
            raise RepositoryError("rows can only be written by the approving transaction of a matching entity")

    def insert_products(self, batch_id, rows: list[ProductInsert]):
        self._writable(batch_id, "product")
        for r in rows:
            if self.r.fail_insert_after is not None and self.written >= self.r.fail_insert_after:
                raise RepositoryError("injected failure")
            if r.product_id in self.s.products:
                raise RepositoryError("duplicate key")
            self.s.products[r.product_id] = (r, batch_id)
            self.written += 1
        return len(rows)

    def list_products(self, product_id, q, limit, offset):
        out = []
        for pid, (row, bid) in sorted(self.s.products.items()):
            if (product_id and pid != product_id) or (q and q.casefold() not in row.product_name.casefold()):
                continue
            source = self.s.batches[bid].source_type if bid else "SYNTHETIC"
            out.append(ApprovedProduct(pid, row.product_name, row.base_unit, row.product_type, row.safety_stock_qty, source, bid))
        return out[offset:offset + limit], len(out)

    def existing_demand_keys(self, keys):
        return sorted(k for k in keys if k in self.s.demand)

    def insert_monthly_demand(self, batch_id, rows: list[DemandRow]):
        self._writable(batch_id, "monthly_demand")
        for r in rows:
            if self.r.fail_insert_after is not None and self.written >= self.r.fail_insert_after:
                raise RepositoryError("injected failure")
            if (r.product_id, r.period) in self.s.demand:
                raise RepositoryError("duplicate key")
            self.s.demand[(r.product_id, r.period)] = (r, batch_id)
            self.written += 1
        return len(rows)

    def list_approved_demand(self, product_id, period_from, period_to, limit, offset):
        out = []
        for (pid, per), (row, bid) in sorted(self.s.demand.items()):
            b = self.s.batches[bid]
            if b.status != "APPROVED" or b.blocking_error_count != 0:      # 뷰 v_forecast_input_monthly_demand 의 조건
                continue
            if product_id and pid != product_id or (period_from and per < period_from) or (period_to and per > period_to):
                continue
            out.append(ApprovedDemand(pid, per, row.demand_qty, row.unit, b.source_type, bid))
        return out[offset:offset + limit], len(out)


class InMemoryBatchRepository:
    def __init__(self, products: dict[str, str] | None = None, materials: dict[str, tuple[str, str]] | None = None) -> None:
        """products: 이미 승인되어 있다고 가정할 제품 {코드: 품명} (기준 단위 EA). materials: {코드: (자재명, 기준 단위)}."""
        self.state = _State()
        for pid, name in (products or {}).items():
            self.state.products[pid] = (ProductInsert(pid, name, "EA", None, Decimal(0)), None)
        for mid, (name, unit) in (materials or {}).items():
            self.state.materials[mid] = (MaterialInsert(mid, name, unit, Decimal(0)), None)
        self.fail_insert_after: int | None = None
        self.unavailable = False
        self.rollbacks = 0
        self.commits = 0
        self._clock = dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc)

    def now(self):
        self._clock += dt.timedelta(seconds=1)
        return self._clock

    @contextmanager
    def transaction(self):
        if self.unavailable:
            raise RepositoryUnavailable()
        backup = copy.deepcopy(self.state)
        try:
            yield FakeTx(self)
        except BaseException:
            self.state = backup
            self.rollbacks += 1
            raise
        self.commits += 1
