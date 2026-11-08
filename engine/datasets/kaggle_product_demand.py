"""공개 데이터 Kaggle "Forecasts for Product Demand"(felixzhao, GPL-2.0) → 예측 요청 JSON.

전처리 규칙은 26-10-26 수요예측 실험기록 §5·§6 그대로다. 결과 파일은 GPL-2.0 데이터의 가공본이므로
**저장소 밖에 둔다** (팀 규칙: 원본·가공 데이터 미커밋).

    python -m engine.datasets.kaggle_product_demand --csv "<...>/Historical Product Demand.csv" --output <저장소 밖>/request.json

규칙
- 날짜 'NA' 행(11,239행, 전부 Whse_A) 제외
- 괄호 음수(반품 추정) 제외 — 타깃은 양수 주문 합
- 2011·2017년 제외 (불완전 연도), 기간 2012-01 ~ 2016-12
- Whse_S 전체 제외 (2016년분이 Excel 행 한계에서 잘림)
- 완전 중복 행 유지 (로트 단위 별개 주문 추정)
- 창고 합산 제품×월 집계, 빈 월은 0 (명시적인 0 행으로 낸다)
- 대상: 2012~2014 수요 상위 N제품 (기본 100, 고정 유니버스)
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

FIRST, LAST = (2012, 1), (2016, 12)
EXCLUDED_WAREHOUSE = "Whse_S"
SOURCE_NOTE = "Kaggle felixzhao 'Forecasts for Product Demand' (GPL-2.0)"


def load_panel(csv_path: Path) -> tuple[dict[str, dict[tuple[int, int], Decimal]], dict]:
    """제품 → {(연, 월): 양수 주문 합}. Whse_S·NA·반품·범위 밖 연도 제외. 점검 수치도 함께 돌려준다."""
    panel: dict[str, dict[tuple[int, int], Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    stats = {"rows": 0, "date_na": 0, "returns": 0, "out_of_range": 0, "whse_s": 0}
    with csv_path.open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            stats["rows"] += 1
            date = row["Date"].strip()
            if date == "NA":
                stats["date_na"] += 1
                continue
            qty = row["Order_Demand"].strip()
            if qty.startswith("("):
                stats["returns"] += 1
                continue
            y, m, _ = date.split("/")
            ym = (int(y), int(m))
            if not FIRST <= ym <= LAST:
                stats["out_of_range"] += 1
                continue
            if row["Warehouse"].strip() == EXCLUDED_WAREHOUSE:
                stats["whse_s"] += 1
                continue
            q = Decimal(qty)
            if q > 0:
                panel[row["Product_Code"].strip()][ym] += q
    return panel, stats


def months() -> list[tuple[int, int]]:
    out, (y, m) = [], FIRST
    while (y, m) <= LAST:
        out.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def build_request(csv_path: Path, top: int = 100, horizon: int = 3, seed: int = 42) -> tuple[dict, dict]:
    panel, stats = load_panel(csv_path)
    stats["panel_products"] = len(panel)
    universe_key = lambda p: sum(v for (y, _), v in panel[p].items() if y <= 2014)  # noqa: E731
    universe = sorted(panel, key=lambda p: (-universe_key(p), p))[:top]
    total = sum(universe_key(p) for p in panel)
    stats["universe_share_2012_2014"] = float(sum(universe_key(p) for p in universe) / total)

    digest = hashlib.sha256(csv_path.read_bytes()).hexdigest()
    batch_id = f"KAGGLE-PD-{digest[:12]}"
    rows = [{"product_id": p, "period": f"{y:04d}-{m:02d}", "demand_qty": format(panel[p].get((y, m), Decimal(0)), "f"),
             "unit": "EA", "source_type": "PUBLIC_DEMAND", "batch_id": batch_id}
            for p in sorted(universe) for (y, m) in months()]
    request = {
        "contract_version": "forecast-input-0.1-draft",
        "request_id": f"FREQ-KAGGLE-TOP{top}-2016-12",
        "trained_until": "2016-12-31",
        "horizon_months": horizon,
        "p50_method": "MA12",
        "quantiles": ["P50", "P80", "P90"],
        "min_history_months": 12,
        "missing_month_policy": "ZERO_FILL",
        "random_seed": seed,
        "rows": rows,
    }
    stats["source_sha256"] = digest
    stats["source_note"] = SOURCE_NOTE
    return request, stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--csv", required=True, type=Path)
    ap.add_argument("--output", required=True, type=Path)
    ap.add_argument("--top", type=int, default=100)
    args = ap.parse_args(argv)
    request, stats = build_request(args.csv, args.top)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(request, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
