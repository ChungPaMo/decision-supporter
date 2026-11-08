"""계약 예시 contracts/examples/engine/forecast_request.json · forecast_response.json 생성.

합성 데이터(SYNTHETIC)다. 공개 데이터(GPL-2.0) 가공본은 저장소에 넣지 않는다.
응답은 실제 엔진 출력이다(형식 설명용 숫자가 아님). 단 학습 행이 적어 SMALL_TRAINING_SET 경고가 붙는다.

    python -m engine.tools.make_forecast_examples
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from engine.forecast import run

OUT = Path(__file__).resolve().parents[2] / "contracts" / "examples" / "engine"


def _periods(start: tuple[int, int], n: int) -> list[str]:
    y, m = start
    out = []
    for _ in range(n):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def build_request() -> dict:
    rng = np.random.default_rng(7)
    rows = []

    def add(pid: str, start: tuple[int, int], values: list[int], skip: set[str] = frozenset()):
        for p, v in zip(_periods(start, len(values)), values):
            if p not in skip:
                rows.append({"product_id": pid, "period": p, "demand_qty": str(v), "unit": "EA",
                             "source_type": "SYNTHETIC", "batch_id": "B-SYN-DEMAND-001"})

    # 0001001: 계획 예시와 맞추기 위해 최근 12개월 평균(MA12)이 정확히 400 이 되도록 마지막 달을 맞춘다
    v = [int(x) for x in np.clip(rng.normal(400, 60, 36), 0, None).round()]
    v[-1] = 4800 - sum(v[-12:-1])
    add("0001001", (2014, 1), v, skip={"2015-02"})                 # 2015-02 행 없음 → ZERO_FILLED_MONTH
    seasonal = 1 + 0.3 * np.sin(np.arange(36) / 12 * 2 * np.pi)
    add("0001004", (2014, 1), [int(x) for x in np.clip(rng.normal(150, 40, 36) * seasonal, 0, None).round()])
    add("0001005", (2014, 1), [int(x) for x in rng.poisson(30, 36)])
    add("0001003", (2016, 5), [int(x) for x in rng.poisson(80, 8)])  # 8개월 → INSUFFICIENT_HISTORY
    return {
        "contract_version": "forecast-input-0.1-draft",
        "request_id": "FREQ-EXAMPLE-001",
        "trained_until": "2016-12-31",
        "horizon_months": 3,
        "p50_method": "MA12",
        "quantiles": ["P50", "P80", "P90"],
        "min_history_months": 12,
        "missing_month_policy": "ZERO_FILL",
        "random_seed": 42,
        "rows": rows,
    }


def main() -> int:
    req = build_request()
    res = run(req)
    for name, doc in (("forecast_request.json", req), ("forecast_response.json", res)):
        (OUT / name).write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"status={res['status']} forecasts={len(res['forecasts'])} -> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
