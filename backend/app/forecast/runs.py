"""Approved demand -> immutable snapshot -> engine -> atomic execution/results.

Synchronous execution holds a transaction advisory lock only for the same dedup
key. A crash or save failure rolls back all writes; FAILED engine attempts remain
auditable and are eligible for retry. No source demand/approval is modified.
"""
import hashlib
from importlib import metadata
import json
from pathlib import Path
import uuid

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError

import engine
from engine.forecast.contract import ForecastRequest
from app.errors import ApiError, ErrorCode
from app.forecast import service as adapter
from app.forecast.runs_schemas import ForecastExecution, ForecastExecutionSummary, ForecastHistory, ForecastRunReply, ForecastRunRequest

SUMMARY_COLUMNS = "execution_id,status,input_digest,dedup_key,parameters,runtime_identity,quantile_model_version,input_row_count,forecast_count,created_at,finished_at"


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _hash(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def runtime_identity():
    packages = {}
    for name in ("lightgbm", "numpy", "pydantic"):
        try:
            packages[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            packages[name] = "unavailable"
    folder = Path(engine.__file__).parent / "forecast"
    code = {name: hashlib.sha256((folder / name).read_bytes()).hexdigest()
            for name in ("contract.py", "features.py", "model.py", "service.py")}
    return {"engine_version": engine.__version__, "dependencies": packages, "code_sha256": code}


def _db_error(exc):
    if isinstance(exc, (OperationalError, InterfaceError)):
        return ApiError(503, ErrorCode.DB_UNAVAILABLE, "예측 저장소에 연결할 수 없습니다.")
    return ApiError(500, ErrorCode.PERSISTENCE_ERROR, "예측 저장에 실패해 이번 실행의 DB 변경을 모두 취소했습니다.")


class ForecastRuns:
    def __init__(self, database):
        self.database = database

    @staticmethod
    def _detail(connection, execution_id):
        row = connection.execute(text(f"SELECT {SUMMARY_COLUMNS},result,failure FROM forecast_run WHERE execution_id=:id"), {"id": execution_id}).mappings().first()
        if row is None:
            raise ApiError(404, ErrorCode.FORECAST_NOT_FOUND, "예측 실행 이력을 찾을 수 없습니다.")
        batches = connection.execute(text("SELECT batch_id,batch_version,source_sha256,result_digest,approved_at "
                                           "FROM forecast_run_input WHERE execution_id=:id ORDER BY batch_id"), {"id": execution_id}).mappings().all()
        return ForecastExecution.model_validate({**row, "input_batches": [dict(b) for b in batches]})

    def get(self, execution_id):
        try:
            with self.database.connect() as c:
                return self._detail(c, execution_id)
        except DBAPIError as exc:
            raise _db_error(exc) from None

    def list(self, limit=20, offset=0):
        try:
            # A single read snapshot keeps total and page consistent.
            with self.database.connect().execution_options(isolation_level="REPEATABLE READ") as c:
                total = c.execute(text("SELECT count(*) FROM forecast_run")).scalar_one()
                rows = c.execute(text(f"SELECT {SUMMARY_COLUMNS} FROM forecast_run ORDER BY created_at DESC,execution_id DESC LIMIT :l OFFSET :o"),
                                 {"l": limit, "o": offset}).mappings().all()
                return ForecastHistory(items=[ForecastExecutionSummary.model_validate(r) for r in rows], total=total, limit=limit, offset=offset)
        except DBAPIError as exc:
            raise _db_error(exc) from None

    def create(self, parameters: ForecastRunRequest):
        try:
            with self.database.begin() as c:
                # This is the sole input source. A single statement snapshots rows and
                # provenance together. Approved rows/batches are append-only already.
                rows = c.execute(text("""
SELECT v.product_id,v.period,v.demand_qty,v.unit,v.source_type,v.batch_id,
       b.version,b.source_sha256,b.result_digest,b.approved_at
FROM v_forecast_input_monthly_demand v JOIN import_batch b ON b.batch_id=v.batch_id
WHERE v.period <= :period AND b.status='APPROVED' AND b.blocking_error_count=0 AND b.entity='monthly_demand'
ORDER BY v.product_id,v.period
"""), {"period": parameters.trained_until.isoformat()[:7]}).mappings().all()
                execution_id = uuid.uuid4()
                payload = {**parameters.model_dump(mode="json"), "contract_version": "forecast-input-0.1-draft",
                           "request_id": str(execution_id), "rows": [
                               {"product_id": r["product_id"], "period": r["period"], "demand_qty": format(r["demand_qty"], "f"),
                                "unit": r["unit"], "source_type": r["source_type"], "batch_id": str(r["batch_id"])} for r in rows]}
                request = ForecastRequest.model_validate(payload)
                snapshot = request.model_dump(mode="json")
                # Canonical snapshots include defaults; pass that exact JSON to
                # the adapter too, so the engine's digest equals the stored one.
                request = ForecastRequest.model_validate(snapshot)
                digest = "sha256:" + _hash({k: v for k, v in snapshot.items() if k != "request_id"})
                provenance = {str(r["batch_id"]): {"batch_id": r["batch_id"], "batch_version": r["version"],
                              "source_sha256": r["source_sha256"], "result_digest": r["result_digest"], "approved_at": r["approved_at"]} for r in rows}
                identity = runtime_identity()
                dedup_key = _hash({"input_digest": digest, "runtime": identity, "batches": sorted(provenance.values(), key=lambda b: str(b["batch_id"]))})
                key = int.from_bytes(bytes.fromhex(dedup_key[:16]), "big", signed=True)
                locked = c.execute(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": key}).scalar_one()
                if not locked:
                    raise ApiError(409, ErrorCode.FORECAST_IN_PROGRESS, "같은 입력의 예측이 실행 중입니다. 잠시 후 실행 이력을 조회하세요.")
                existing = c.execute(text("SELECT execution_id FROM forecast_run WHERE dedup_key=:key AND status <> 'FAILED'"), {"key": dedup_key}).scalar_one_or_none()
                if existing:
                    return ForecastRunReply(execution=self._detail(c, existing), reused=True)
                result, failure = None, None
                try:
                    result = adapter.execute(request).model_dump(mode="json", exclude_unset=True)
                    if result["run"] and result["run"]["input_digest"] != digest:
                        raise ApiError(500, ErrorCode.FORECAST_ENGINE_ERROR, "예측 엔진의 입력 지문이 일치하지 않습니다.")
                except ApiError as exc:
                    failure = {"code": exc.code.value, "message": exc.message}
                    result = None
                status = result["status"] if result else "FAILED"
                values = {"id": execution_id, "key": dedup_key, "digest": digest, "parameters": _json(parameters.model_dump(mode="json")),
                          "snapshot": _json(snapshot), "identity": _json(identity), "inputs": len(rows),
                          "count": len(result["forecasts"]) if result else 0, "status": status,
                          "model": result["run"]["quantile_model_version"] if result and result["run"] else None,
                          "result": _json(result) if result is not None else None, "failure": _json(failure) if failure else None}
                c.execute(text("""
INSERT INTO forecast_run(execution_id,dedup_key,input_digest,parameters,input_snapshot,runtime_identity,
 input_row_count,forecast_count,status,quantile_model_version,result,failure)
VALUES (:id,:key,:digest,CAST(:parameters AS jsonb),CAST(:snapshot AS jsonb),CAST(:identity AS jsonb),
 :inputs,:count,:status,:model,CAST(:result AS jsonb),CAST(:failure AS jsonb))
"""), values)
                for batch in provenance.values():
                    c.execute(text("""INSERT INTO forecast_run_input(execution_id,batch_id,batch_version,source_sha256,result_digest,approved_at)
VALUES (:id,:batch_id,:batch_version,:source_sha256,:result_digest,:approved_at)"""), {"id": execution_id, **batch})
                if result and result["forecasts"]:
                    self._save_rows(c, execution_id, result["forecasts"])
                return ForecastRunReply(execution=self._detail(c, execution_id), reused=False)
        except DBAPIError as exc:
            raise _db_error(exc) from None

    @staticmethod
    def _save_rows(c, execution_id, rows):
        c.execute(text("""
INSERT INTO demand_forecast(forecast_run_id,product_id,target_month,horizon,p50,p80,p90,p50_method,quantile_model_version,trained_until)
VALUES (:id,:product_id,:target_month,:horizon,:p50,:p80,:p90,:p50_method,:quantile_model_version,:trained_until)
"""), [{"id": execution_id, **r} for r in rows])
