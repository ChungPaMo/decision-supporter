"""계획 결과 XLSX 출력 (DB·계획 엔진 없음). 입력은 합성 JSON fixture 이며 계산을 검증하지 않고 표시만 검증한다."""
import asyncio
import copy
import io
import json
import zipfile
from pathlib import Path

import openpyxl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import Settings
from app.exports import xlsx
from app.exports.contract import PlanExportRequest, PlanResult
from app.exports.limits import ExportBodyLimit, json_depth_exceeds
from app.exports.xlsx import BANNER, MISSING, MEDIA_TYPE, build_workbook, consistency_warnings, content_disposition, export_filename, safe_text
from app.main import app, configure_cors

SAMPLE = Path(__file__).resolve().parents[2] / "contracts" / "examples" / "plan_export_sample.json"
client = TestClient(app)


@pytest.fixture
def body():
    return json.loads(SAMPLE.read_text(encoding="utf-8"))


def wb_of(data: bytes):
    return openpyxl.load_workbook(io.BytesIO(data))


def build(body):
    return wb_of(build_workbook(PlanExportRequest(**body)))


def rows(ws):
    """데이터 행 (5행부터)."""
    return [list(r) for r in ws.iter_rows(min_row=5, values_only=True)]


def header(ws):
    return [c.value for c in ws[4] if c.value is not None]


def find(ws, *needles):
    return next(r for r in rows(ws) if all(n in r for n in needles))


# ---------------------------------------------------------------- 구성
def test_five_sheets_in_order_with_columns(body):
    wb = build(body)
    assert wb.sheetnames == ["계획안 비교", "제품별 생산계획", "자재 소요량 및 부족량", "적용 제약조건", "계산 근거·데이터 출처"]
    assert header(wb["계획안 비교"]) == ["항목", "단위", "기준형", "안정형", "비용절감형"]
    assert header(wb["제품별 생산계획"]) == list(xlsx.PRODUCTION_COLUMNS)
    assert header(wb["자재 소요량 및 부족량"]) == list(xlsx.MATERIAL_COLUMNS)
    assert header(wb["적용 제약조건"]) == list(xlsx.CONSTRAINT_COLUMNS)


def test_every_sheet_carries_demo_marking_that_cannot_be_turned_off(body):
    body["title"] = "공식 승인 계획"
    wb = build(body)
    for ws in wb.worksheets:
        assert ws["A1"].value == BANNER and "DEMO / UNVERIFIED" in ws.oddHeader.center.text and ws.freeze_panes == "A5"
    assert "DEMO" in wb.properties.creator
    assert "SYNTHETIC" in wb["계획안 비교"]["A2"].value


@pytest.mark.parametrize("extra", [{"approved": True}, {"status": "APPROVED"}, {"export_status": "OFFICIAL"}])
def test_request_cannot_claim_approval(body, extra):
    with pytest.raises(ValidationError):
        PlanExportRequest(**{**body, **extra})


def test_plans_ordered_regardless_of_input_and_fewer_than_three_allowed(body):
    body["plans"] = [body["plans"][2], body["plans"][0]]
    assert header(build(body)["계획안 비교"]) == ["항목", "단위", "기준형", "비용절감형"]


# ---------------------------------------------------------------- 값 정확성
def test_quantities_units_and_periods_are_shown_as_given(body):
    wb = build(body)
    row = find(wb["제품별 생산계획"], "0001001", "2026-12", "기준형")
    assert row[1:8] == ["0001001", "합성 제품 A", "2026-12", 400, "EA", 380.5, "P80"]            # 선행 0 보존
    row = find(wb["자재 소요량 및 부족량"], "M001")
    assert row[4:8] == [1000.25, 800, 200.25, "kg"]
    assert find(wb["계획안 비교"], "총 비용")[1:] == ["KRW", 45000000.5, 51000000, MISSING]


def test_decimal_text_is_exact_in_the_file_and_format_keeps_places(body):
    body["plans"][0]["kpis"][1]["value"] = "12345678901.5678"
    data = build_workbook(PlanExportRequest(**body))
    sheet = zipfile.ZipFile(io.BytesIO(data)).read("xl/worksheets/sheet1.xml").decode()
    assert "12345678901.5678" in sheet
    assert wb_of(data)["계획안 비교"]["C8"].number_format == "#,##0.0000"


def test_more_than_15_digits_stay_text_not_rounded(body):
    body["plans"][0]["kpis"][0]["value"] = "1234567890123456.78"
    assert find(build(body)["계획안 비교"], "총 생산량")[2] == "1234567890123456.78"


def test_no_recalculation_even_if_numbers_disagree(body):
    m = body["plans"][0]["materials"][0]
    m["required_qty"], m["available_qty"], m["shortage_qty"] = "10", "10", "999"          # 일부러 맞지 않는 값
    assert find(build(body)["자재 소요량 및 부족량"], "M001")[6] == 999


def test_kpi_with_different_units_is_not_merged(body):
    body["plans"][1]["kpis"][0]["unit"] = "BOX"
    ws = build(body)["계획안 비교"]
    assert find(ws, "총 생산량", "EA")[2:] == [1200, MISSING, 1100] and find(ws, "총 생산량", "BOX")[2:] == [MISSING, 1350, MISSING]


# ---------------------------------------------------------------- 누락
def test_missing_values_are_never_zero(body):
    wb = build(body)
    m = find(wb["자재 소요량 및 부족량"], "M002")
    assert m[5:7] == [MISSING, MISSING]
    p = find(wb["제품별 생산계획"], "2027-01")
    assert p[6] == MISSING and p[9] == MISSING
    assert find(wb["계획안 비교"], "납기 준수율")[2:] == [MISSING, MISSING, MISSING]


def test_explicit_zero_stays_zero(body):
    body["plans"][0]["materials"][0]["shortage_qty"] = "0"
    assert find(build(body)["자재 소요량 및 부족량"], "M001")[6] == 0


def test_omitted_optional_fields_show_missing(body):
    body["plans"][0]["production"] = [{"product_id": "P1", "period": "2026-12"}]
    r = find(build(body)["제품별 생산계획"], "P1")
    assert r[2] == MISSING and r[4] == MISSING and r[5] == MISSING


def test_no_forecast_basis_and_engine(body):
    del body["forecast_basis"], body["engine"], body["generated_at"]
    wb = build(body)
    assert find(wb["계산 근거·데이터 출처"], "생성 엔진")[1] == MISSING and find(wb["계산 근거·데이터 출처"], "예측 분위수")[1] == MISSING
    assert find(wb["제품별 생산계획"], "2026-12")[7] == MISSING


@pytest.mark.parametrize("value", [1200, 1200.5, "1e3", "NaN", "1,200", "１２"])
def test_numbers_must_be_plain_decimal_strings(body, value):
    body["plans"][0]["production"][0]["planned_qty"] = value
    with pytest.raises(ValidationError):
        PlanExportRequest(**body)


# ---------------------------------------------------------------- 다중 CAPA
def test_multiple_capacity_dimensions_are_listed_side_by_side(body):
    ws = build(body)["적용 제약조건"]
    base = [r for r in rows(ws) if r[0] == "기준형"]
    assert [r[3] for r in base] == ["QUANTITY", "WORK_HOURS", "EQUIPMENT_HOURS", "MATERIAL"]
    hours = find(ws, "CAP-HRS-L1")
    assert hours[3:] == ["WORK_HOURS", "HARD", "SATISFIED", "L1", "2026-12", 176, 150.5, "h", "합성 생산능력", MISSING]
    eqp = find(ws, "CAP-EQP-PRESS")
    assert eqp[5] == "UNVERIFIED" and eqp[8:10] == [MISSING, MISSING]


def test_unknown_dimension_is_shown_not_rejected(body):
    body["plans"][1]["constraints"][0]["dimension"] = "NEW_CAPA_KIND"
    assert find(build(body)["적용 제약조건"], "NEW_CAPA_KIND")


def test_feasible_with_unmet_hard_constraint_is_flagged_but_not_changed(body):
    body["plans"][0]["feasibility"] = "FEASIBLE"
    req = PlanExportRequest(**body)
    assert len(consistency_warnings(req)) == 1
    wb = wb_of(build_workbook(req))
    assert find(wb["계획안 비교"], "실행 가능성")[2] == "FEASIBLE"
    assert any("입력 불일치 경고" in str(r[0]) for r in rows(wb["계획안 비교"]))


# ---------------------------------------------------------------- 출처
def test_sources_and_classification_shown(body):
    ws = build(body)["계산 근거·데이터 출처"]
    assert find(ws, "데이터 구분")[1] == "SYNTHETIC" and find(ws, "출력 등급")[1] == "DEMO_UNVERIFIED"
    assert find(ws, "입력 계약 버전")[1] == "plan-export-0.1-draft"
    assert find(ws, "SYNTHETIC", "fixture plan_export_sample.json")
    assert find(ws, "가정 (기준형)")[1].startswith("합성 수치")


def test_classification_is_required(body):
    del body["data_classification"]
    with pytest.raises(ValidationError):
        PlanExportRequest(**body)


# ---------------------------------------------------------------- 수식 주입·파일명
@pytest.mark.parametrize("evil", ['=HYPERLINK("http://x","y")', "=cmd|'/c calc'!A0", "+1+1", "-1+1", "@SUM(A1)", "\t=1+1", "＝1+1"])
def test_formula_injection_is_neutralized_everywhere(body, evil):
    body["title"] = evil
    body["plans"][0]["production"][0]["product_name"] = evil
    body["plans"][0]["production"][0]["note"] = evil
    body["plans"][0]["kpis"][0]["label"] = evil
    body["plans"][0]["constraints"][0]["label"] = evil
    body["plans"][0]["materials"][0]["unit"] = evil
    body["plans"][0]["assumptions"] = [evil]
    body["evidence"][0]["name"] = evil
    data = build_workbook(PlanExportRequest(**body))
    for ws in openpyxl.load_workbook(io.BytesIO(data)).worksheets:
        for row in ws.iter_rows():
            for c in row:
                assert c.data_type != "f", (ws.title, c.coordinate, c.value)
                if isinstance(c.value, str):
                    assert not c.value.startswith(("=", "+", "@", "＝")), (ws.title, c.coordinate)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        assert not any(b"<f>" in z.read(n) for n in z.namelist() if n.startswith("xl/worksheets/"))


def test_safe_text_keeps_normal_text_and_strips_control_chars():
    assert safe_text("히터 A-1") == "히터 A-1" and safe_text("a\x07b") == "ab" and safe_text("-5") == "'-5"
    assert safe_text("\x01 =1") == "' =1"


@pytest.mark.parametrize("name,expected", [
    (None, "DEMO_UNVERIFIED_plan_export.xlsx"),
    ("계획 비교", "DEMO_UNVERIFIED_계획_비교.xlsx"),
    ("../../etc/passwd", "DEMO_UNVERIFIED_etc_passwd.xlsx"),
    ("..\\..\\win.ini", "DEMO_UNVERIFIED_win_ini.xlsx"),
    ('a"; filename="evil.exe', "DEMO_UNVERIFIED_a_filename_evil_exe.xlsx"),
    ("a\r\nX-Evil: 1", "DEMO_UNVERIFIED_a_X-Evil_1.xlsx"),
    ("...", "DEMO_UNVERIFIED_plan_export.xlsx"),
])
def test_filenames_are_safe(name, expected):
    assert export_filename(name)[0] == expected


def test_content_disposition_has_ascii_fallback_and_no_injection():
    header_value = content_disposition('계획\r\nX-Evil: 1"')
    assert "\r" not in header_value and "\n" not in header_value
    assert header_value.startswith('attachment; filename="DEMO_UNVERIFIED_') and "filename*=UTF-8''DEMO_UNVERIFIED_" in header_value
    assert header_value.encode("ascii")


# ---------------------------------------------------------------- HTTP
def test_download_response(body):
    r = client.post("/exports/plans/xlsx", json=body)
    assert r.status_code == 200 and r.headers["content-type"] == MEDIA_TYPE
    assert r.headers["content-disposition"] == ('attachment; filename="DEMO_UNVERIFIED_plan_compare_sample.xlsx"; '
                                                "filename*=UTF-8''DEMO_UNVERIFIED_plan_compare_sample.xlsx")
    assert r.headers["x-export-status"] == "DEMO_UNVERIFIED" and r.headers["x-content-type-options"] == "nosniff"
    assert wb_of(r.content).sheetnames[0] == "계획안 비교"


def test_works_without_database_or_persistence(body, monkeypatch):
    monkeypatch.setattr("app.db.get_engine", lambda: (_ for _ in ()).throw(AssertionError("DB 접근 금지")))
    assert client.post("/exports/plans/xlsx", json=body).status_code == 200


def test_invalid_request_shape(body):
    body["plans"][0]["production"][0]["planned_qty"] = 100
    r = client.post("/exports/plans/xlsx", json=body)
    d = r.json()["detail"]
    assert r.status_code == 422 and d["code"] == "REQUEST_INVALID" and d["details"][0]["loc"][:3] == ["body", "plans", "0"]


def test_duplicate_plan_type_and_bad_contract_version(body):
    dup = copy.deepcopy(body)
    dup["plans"][1]["plan_type"] = "BASELINE"
    assert client.post("/exports/plans/xlsx", json=dup).status_code == 422
    body["contract_version"] = "9"
    assert client.post("/exports/plans/xlsx", json=body).status_code == 422


def test_too_many_rows_is_413(body, monkeypatch):
    monkeypatch.setattr(xlsx, "MAX_ROWS", 3)
    r = client.post("/exports/plans/xlsx", json=body)
    assert r.status_code == 413 and r.json()["detail"]["code"] == "EXPORT_TOO_LARGE" and r.json()["detail"]["details"]["max_rows"] == 3


def test_body_size_limit_is_413():
    r = client.post("/exports/plans/xlsx", content=b"x" * (5 * 1024 * 1024 + 1), headers={"content-type": "application/json"})
    assert r.status_code == 413 and r.json()["detail"]["code"] == "EXPORT_TOO_LARGE"


def test_sample_fixture_satisfies_contract():
    """샘플 fixture 는 계약을 만족하고 합성 데이터임을 밝힌다 (화면 담당이 그대로 쓴다)."""
    req = PlanExportRequest(**json.loads(SAMPLE.read_text(encoding="utf-8")))
    assert req.data_classification == "SYNTHETIC" and [p.plan_type for p in req.plans] == ["BASELINE", "STABLE", "COST_SAVING"]


def test_openapi_documents_download():
    spec = client.get("/openapi.json").json()
    op = spec["paths"]["/exports/plans/xlsx"]["post"]
    assert MEDIA_TYPE in op["responses"]["200"]["content"] and "Content-Disposition" in op["responses"]["200"]["headers"]
    assert "PlanExportRequest" in op["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    assert "EXPORT_TOO_LARGE" in spec["components"]["schemas"]["ErrorCode"]["enum"]


# ---------------------------------------------------------------- CORS (최소 허용)
def _cors_app(origins: str):
    a = FastAPI()
    a.get("/x")(lambda: {})
    configure_cors(a, Settings(cors_allow_origins=origins, _env_file=None))
    return TestClient(a)


def test_cors_is_off_by_default():
    assert Settings(_env_file=None).cors_origins() == []
    r = _cors_app("").get("/x", headers={"Origin": "http://localhost:5173"})
    assert "access-control-allow-origin" not in r.headers


def test_cors_allows_only_listed_origin_and_exposes_filename_header():
    c = _cors_app("http://localhost:5173")
    ok = c.get("/x", headers={"Origin": "http://localhost:5173"})
    assert ok.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert "content-disposition" in ok.headers["access-control-expose-headers"].lower()
    assert "access-control-allow-credentials" not in ok.headers
    assert "access-control-allow-origin" not in c.get("/x", headers={"Origin": "http://evil.example"}).headers


def test_cors_wildcard_is_refused():
    with pytest.raises(ValueError):
        Settings(cors_allow_origins="*", _env_file=None).cors_origins()


# ---------------------------------------------------------------- 악성 요청: 본문 크기·구조
JSON_HEADERS = {"content-type": "application/json"}
LIMIT = 5 * 1024 * 1024


def chunks(total: int, size: int = 1024 * 1024):
    while total > 0:
        yield b" " * min(size, total)
        total -= size


async def _drive(middleware, messages, headers=()):
    """ASGI 를 직접 호출해 클라이언트가 보낸 메시지와 응답을 그대로 재현한다."""
    sent, it = [], iter(messages)

    async def receive():
        return next(it)

    async def send(m):
        sent.append(m)

    await middleware({"type": "http", "path": "/exports/plans/xlsx", "headers": list(headers)}, receive, send)
    return sent


def _asgi(messages, headers=(), limit=10):
    called = []

    async def inner(scope, receive, send):
        called.append(await receive())
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    return asyncio.run(_drive(ExportBodyLimit(inner, limit), messages, headers)), called


def test_body_without_content_length_is_counted():
    r = client.post("/exports/plans/xlsx", content=chunks(LIMIT + 1), headers=JSON_HEADERS)
    assert "content-length" not in r.request.headers
    assert r.status_code == 413 and r.json()["detail"]["code"] == "EXPORT_TOO_LARGE"


def test_body_exactly_at_limit_reaches_validation():
    r = client.post("/exports/plans/xlsx", content=chunks(LIMIT), headers=JSON_HEADERS)
    assert r.status_code == 422 and r.json()["detail"]["code"] == "REQUEST_INVALID"


def test_false_small_content_length_does_not_bypass_the_limit():
    sent, called = _asgi([{"type": "http.request", "body": b"x" * 6, "more_body": True}, {"type": "http.request", "body": b"x" * 6, "more_body": False}],
                         headers=[(b"content-length", b"3")])
    assert sent[0]["status"] == 413 and called == []                 # 앱은 호출되지 않는다


def test_reading_stops_as_soon_as_the_limit_is_crossed():
    messages = [{"type": "http.request", "body": b"x" * 11, "more_body": True}]         # 다음 메시지를 요구하면 StopIteration 으로 실패
    assert _asgi(messages)[0][0]["status"] == 413


def test_declared_oversize_is_refused_without_reading():
    assert _asgi([], headers=[(b"content-length", b"999999")])[0][0]["status"] == 413


def test_chunks_are_reassembled_for_the_app():
    sent, called = _asgi([{"type": "http.request", "body": b"ab", "more_body": True}, {"type": "http.request", "body": b"cd", "more_body": False}])
    assert sent[0]["status"] == 200 and called[0]["body"] == b"abcd" and called[0]["more_body"] is False


def test_client_disconnect_does_not_call_the_app():
    sent, called = _asgi([{"type": "http.disconnect"}])
    assert sent == [] and called == []


def test_limit_does_not_apply_to_other_paths():
    called = []

    async def inner(scope, receive, send):
        called.append(scope["path"])

    asyncio.run(ExportBodyLimit(inner, 1)({"type": "http", "path": "/batches", "headers": [(b"content-length", b"999")]}, None, None))
    assert called == ["/batches"]


@pytest.mark.parametrize("payload", [b"[" * 200_000 + b"]" * 200_000, b'{"a":' * 100_000 + b"1" + b"}" * 100_000, b"[" * 17 + b"]" * 17])
def test_deep_nesting_gets_the_common_error_shape(payload):
    r = client.post("/exports/plans/xlsx", content=payload, headers=JSON_HEADERS)
    assert r.status_code == 422 and r.json()["detail"]["code"] == "REQUEST_INVALID" and "중첩" in r.json()["detail"]["message"]


def test_brackets_inside_strings_are_not_depth():
    assert not json_depth_exceeds(b'{"a": "' + b"[" * 1000 + b'"}')
    assert not json_depth_exceeds(json.dumps({"a": "\\", "b": "\"" + "[" * 100}).encode())       # 이스케이프된 따옴표·역슬래시
    assert json_depth_exceeds(b"[" * 17) and not json_depth_exceeds(b"[" * 16)


def test_sample_is_well_within_depth_limit(body):
    assert not json_depth_exceeds(json.dumps(body).encode())


@pytest.mark.parametrize("payload,headers", [
    (b"\xff\xfe{}", JSON_HEADERS), (b"{", JSON_HEADERS), (b"", JSON_HEADERS), (b"null", JSON_HEADERS), (b"[]", JSON_HEADERS), (b'"x"', JSON_HEADERS),
    (b"{}", {"content-type": "text/plain"}),
])
def test_malformed_bodies_use_the_common_error_shape(payload, headers):
    r = client.post("/exports/plans/xlsx", content=payload, headers=headers)
    assert r.status_code == 422 and r.json()["detail"]["code"] == "REQUEST_INVALID"


@pytest.mark.parametrize("path,value", [
    ("plans", []), ("plans", [{}] * 4), ("evidence", [{}] * 1001), ("title", "x" * 201), ("export_name", "x" * 201),
])
def test_oversized_structures_are_rejected(body, path, value):
    body[path] = value
    r = client.post("/exports/plans/xlsx", json=body)
    assert r.status_code == 422 and r.json()["detail"]["code"] == "REQUEST_INVALID"


def test_list_size_limits_exist_for_every_plan_list():
    fields = PlanResult.model_fields
    for name in ("kpis", "production", "materials", "constraints", "assumptions"):
        assert any(getattr(m, "max_length", None) for m in fields[name].metadata), name


def test_error_responses_do_not_echo_input(body):
    body["title"] = {"secret": "SYNTHETIC_MARKER_VALUE"}
    r = client.post("/exports/plans/xlsx", json=body)
    assert r.status_code == 422 and "SYNTHETIC_MARKER_VALUE" not in r.text


def test_413_carries_cors_headers_so_the_browser_can_read_it():
    a = FastAPI()
    a.add_middleware(ExportBodyLimit, limit=1)
    configure_cors(a, Settings(cors_allow_origins="http://localhost:5173", _env_file=None))
    r = TestClient(a).post("/exports/plans/xlsx", content=b"xx", headers={"Origin": "http://localhost:5173"})
    assert r.status_code == 413 and r.headers["access-control-allow-origin"] == "http://localhost:5173"


# ---------------------------------------------------------------- 악성 요청: 수식·표시 제거 시도
@pytest.mark.parametrize("evil", ["\x00=1+1", "\x01=1+1", "\u00a0=1+1", " =1+1", "\r\n=1+1", "\x07+cmd", '=IMPORTXML("http://x","//a")', "@A1", "-2+3+cmd|' /C calc'!A0", "=1;=2"])
def test_formula_variants_never_become_formulas_through_the_api(body, evil):
    body["title"] = evil
    body["plans"][0]["production"][0]["product_id"] = evil
    body["plans"][0]["kpis"][0]["unit"] = evil
    body["plans"][0]["constraints"][0]["dimension"] = evil
    body["evidence"][0]["name"] = evil
    r = client.post("/exports/plans/xlsx", json=body)
    assert r.status_code == 200
    for ws in wb_of(r.content).worksheets:
        for row in ws.iter_rows():
            for c in row:
                assert c.data_type != "f" and not (isinstance(c.value, str) and c.value.lstrip().startswith(("=", "+", "@", "-"))), (ws.title, c.coordinate, c.value)
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        assert not any(b"<f>" in z.read(n) for n in z.namelist() if n.startswith("xl/worksheets/"))
        assert "xl/externalLinks" not in " ".join(z.namelist())


@pytest.mark.parametrize("extra", [
    {"export_status": "OFFICIAL"}, {"demo": False}, {"banner": ""}, {"approved_by": "REVIEWER-01"}, {"unverified": False},
])
def test_demo_marking_cannot_be_removed_by_request(body, extra):
    assert client.post("/exports/plans/xlsx", json={**body, **extra}).status_code == 422


@pytest.mark.parametrize("name", ["OFFICIAL_APPROVED", "../DEMO", "x.xlsx", ".."])
def test_demo_prefix_survives_any_export_name(body, name):
    body["export_name"] = name
    r = client.post("/exports/plans/xlsx", json=body)
    assert r.status_code == 200 and r.headers["x-export-status"] == "DEMO_UNVERIFIED"
    assert 'filename="DEMO_UNVERIFIED_' in r.headers["content-disposition"]
    assert "/" not in r.headers["content-disposition"].split("filename=")[1].split(";")[0]
    assert r.headers["content-disposition"].count(".xlsx") == 2 and ".xlsx.xlsx" not in r.headers["content-disposition"]


def test_banner_stays_even_with_a_title_claiming_approval(body):
    body["title"] = "공식 승인본 APPROVED"
    wb = wb_of(client.post("/exports/plans/xlsx", json=body).content)
    assert all(ws["A1"].value == BANNER and "공식 승인본" not in ws["A1"].value for ws in wb.worksheets)
    assert all(ws.sheet_properties.tabColor.rgb.endswith("C00000") for ws in wb.worksheets)


# ---------------------------------------------------------------- 기존 계약 불변
def test_export_contract_is_separate_from_data_contract():
    from app import contracts
    assert contracts.CONTRACT_VERSION == "1.2"
