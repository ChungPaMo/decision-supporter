"""화면 담당에게 주는 자료가 실제 코드와 어긋나지 않는지 검사한다 (DB 없음)."""
import json
from pathlib import Path

from generate_api_examples import EXAMPLES, NOTE, OPENAPI_OUT, OUT_DIR, build_examples, build_openapi

from app.errors import ErrorCode
from app.main import app

ROOT = Path(__file__).resolve().parents[2]
GUIDE = ROOT / "docs" / "frontend_guide.md"
HINT = "python generate_api_examples.py 를 실행해 갱신하세요"


def test_response_examples_are_generated_from_current_code():
    built = build_examples()
    assert sorted(p.name for p in OUT_DIR.glob("*.json")) == sorted(built), HINT
    for name, text in built.items():
        assert (OUT_DIR / name).read_text(encoding="utf-8") == text, f"{name}: {HINT}"


def test_openapi_snapshot_is_current():
    assert OPENAPI_OUT.read_text(encoding="utf-8") == build_openapi(), HINT


def test_every_batch_example_says_it_is_not_persistence_proof():
    for f in OUT_DIR.glob("*.json"):
        assert json.loads(f.read_text(encoding="utf-8"))["_note"] == NOTE


def test_examples_show_the_intended_outcomes():
    def load(name):
        return json.loads((OUT_DIR / f"{name}.json").read_text(encoding="utf-8"))

    assert load("02_approve_product")["response"]["status"] == "APPROVED"
    assert load("09_upload_demand_mapping_hold")["response"]["status"] == "MAPPING_HOLD"
    assert load("10_revalidate_ready")["response"]["status"] == "READY_FOR_APPROVAL"
    assert load("13_upload_demand_has_errors")["response"]["status"] == "HAS_ERRORS"
    assert load("15_reject")["response"]["status"] == "REJECTED"
    assert load("12_error_already_approved")["response"]["detail"]["code"] == "ALREADY_APPROVED"
    assert load("14_error_approval_blocked")["response"]["detail"]["code"] == "APPROVAL_BLOCKED_ERRORS"
    assert load("16_error_duplicate_upload")["response"]["detail"]["code"] == "DUPLICATE_UPLOAD"
    assert load("27_error_request_invalid")["response"]["detail"]["code"] == "REQUEST_INVALID"
    p = load("28_error_persistence_disabled_default")
    assert p["status"] == 501 and p["response"]["detail"]["code"] == "PERSISTENCE_DISABLED"
    assert load("24_list_inventory")["response"]["items"][1]["available_qty"] == "7"


def test_sample_files_are_synthetic_and_kept_as_lf():
    for f in EXAMPLES.glob("sample_*.csv"):
        data = f.read_bytes()
        assert b"\r" not in data, f"{f.name}: 줄바꿈이 바뀌면 sha256 과 예시가 달라진다 (.gitattributes -text)"
        assert "합성" in data.decode("utf-8") or f.name in ("sample_inventory.csv", "sample_bom.csv")


def test_guide_lists_every_api_path_and_common_error_code():
    guide = GUIDE.read_text(encoding="utf-8")
    for path in app.openapi()["paths"]:
        if path != "/health":
            assert f"`{path}" in guide or f" {path}" in guide, path
    assert "`/health`" in guide
    for code in ErrorCode:
        assert code.value in guide or code.value in (ROOT / "docs" / "api_contract_batches.md").read_text(encoding="utf-8"), code
