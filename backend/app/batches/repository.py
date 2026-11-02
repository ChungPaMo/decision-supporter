"""배치 저장소 인터페이스. 서비스는 이 인터페이스만 안다.

- 구현: sql_repository.py (PostgreSQL, **Docker 개발 PC 에서 미검증**). 테스트용 인메모리 구현은 tests/fakes.py 에만 있다.
- 한 번의 transaction() 은 하나의 DB 트랜잭션이다. 블록이 예외로 끝나면 그 안의 모든 쓰기가 롤백되어야 한다.
"""

from __future__ import annotations

import datetime as dt
import uuid
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol

from app.ingest.validation import KnownProduct


class RepositoryUnavailable(Exception):
    """저장소에 연결할 수 없음 (→ 503 DB_UNAVAILABLE)."""


class RepositoryError(Exception):
    """저장 실패. 트랜잭션은 롤백되었다 (→ 500 PERSISTENCE_ERROR)."""


class DuplicateBatchError(Exception):
    """같은 SHA-256 의 살아있는 배치가 이미 있음 (동시 업로드 경합 포함)."""


@dataclass
class BatchRecord:
    batch_id: uuid.UUID
    entity: str
    status: str
    source_type: str
    source_name: str
    source_note: str | None
    source_sha256: str
    created_by: str | None
    created_at: dt.datetime
    default_unit: str | None
    column_overrides: dict
    sheet_filter: str | None
    blocking_error_count: int
    warning_count: int
    result_digest: str | None
    validation_result: dict | None
    version: int = 1
    approved_by: str | None = None
    approved_at: dt.datetime | None = None
    approved_record_count: int | None = None
    rejected_by: str | None = None
    rejected_at: dt.datetime | None = None
    reject_reason: str | None = None


@dataclass
class DemandRow:
    product_id: str
    period: str
    demand_qty: Decimal
    unit: str


@dataclass
class ProductInsert:
    product_id: str
    product_name: str
    base_unit: str
    product_type: str | None
    safety_stock_qty: Decimal


@dataclass
class ApprovedProduct:
    product_id: str
    product_name: str
    base_unit: str
    product_type: str | None
    safety_stock_qty: Decimal
    source_type: str
    batch_id: uuid.UUID


@dataclass
class ApprovedDemand:
    product_id: str
    period: str
    demand_qty: Decimal
    unit: str
    source_type: str
    batch_id: uuid.UUID


@dataclass
class ValidationUpdate:
    status: str
    default_unit: str | None
    column_overrides: dict
    sheet_filter: str | None
    blocking_error_count: int
    warning_count: int
    result_digest: str
    validation_result: dict = field(default_factory=dict)


class BatchTx(Protocol):
    def get_batch(self, batch_id: uuid.UUID, lock: bool = False) -> BatchRecord | None: ...
    def list_batches(self, status: str | None, limit: int, offset: int) -> tuple[list[BatchRecord], int]: ...
    def find_live_by_sha(self, sha256: str, entity: str) -> BatchRecord | None: ...
    def insert_batch(self, rec: BatchRecord, content: bytes) -> None: ...
    def get_file(self, batch_id: uuid.UUID) -> bytes: ...
    def update_validation(self, batch_id: uuid.UUID, upd: ValidationUpdate) -> BatchRecord: ...
    def mark_approved(self, batch_id: uuid.UUID, approved_by: str, record_count: int) -> BatchRecord: ...
    def mark_rejected(self, batch_id: uuid.UUID, rejected_by: str, reason: str) -> BatchRecord: ...
    def list_known_products(self) -> list[KnownProduct]: ...
    def insert_products(self, batch_id: uuid.UUID, rows: list[ProductInsert]) -> int: ...
    def list_products(self, product_id: str | None, q: str | None, limit: int, offset: int) -> tuple[list[ApprovedProduct], int]: ...
    def existing_demand_keys(self, keys: list[tuple[str, str]]) -> list[tuple[str, str]]: ...
    def insert_monthly_demand(self, batch_id: uuid.UUID, rows: list[DemandRow]) -> int: ...
    def list_approved_demand(self, product_id: str | None, period_from: str | None, period_to: str | None,
                             limit: int, offset: int) -> tuple[list[ApprovedDemand], int]: ...


class BatchRepository(Protocol):
    def transaction(self) -> AbstractContextManager[BatchTx]: ...
