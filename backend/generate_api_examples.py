"""화면 담당용 예시 생성: 합성 CSV 로 실제 API 코드를 호출해 contracts/examples/responses/*.json 과 contracts/openapi.json 을 만든다.

⚠ /batches 계열 응답은 테스트 전용 인메모리 저장소(tests/fakes.py)로 만든 것이다. 응답 형식의 예시이며 영구 저장이 동작한다는 뜻이 아니다.
사용: python generate_api_examples.py   (tests/test_frontend_fixtures.py 가 파일이 최신인지 검사)
"""
import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.api.batches import get_service
from app.batches.service import BatchService
from app.main import app
from tests.fakes import InMemoryBatchRepository

EXAMPLES = Path(__file__).resolve().parents[1] / "contracts" / "examples"
OUT_DIR = EXAMPLES / "responses"
OPENAPI_OUT = EXAMPLES.parent / "openapi.json"
APPROVER = "REVIEWER-01"
NOTE = "합성 CSV 로 생성한 응답 형식 예시. /batches 계열은 테스트 전용 인메모리 저장소 결과이며 영구 저장 동작을 증명하지 않는다."


def _sample(name: str) -> bytes:
    return (EXAMPLES / name).read_bytes()


class _Recorder:
    def __init__(self) -> None:
        self.files: dict[str, dict] = {}
        self.ids: dict[str, str] = {}

    def _stable(self, text: str) -> str:
        """무작위 batch_id·생성 시각을 고정해 다시 생성해도 같은 파일이 되게 한다."""
        data = json.loads(text)

        def walk(v):
            if isinstance(v, dict):
                return {k: ("2026-11-02T00:00:00Z" if k == "created_at" else self._id(x) if k == "batch_id" and isinstance(x, str) else walk(x)) for k, x in v.items()}
            return [walk(x) for x in v] if isinstance(v, list) else v

        return walk(data)

    def _id(self, value: str) -> str:
        return self.ids.setdefault(value, f"00000000-0000-0000-0000-{len(self.ids) + 1:012d}")

    def save(self, name: str, request: dict, response) -> dict:
        body = response.json()
        self.files[name] = {"_note": NOTE, "request": self._mask(request), "status": response.status_code, "response": self._stable(response.text)}
        return body

    def _mask(self, request: dict) -> dict:
        path = request.get("path", "")
        for real, fixed in self.ids.items():
            path = path.replace(real, fixed)
        return {**request, "path": path}


def build_examples() -> dict[str, str]:
    r = _Recorder()
    service = BatchService(InMemoryBatchRepository())
    app.dependency_overrides[get_service] = lambda: service
    c = TestClient(app)

    def upload(name, sample, entity):
        return r.save(name, {"method": "POST", "path": "/batches", "multipart": {"file": sample, "entity": entity}},
                      c.post("/batches", files={"file": (sample, _sample(sample))}, data={"entity": entity}))

    def approve(name, batch):
        path = f"/batches/{batch['batch_id']}/approve"
        body = {"approved_by": APPROVER, "expected_result_digest": batch["result_digest"]}
        return r.save(name, {"method": "POST", "path": path, "json": body}, c.post(path, json=body))

    def get(name, path):
        return r.save(name, {"method": "GET", "path": path}, c.get(path))

    try:
        # 순서: 참조되는 쪽 먼저 (제품·자재 → BOM·재고·월별 수요)
        approve("02_approve_product", upload("01_upload_product_ready", "sample_products.csv", "product"))
        approve("04_approve_material", upload("03_upload_material_ready", "sample_materials.csv", "material"))
        approve("06_approve_bom", upload("05_upload_bom_ready", "sample_bom.csv", "bom"))
        approve("08_approve_inventory", upload("07_upload_inventory_ready", "sample_inventory.csv", "inventory"))

        hold = upload("09_upload_demand_mapping_hold", "sample_demand_hold.csv", "monthly_demand")
        path = f"/batches/{hold['batch_id']}/revalidate"
        body = {"expected_version": hold["version"], "column_overrides": {"demand_qty": "주문수량"}}
        ready = r.save("10_revalidate_ready", {"method": "POST", "path": path, "json": body}, c.post(path, json=body))
        approve("11_approve_demand", ready)
        approve("12_error_already_approved", ready)

        bad = upload("13_upload_demand_has_errors", "sample_demand_errors.csv", "monthly_demand")
        approve("14_error_approval_blocked", bad)
        path = f"/batches/{bad['batch_id']}/reject"
        body = {"rejected_by": APPROVER, "reason": "기준월 오류, 원본 수정 후 재업로드"}
        r.save("15_reject", {"method": "POST", "path": path, "json": body}, c.post(path, json=body))
        upload("16_error_duplicate_upload", "sample_products.csv", "product")

        get("20_list_batches", "/batches?limit=2")
        get("21_list_products", "/products")
        get("22_list_materials", "/materials")
        get("23_list_boms", "/boms?active_only=true")
        get("24_list_inventory", "/inventory?as_of_date=2026-10-31")
        get("25_list_demand", "/demand/monthly")
        get("26_error_batch_not_found", "/batches/00000000-0000-0000-0000-00000000ffff")
        get("27_error_request_invalid", "/batches/not-a-uuid")
    finally:
        app.dependency_overrides.clear()

    get("28_error_persistence_disabled_default", "/batches")               # 기본 설정의 실제 응답
    r.save("29_preview_without_persistence", {"method": "POST", "path": "/ingest/preview", "multipart": {"file": "sample_products.csv", "entity": "product"}},
           c.post("/ingest/preview", files={"file": ("sample_products.csv", _sample("sample_products.csv"))}, data={"entity": "product"}))
    return {f"{k}.json": json.dumps(v, ensure_ascii=False, indent=2) + "\n" for k, v in r.files.items()}


def build_openapi() -> str:
    return json.dumps(app.openapi(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


if __name__ == "__main__":
    OUT_DIR.mkdir(exist_ok=True)
    for name, text in build_examples().items():
        (OUT_DIR / name).write_text(text, encoding="utf-8", newline="\n")
    OPENAPI_OUT.write_text(build_openapi(), encoding="utf-8", newline="\n")
    print("wrote", OUT_DIR, OPENAPI_OUT)
