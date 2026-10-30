"""app/contracts.py 에서 ../contracts/data_contract_v1.schema.json 을 생성한다. 프론트엔드·예측 모듈 담당이 읽는 기계 판독용 계약.
사용: python export_contract_schema.py   (tests/test_contract_schema_sync.py 가 파일이 최신인지 검사)"""
import json
from pathlib import Path

from pydantic import TypeAdapter

from app import contracts as c

ROWS = ["UnitRow", "ProductRow", "MaterialRow", "MonthlyDemandRow", "SalesOrderRow", "BomHeaderRow", "BomLineRow",
        "InventoryRow", "CapacityRow", "ForecastRow"]
OUT = Path(__file__).resolve().parents[1] / "contracts" / "data_contract_v1.schema.json"


def build() -> str:
    schema = {"contract_version": c.CONTRACT_VERSION,
              "models": {n: TypeAdapter(getattr(c, n)).json_schema(mode="validation") for n in ROWS}}
    return json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


if __name__ == "__main__":
    OUT.write_text(build(), encoding="utf-8", newline="\n")
    print("wrote", OUT)
