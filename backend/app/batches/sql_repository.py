"""PostgreSQL 저장소. 2026-11-10 PostgreSQL 16.6에서 합성 입력으로 실제 검증했다.
범위와 남은 제약은 docs/postgresql_integration_verification_20261110.md 참고 (운영 검증은 아님).

트랜잭션 경계: transaction() 한 번 = engine.begin() 한 번. 예외가 나가면 SQLAlchemy 가 롤백한다.
승인 게이트의 최종 강제는 DB 트리거(migrations 0002)가 한다: 이 클래스가 규칙을 어겨도 DB 가 거부한다.
"""

from __future__ import annotations

import json
import uuid
from contextlib import contextmanager

from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError, InterfaceError, OperationalError

from app.batches.repository import (
    ApprovedBom, ApprovedDemand, ApprovedInventory, ApprovedMaterial, ApprovedProduct, BatchRecord, BomHeaderInsert, BomLineInsert, DemandRow,
    DuplicateBatchError, InventoryInsert, MaterialInsert, ProductInsert, RepositoryError, RepositoryUnavailable, ValidationUpdate,
)
from app.ingest.validation import KnownBom, KnownMaterial, KnownProduct

_COLS = """batch_id, entity, status, source_type, source_name, source_note, source_sha256, created_by, uploaded_at AS created_at,
  default_unit, column_overrides, sheet_filter, blocking_error_count, warning_count, result_digest, validation_result, version,
  approved_by, approved_at, approved_record_count, rejected_by, rejected_at, reject_reason"""


def _rec(row) -> BatchRecord:
    return BatchRecord(**dict(row._mapping))


class SqlBatchTx:
    def __init__(self, conn) -> None:
        self.c = conn

    def get_batch(self, batch_id: uuid.UUID, lock: bool = False) -> BatchRecord | None:
        row = self.c.execute(text(f"SELECT {_COLS} FROM import_batch WHERE batch_id = :id" + (" FOR UPDATE" if lock else "")), {"id": batch_id}).first()
        return _rec(row) if row else None

    def list_batches(self, status: str | None, limit: int, offset: int) -> tuple[list[BatchRecord], int]:
        where, p = ("WHERE status = :st", {"st": status}) if status else ("", {})
        total = self.c.execute(text(f"SELECT count(*) FROM import_batch {where}"), p).scalar_one()
        rows = self.c.execute(text(f"SELECT {_COLS} FROM import_batch {where} ORDER BY uploaded_at DESC, batch_id LIMIT :l OFFSET :o"),
                              {**p, "l": limit, "o": offset}).all()
        return [_rec(r) for r in rows], total

    def find_live_by_sha(self, sha256: str, entity: str) -> BatchRecord | None:
        row = self.c.execute(text(f"SELECT {_COLS} FROM import_batch WHERE source_sha256 = :s AND entity = :e AND status <> 'REJECTED'"),
                             {"s": sha256, "e": entity}).first()
        return _rec(row) if row else None

    def insert_batch(self, rec: BatchRecord, content: bytes) -> None:
        self.c.execute(text(
            "INSERT INTO import_batch (batch_id, entity, status, source_type, source_name, source_note, source_sha256, created_by, default_unit,"
            " column_overrides, sheet_filter, blocking_error_count, warning_count, result_digest, validation_result)"
            " VALUES (:id, :entity, :status, :st, :name, :note, :sha, :by, :du, CAST(:co AS jsonb), :sheet, :err, :warn, :dig, CAST(:vr AS jsonb))"),
            {"id": rec.batch_id, "entity": rec.entity, "status": rec.status, "st": rec.source_type, "name": rec.source_name, "note": rec.source_note,
             "sha": rec.source_sha256, "by": rec.created_by, "du": rec.default_unit, "co": json.dumps(rec.column_overrides), "sheet": rec.sheet_filter,
             "err": rec.blocking_error_count, "warn": rec.warning_count, "dig": rec.result_digest, "vr": json.dumps(rec.validation_result)})
        self.c.execute(text("INSERT INTO batch_source_file (batch_id, content, size_bytes) VALUES (:id, :c, :n)"),
                       {"id": rec.batch_id, "c": content, "n": len(content)})

    def get_file(self, batch_id: uuid.UUID) -> bytes:
        row = self.c.execute(text("SELECT content FROM batch_source_file WHERE batch_id = :id"), {"id": batch_id}).first()
        if row is None:
            raise RepositoryError("source file missing")
        return bytes(row[0])

    def update_validation(self, batch_id: uuid.UUID, u: ValidationUpdate) -> BatchRecord:
        self.c.execute(text(
            "UPDATE import_batch SET status = :st, default_unit = :du, column_overrides = CAST(:co AS jsonb), sheet_filter = :sheet,"
            " blocking_error_count = :err, warning_count = :warn, result_digest = :dig, validation_result = CAST(:vr AS jsonb), version = version + 1"
            " WHERE batch_id = :id"),
            {"id": batch_id, "st": u.status, "du": u.default_unit, "co": json.dumps(u.column_overrides), "sheet": u.sheet_filter,
             "err": u.blocking_error_count, "warn": u.warning_count, "dig": u.result_digest, "vr": json.dumps(u.validation_result)})
        return self.get_batch(batch_id)

    def mark_approved(self, batch_id: uuid.UUID, approved_by: str, record_count: int) -> BatchRecord:
        # approved_at 은 DB 트리거가 now() 로 채운다. 상태 전이(READY_FOR_APPROVAL → APPROVED)도 트리거가 검사한다.
        self.c.execute(text("UPDATE import_batch SET status = 'APPROVED', approved_by = :by, approved_record_count = :n, version = version + 1"
                            " WHERE batch_id = :id"), {"id": batch_id, "by": approved_by, "n": record_count})
        return self.get_batch(batch_id)

    def mark_rejected(self, batch_id: uuid.UUID, rejected_by: str, reason: str) -> BatchRecord:
        self.c.execute(text("UPDATE import_batch SET status = 'REJECTED', rejected_by = :by, rejected_at = now(), reject_reason = :r,"
                            " version = version + 1 WHERE batch_id = :id"), {"id": batch_id, "by": rejected_by, "r": reason})
        return self.get_batch(batch_id)

    def list_known_products(self) -> list[KnownProduct]:
        rows = self.c.execute(text("SELECT product_id, product_name, base_unit, product_type, safety_stock_qty FROM product ORDER BY product_id")).all()
        return [KnownProduct(*r) for r in rows]

    def list_known_materials(self) -> list[KnownMaterial]:
        rows = self.c.execute(text("SELECT material_id, material_name, base_unit, safety_stock_qty FROM material ORDER BY material_id")).all()
        return [KnownMaterial(*r) for r in rows]

    def list_known_boms(self) -> list[KnownBom]:
        rows = self.c.execute(text(
            "SELECT h.bom_id, h.product_id, h.bom_version, h.is_active, h.valid_from, h.valid_to,"
            " COALESCE(array_agg(l.component_product_id ORDER BY l.line_no) FILTER (WHERE l.component_product_id IS NOT NULL), '{}')"
            " FROM bom_header h LEFT JOIN bom_line l ON l.bom_id = h.bom_id GROUP BY h.bom_id ORDER BY h.bom_id")).all()
        return [KnownBom(*r[:6], tuple(r[6])) for r in rows]

    def insert_materials(self, batch_id: uuid.UUID, rows: list[MaterialInsert]) -> int:
        self.c.execute(text("INSERT INTO material (material_id, material_name, base_unit, safety_stock_qty, batch_id) VALUES (:m, :n, :u, :s, :b)"),
                       [{"m": r.material_id, "n": r.material_name, "u": r.base_unit, "s": r.safety_stock_qty, "b": batch_id} for r in rows])
        return len(rows)

    def insert_bom(self, batch_id: uuid.UUID, headers: list[BomHeaderInsert], lines: list[BomLineInsert]) -> int:
        self.c.execute(text("INSERT INTO bom_header (bom_id, product_id, bom_version, status, is_active, valid_from, valid_to, base_qty, batch_id)"
                            " VALUES (:i, :p, :v, :s, :a, :f, :t, :q, :b)"),
                       [{"i": h.bom_id, "p": h.product_id, "v": h.bom_version, "s": h.status, "a": h.is_active, "f": h.valid_from, "t": h.valid_to,
                         "q": h.base_qty, "b": batch_id} for h in headers])
        self.c.execute(text("INSERT INTO bom_line (bom_id, line_no, component_type, component_material_id, component_product_id, qty_per, unit, scrap_rate, batch_id)"
                            " VALUES (:i, :n, :t, :m, :p, :q, :u, :s, :b)"),
                       [{"i": x.bom_id, "n": x.line_no, "t": x.component_type,
                         "m": x.component_id if x.component_type == "MATERIAL" else None, "p": x.component_id if x.component_type == "PRODUCT" else None,
                         "q": x.qty_per, "u": x.unit, "s": x.scrap_rate, "b": batch_id} for x in lines])
        return len(lines)

    def existing_inventory_keys(self, keys):
        rows = self.c.execute(text(
            "SELECT i.item_type, i.item_id, i.as_of_date FROM inventory i"
            " JOIN unnest(CAST(:ts AS text[]), CAST(:ids AS text[]), CAST(:ds AS date[])) AS k(t, id, d)"
            " ON i.item_type = k.t AND i.item_id = k.id AND i.as_of_date = k.d ORDER BY 1, 2, 3"),
            {"ts": [k[0] for k in keys], "ids": [k[1] for k in keys], "ds": [k[2] for k in keys]}).all()
        return [(r[0], r[1], r[2]) for r in rows]

    def insert_inventory(self, batch_id: uuid.UUID, rows: list[InventoryInsert]) -> int:
        self.c.execute(text("INSERT INTO inventory (item_type, item_id, as_of_date, on_hand_qty, allocated_qty, unit, batch_id) VALUES (:t, :i, :d, :o, :a, :u, :b)"),
                       [{"t": r.item_type, "i": r.item_id, "d": r.as_of_date, "o": r.on_hand_qty, "a": r.allocated_qty, "u": r.unit, "b": batch_id} for r in rows])
        return len(rows)

    def list_materials(self, material_id: str | None, q: str | None, limit: int, offset: int) -> tuple[list[ApprovedMaterial], int]:
        conds, p = ["b.status = 'APPROVED'"], {"l": limit, "o": offset}
        if material_id:
            conds.append("t.material_id = :mid")
            p["mid"] = material_id
        if q:
            conds.append("position(lower(:q) in lower(t.material_name)) > 0")
            p["q"] = q
        base = f"FROM material t JOIN import_batch b ON b.batch_id = t.batch_id WHERE {' AND '.join(conds)}"
        total = self.c.execute(text(f"SELECT count(*) {base}"), p).scalar_one()
        rows = self.c.execute(text("SELECT t.material_id, t.material_name, t.base_unit, t.safety_stock_qty, b.source_type, t.batch_id "
                                   f"{base} ORDER BY t.material_id LIMIT :l OFFSET :o"), p).all()
        return [ApprovedMaterial(*r) for r in rows], total

    def list_boms(self, product_id: str | None, bom_id: str | None, active_only: bool, limit: int, offset: int) -> tuple[list[ApprovedBom], int]:
        conds, p = ["b.status = 'APPROVED'"], {"l": limit, "o": offset}
        if product_id:
            conds.append("h.product_id = :pid")
            p["pid"] = product_id
        if bom_id:
            conds.append("h.bom_id = :bid")
            p["bid"] = bom_id
        if active_only:
            conds.append("h.is_active")
        base = f"FROM bom_header h JOIN import_batch b ON b.batch_id = h.batch_id WHERE {' AND '.join(conds)}"
        total = self.c.execute(text(f"SELECT count(*) {base}"), p).scalar_one()
        rows = self.c.execute(text("SELECT h.bom_id, h.product_id, h.bom_version, h.status, h.is_active, h.valid_from, h.valid_to, h.base_qty, b.source_type, h.batch_id "
                                   f"{base} ORDER BY h.product_id, h.bom_version LIMIT :l OFFSET :o"), p).all()
        ids = [r[0] for r in rows]
        lines: dict[str, list[BomLineInsert]] = {i: [] for i in ids}
        if ids:
            for r in self.c.execute(text("SELECT bom_id, line_no, component_type, COALESCE(component_material_id, component_product_id), qty_per, unit, scrap_rate"
                                         " FROM bom_line WHERE bom_id = ANY(CAST(:ids AS text[])) ORDER BY bom_id, line_no"), {"ids": ids}):
                lines[r[0]].append(BomLineInsert(*r))
        return [ApprovedBom(BomHeaderInsert(*r[:8]), lines[r[0]], r[8], r[9]) for r in rows], total

    def list_inventory(self, item_type, item_id, as_of_date, limit, offset) -> tuple[list[ApprovedInventory], int]:
        conds, p = ["b.status = 'APPROVED'"], {"l": limit, "o": offset}
        for sql, key, val in (("t.item_type = :it", "it", item_type), ("t.item_id = :id", "id", item_id), ("t.as_of_date = :d", "d", as_of_date)):
            if val:
                conds.append(sql)
                p[key] = val
        base = f"FROM inventory t JOIN import_batch b ON b.batch_id = t.batch_id WHERE {' AND '.join(conds)}"
        total = self.c.execute(text(f"SELECT count(*) {base}"), p).scalar_one()
        rows = self.c.execute(text("SELECT t.item_type, t.item_id, t.as_of_date, t.on_hand_qty, t.allocated_qty, t.unit, b.source_type, t.batch_id "
                                   f"{base} ORDER BY t.item_type, t.item_id, t.as_of_date LIMIT :l OFFSET :o"), p).all()
        return [ApprovedInventory(*r) for r in rows], total

    def insert_products(self, batch_id: uuid.UUID, rows: list[ProductInsert]) -> int:
        self.c.execute(text("INSERT INTO product (product_id, product_name, product_type, base_unit, safety_stock_qty, batch_id)"
                            " VALUES (:p, :n, :t, :u, :s, :b)"),
                       [{"p": r.product_id, "n": r.product_name, "t": r.product_type, "u": r.base_unit, "s": r.safety_stock_qty, "b": batch_id} for r in rows])
        return len(rows)      # 실제 행 수는 커밋 시점에 DB 트리거가 대조한다

    def list_products(self, product_id: str | None, q: str | None, limit: int, offset: int) -> tuple[list[ApprovedProduct], int]:
        conds, p = ["b.status = 'APPROVED'"], {"l": limit, "o": offset}
        if product_id:
            conds.append("t.product_id = :pid")
            p["pid"] = product_id
        if q:                                     # LIKE 가 아니라 position: 입력의 % _ 를 패턴으로 해석하지 않는다
            conds.append("position(lower(:q) in lower(t.product_name)) > 0")
            p["q"] = q
        base = f"FROM product t JOIN import_batch b ON b.batch_id = t.batch_id WHERE {' AND '.join(conds)}"
        total = self.c.execute(text(f"SELECT count(*) {base}"), p).scalar_one()
        rows = self.c.execute(text("SELECT t.product_id, t.product_name, t.base_unit, t.product_type, t.safety_stock_qty, b.source_type, t.batch_id "
                                   f"{base} ORDER BY t.product_id LIMIT :l OFFSET :o"), p).all()
        return [ApprovedProduct(*r) for r in rows], total

    def existing_demand_keys(self, keys: list[tuple[str, str]]) -> list[tuple[str, str]]:
        rows = self.c.execute(text(
            "SELECT d.product_id, d.period FROM monthly_demand d"
            " JOIN unnest(CAST(:pids AS text[]), CAST(:periods AS text[])) AS k(pid, per) ON d.product_id = k.pid AND d.period = k.per"
            " ORDER BY 1, 2"), {"pids": [k[0] for k in keys], "periods": [k[1] for k in keys]}).all()
        return [(r[0], r[1]) for r in rows]

    def insert_monthly_demand(self, batch_id: uuid.UUID, rows: list[DemandRow]) -> int:
        self.c.execute(text("INSERT INTO monthly_demand (product_id, period, demand_qty, unit, batch_id) VALUES (:p, :m, :q, :u, :b)"),
                       [{"p": r.product_id, "m": r.period, "q": r.demand_qty, "u": r.unit, "b": batch_id} for r in rows])
        return len(rows)      # 실제 행 수는 커밋 시점에 DB 트리거(verify_approved_record_count)가 approved_record_count 와 대조한다

    def list_approved_demand(self, product_id, period_from, period_to, limit, offset) -> tuple[list[ApprovedDemand], int]:
        conds, p = [], {"l": limit, "o": offset}
        for sql, key, val in (("product_id = :pid", "pid", product_id), ("period >= :pf", "pf", period_from), ("period <= :pt", "pt", period_to)):
            if val:
                conds.append(sql)
                p[key] = val
        where = ("WHERE " + " AND ".join(conds)) if conds else ""
        total = self.c.execute(text(f"SELECT count(*) FROM v_forecast_input_monthly_demand {where}"), p).scalar_one()
        rows = self.c.execute(text(f"SELECT product_id, period, demand_qty, unit, source_type, batch_id FROM v_forecast_input_monthly_demand {where}"
                                   " ORDER BY product_id, period LIMIT :l OFFSET :o"), p).all()
        return [ApprovedDemand(*r) for r in rows], total


class SqlBatchRepository:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    @contextmanager
    def transaction(self):
        try:
            with self.engine.begin() as conn:
                yield SqlBatchTx(conn)
        except IntegrityError as exc:
            if "uq_batch_live_entity_sha" in str(exc.orig):
                raise DuplicateBatchError() from exc
            raise RepositoryError(type(exc.orig).__name__) from exc
        except (OperationalError, InterfaceError) as exc:
            raise RepositoryUnavailable() from exc
        except DBAPIError as exc:
            raise RepositoryError(type(exc.orig).__name__) from exc
