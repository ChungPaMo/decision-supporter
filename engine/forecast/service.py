"""예측 모듈 진입점: `run(request: dict) -> dict` (docs/engine_forecast_plan_contract.md §3, §6.1).

- P50: `MA12`(최근 12개월 평균, 모든 horizon 같은 값, 기본) 또는 `LGBM_L1`(LightGBM l1 점예측).
- P80·P90: LightGBM quantile (α=0.8, 0.9). 요청의 이력 전체로 매 실행마다 학습한다(100제품 기준 수 초).
- 분위수 교차 보정: p80 = max(p80, p50), p90 = max(p90, p80) 후 adjustments 에 기록.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal

import numpy as np
from pydantic import ValidationError

from engine import __version__
from engine.forecast import features, model
from engine.forecast.contract import REQUEST_VERSION, RESPONSE_VERSION, ForecastRequest

QUANT = Decimal("0.0001")
VALIDATED_MAX_HORIZON = 3          # 실험기록에서 검증한 horizon (h=2 주, h=1·3 보조)
SMALL_TRAINING_ROWS = 1000         # 기록의 학습 규모(100제품, 수천~1만여 행)보다 훨씬 작으면 경고
QUANTILE_MODEL_VERSION = f"lgbm-q/engine-{__version__}/{features.FEATURE_SET_VERSION}"


def run(request: dict) -> dict:
    try:
        req = ForecastRequest.model_validate(request)
    except ValidationError as e:
        return _invalid(request, [{"code": "SCHEMA_INVALID", "loc": ".".join(str(p) for p in err["loc"]),
                                   "detail": err["msg"]} for err in e.errors()])

    errors = _check(req)
    if errors:
        return _invalid(request, errors)

    T = _month_index(req.trained_until)
    horizons = list(range(1, req.horizon_months + 1))
    warnings: list[dict] = []
    skipped: list[dict] = []

    by_product: dict[str, list] = defaultdict(list)
    for r in req.rows:
        by_product[r.product_id].append(r)

    series: dict[str, tuple[int, list[Decimal]]] = {}
    for pid in sorted(by_product):
        rows = by_product[pid]
        units = sorted({r.unit for r in rows})
        if len(units) > 1:
            skipped.append({"product_id": pid, "reason_code": "UNIT_MIXED",
                            "detail": f"단위가 섞여 있음: {', '.join(units)}", "observed_months": None})
            continue
        qty = {_period_index(r.period): Decimal(r.demand_qty) for r in rows}
        start = min(qty)
        observed = T - start + 1
        if observed < req.min_history_months:
            skipped.append({"product_id": pid, "reason_code": "INSUFFICIENT_HISTORY",
                            "detail": f"관측 기간 {observed}개월 ({_period(start)}~{_period(T)}) < 최소 {req.min_history_months}개월",
                            "observed_months": observed})
            continue
        y = [qty.get(m, Decimal(0)) for m in range(start, T + 1)]
        if not any(y):
            skipped.append({"product_id": pid, "reason_code": "ALL_ZERO",
                            "detail": "관측 기간 수요가 모두 0", "observed_months": observed})
            continue
        filled = [_period(m) for m in range(start, T + 1) if m not in qty]
        if filled:
            warnings.append({"code": "ZERO_FILLED_MONTH", "product_id": pid, "periods": filled,
                             "detail": "승인 데이터에 행이 없는 달을 0으로 채움 (missing_month_policy=ZERO_FILL)"})
        if not any(y[-features.WINDOW:]):
            warnings.append({"code": "ZERO_RECENT_DEMAND", "product_id": pid,
                             "detail": "최근 12개월 수요가 모두 0이라 P50·P80·P90 을 0으로 냄"})
        series[pid] = (start, y)

    forecasts: list[dict] = []
    adjustments: list[dict] = []
    if series:
        preds, train_rows = _predict(series, horizons, req)
        if train_rows == 0:
            return _response(req, request, "DATA_INSUFFICIENT", [], skipped, [], warnings,
                             errors=[{"code": "NO_TRAINING_ROWS",
                                      "detail": "학습 행이 없음: 12개월 창 뒤에 실적 목표 월이 있는 제품이 필요"}])
        if train_rows < SMALL_TRAINING_ROWS:
            warnings.append({"code": "SMALL_TRAINING_SET", "training_rows": train_rows,
                             "detail": f"분위수 모델 학습 행 {train_rows}개 (기록 기준 1만여 행보다 작음). P80·P90 신뢰도 낮음"})
        if req.horizon_months > VALIDATED_MAX_HORIZON:
            warnings.append({"code": "HORIZON_BEYOND_VALIDATED",
                             "detail": f"h>{VALIDATED_MAX_HORIZON} 는 백테스트로 검증하지 않은 범위"})
        for pid, (start, y) in series.items():
            ma12 = (sum(y[-features.WINDOW:]) / features.WINDOW).quantize(QUANT, ROUND_HALF_UP)
            for h in horizons:
                q = preds.get((pid, h))
                p50 = ma12 if req.p50_method == "MA12" else _qty(q["l1"]) if q else Decimal(0)
                p80 = _qty(q["q80"]) if q else Decimal(0)
                p90 = _qty(q["q90"]) if q else Decimal(0)
                fixed80, fixed90 = max(p80, p50), max(p90, max(p80, p50))
                target = _period(T + h)
                if (fixed80, fixed90) != (p80, p90):
                    adjustments.append({"product_id": pid, "target_month": target, "code": "QUANTILE_CROSSING_FIXED",
                                        "before": {"p50": _fmt(p50), "p80": _fmt(p80), "p90": _fmt(p90)},
                                        "after": {"p50": _fmt(p50), "p80": _fmt(fixed80), "p90": _fmt(fixed90)}})
                forecasts.append({"product_id": pid, "target_month": target, "horizon": h,
                                  "p50": _fmt(p50), "p80": _fmt(fixed80), "p90": _fmt(fixed90),
                                  "p50_method": req.p50_method, "quantile_model_version": QUANTILE_MODEL_VERSION,
                                  "trained_until": req.trained_until.isoformat()})

    status = "DATA_INSUFFICIENT" if not forecasts else "PARTIAL" if skipped else "OK"
    return _response(req, request, status, forecasts, skipped, adjustments, warnings)


def _predict(series: dict, horizons: list[int], req: ForecastRequest):
    """전 제품 통합 모델을 학습하고 (제품, h) → {q80, q90[, l1]} 정규화 해제 예측을 돌려준다."""
    train_h = list(range(1, max(VALIDATED_MAX_HORIZON, max(horizons)) + 1))   # 기록과 같이 h=1~3 이상으로 학습
    Xs, ys, keys, Xp, sp = [], [], [], [], []
    for pid, (start, y) in series.items():
        arr = np.array([float(v) for v in y])
        X, t, _ = features.build(arr, start, train_h, for_training=True)
        Xs.append(X)
        ys.append(t)
        P, _, s = features.build(arr, start, horizons, for_training=False)
        for row, scale in zip(P, s):
            keys.append((pid, int(row[-1])))
            Xp.append(row)
            sp.append(scale)
    X, y = np.vstack(Xs), np.concatenate(ys)
    if len(y) == 0 or not Xp:
        return {}, len(y)
    Xp_arr, scale = np.vstack(Xp), np.array(sp)
    out = {k: {} for k in keys}
    jobs = [("q80", "quantile", 0.8), ("q90", "quantile", 0.9)]
    if req.p50_method == "LGBM_L1":
        jobs.append(("l1", "l1", None))
    for name, objective, alpha in jobs:
        pred = model.fit_predict(X, y, Xp_arr, objective=objective, alpha=alpha, seed=req.random_seed) * scale
        for k, v in zip(keys, pred):
            out[k][name] = v
    return out, len(y)


def _check(req: ForecastRequest) -> list[dict]:
    errors = []
    tu = req.trained_until
    if (tu + dt.timedelta(days=1)).day != 1:
        errors.append({"code": "TRAINED_UNTIL_NOT_MONTH_END", "detail": f"trained_until={tu} 은 월말이어야 함"})
    if req.quantiles != ["P50", "P80", "P90"]:
        errors.append({"code": "QUANTILES_UNSUPPORTED", "detail": 'quantiles 는 ["P50","P80","P90"] 고정'})
    T = _month_index(tu)
    seen = set()
    for i, r in enumerate(req.rows):
        key = (r.product_id, r.period)
        if key in seen:
            errors.append({"code": "DUPLICATE_PERIOD", "loc": f"rows.{i}", "detail": f"{r.product_id} {r.period} 중복"})
        seen.add(key)
        if _period_index(r.period) > T:
            errors.append({"code": "ROW_AFTER_TRAINED_UNTIL", "loc": f"rows.{i}",
                           "detail": f"{r.product_id} {r.period} 는 trained_until 이후 실적 (미래 누수)"})
    return errors


def _invalid(request: dict, errors: list[dict]) -> dict:
    rid = request.get("request_id") if isinstance(request, dict) else None
    return {"contract_version": RESPONSE_VERSION, "request_id": rid, "status": "INPUT_INVALID",
            "run": None, "forecasts": [], "skipped": [], "adjustments": [], "warnings": [], "errors": errors}


def _response(req, request, status, forecasts, skipped, adjustments, warnings, errors=None) -> dict:
    digest = _digest(request)
    sources = sorted({r.source_type for r in req.rows})
    return {
        "contract_version": RESPONSE_VERSION,
        "request_id": req.request_id,
        "status": status,
        "run": {
            "run_id": f"FRUN-{digest[:12]}",
            "engine": f"chungpamo-engine {__version__} (lightgbm {model.LIBRARY_VERSION})",
            "p50_method": req.p50_method,
            "quantile_model_version": QUANTILE_MODEL_VERSION,
            "trained_until": req.trained_until.isoformat(),
            "horizon_months": req.horizon_months,
            "random_seed": req.random_seed,
            "input_digest": f"sha256:{digest}",
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "data_classification": sources[0] if len(sources) == 1 else "MIXED" if sources else None,
        },
        "forecasts": forecasts,
        "skipped": skipped,
        "adjustments": adjustments,
        "warnings": warnings,
        "errors": errors or [],
    }


def _digest(request: dict) -> str:
    """request_id 를 뺀 요청 내용의 sha256. 같은 데이터·설정이면 같은 run_id."""
    body = {k: v for k, v in request.items() if k != "request_id"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                                     default=str).encode("utf-8")).hexdigest()


def _qty(x: float) -> Decimal:
    return Decimal(repr(float(x))).quantize(QUANT, ROUND_HALF_UP)


def _fmt(d: Decimal) -> str:
    s = format(d.quantize(QUANT, ROUND_HALF_UP), "f")
    return s.rstrip("0").rstrip(".") if "." in s else s


def _month_index(d: dt.date) -> int:
    return d.year * 12 + d.month - 1


def _period_index(p: str) -> int:
    return int(p[:4]) * 12 + int(p[5:7]) - 1


def _period(m: int) -> str:
    return f"{m // 12:04d}-{m % 12 + 1:02d}"
