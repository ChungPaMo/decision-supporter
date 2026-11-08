"""Rolling-origin 백테스트: 예측 모듈(`engine.forecast.run`) 자체를 과거 기준 월마다 다시 돌려 평가한다.

기준 월 t 마다 t 까지의 행만 넣은 요청으로 run 을 호출하고(미래 누수 없음), 목표 월 실적과 비교한다.
지표 (26-10-26 실험기록 §7·§8): P50 WAPE·Bias%, P80·P90 pinball(실적 합으로 나눔)·커버리지.

    python -m engine.backtest --input <request.json> --valid-year 2015 --test-year 2016 --output report.json
"""

from __future__ import annotations

import argparse
import copy
import json
import time
from collections import defaultdict
from pathlib import Path

from engine.forecast import run


def _idx(period: str) -> int:
    return int(period[:4]) * 12 + int(period[5:7]) - 1


def _month_end(m: int) -> str:
    import calendar
    y, mo = m // 12, m % 12 + 1
    return f"{y:04d}-{mo:02d}-{calendar.monthrange(y, mo)[1]:02d}"


def backtest(request: dict, years: dict[str, int], methods=("MA12", "LGBM_L1")) -> dict:
    actual = {(r["product_id"], _idx(r["period"])): float(r["demand_qty"]) for r in request["rows"]}
    H = request.get("horizon_months", 3)
    target_months = {m for y in years.values() for m in range(y * 12, y * 12 + 12)}
    bases = sorted({t - h for t in target_months for h in range(1, H + 1)})

    # (method, split, h) → 누적값
    acc = defaultdict(lambda: defaultdict(float))
    adjust_count = defaultdict(int)
    t0 = time.time()
    for method in methods:
        for base in bases:
            req = copy.deepcopy(request)
            req["rows"] = [r for r in request["rows"] if _idx(r["period"]) <= base]
            req["trained_until"] = _month_end(base)
            req["p50_method"] = method
            req["request_id"] = f"BT-{method}-{base}"
            res = run(req)
            if res["status"] not in ("OK", "PARTIAL"):
                raise RuntimeError(f"base {base}: {res['status']} {res.get('errors')}")
            fixed = {(a["product_id"], a["target_month"]) for a in res["adjustments"]}
            for f in res["forecasts"]:
                tm = _idx(f["target_month"])
                split = next((s for s, y in years.items() if tm // 12 == y), None)
                if split is None or (f["product_id"], tm) not in actual:
                    continue
                y = actual[(f["product_id"], tm)]
                p50, p80, p90 = float(f["p50"]), float(f["p80"]), float(f["p90"])
                a = acc[(method, split, f["horizon"])]
                a["n"] += 1
                a["y"] += y
                a["abs_err"] += abs(y - p50)
                a["err"] += p50 - y
                a["pin80"] += 0.8 * (y - p80) if y >= p80 else 0.2 * (p80 - y)
                a["pin90"] += 0.9 * (y - p90) if y >= p90 else 0.1 * (p90 - y)
                a["cov80"] += y <= p80
                a["cov90"] += y <= p90
                if (f["product_id"], f["target_month"]) in fixed:
                    adjust_count[(method, split, f["horizon"])] += 1

    table = []
    for (method, split, h), a in sorted(acc.items()):
        table.append({"p50_method": method, "split": split, "horizon": h, "n": int(a["n"]),
                      "wape": round(a["abs_err"] / a["y"], 4), "bias_pct": round(a["err"] / a["y"] * 100, 2),
                      "pinball80": round(a["pin80"] / a["y"], 4), "pinball90": round(a["pin90"] / a["y"], 4),
                      "coverage80": round(a["cov80"] / a["n"], 4), "coverage90": round(a["cov90"] / a["n"], 4),
                      "crossing_fixed_rows": adjust_count[(method, split, h)]})
    return {"years": years, "base_months": len(bases), "products": len({p for p, _ in actual}),
            "elapsed_s": round(time.time() - t0, 1), "results": table}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="예측 모듈 rolling-origin 백테스트")
    ap.add_argument("--input", required=True, type=Path, help="전체 이력이 든 forecast-input JSON")
    ap.add_argument("--valid-year", type=int, default=2015)
    ap.add_argument("--test-year", type=int, default=2016)
    ap.add_argument("--output", type=Path)
    args = ap.parse_args(argv)
    request = json.loads(args.input.read_text(encoding="utf-8"))
    report = backtest(request, {"valid": args.valid_year, "test": args.test_year})
    for r in report["results"]:
        print(f"{r['p50_method']:8} {r['split']:5} h={r['horizon']} n={r['n']}  WAPE {r['wape']:.3f} ({r['bias_pct']:+.1f}%)  "
              f"pinball {r['pinball80']:.3f}/{r['pinball90']:.3f}  cov {r['coverage80']:.3f}/{r['coverage90']:.3f}  "
              f"crossing {r['crossing_fixed_rows']}")
    print(f"base months {report['base_months']}, products {report['products']}, {report['elapsed_s']}s")
    if args.output:
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
