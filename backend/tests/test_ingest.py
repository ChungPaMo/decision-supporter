"""파싱·매핑·검증 단위 테스트. DB·Docker 불필요 (모든 입력은 메모리에서 생성)."""
import hashlib
import io
import re
from pathlib import Path

import openpyxl
import pytest

from app.ingest import parsing
from app.ingest.parsing import FileError, parse_file
from app.ingest.pipeline import run_preview
from app.ingest.validation import UNITS

HEADER = "제품코드,제품명,기준월,수요량,단위\n"


def csv_bytes(body: str, header: str = HEADER, enc: str = "utf-8") -> bytes:
    return (header + body).encode(enc)


def xlsx_bytes(rows, merged=None, formats=None) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "수요"
    for r in rows:
        ws.append(r)
    for rng in merged or []:
        ws.merge_cells(rng)
    for ref, fmt in (formats or {}).items():
        ws[ref].number_format = fmt
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def codes(result, sheet=0):
    return [(i.code, i.location.cell_ref if i.location else None) for i in result.sheets[sheet].issues]


# ---------------------------------------------------------------- 정상 입력
def test_clean_csv_ready_and_codes_preserved():
    data = csv_bytes("0001001,가정용 히터 A,2026-01,\"1,200\",EA\n0001001,가정용 히터 A,2026-02,900.5,EA\n0001002,가정용 히터 B,202601,300,개\n")
    r = run_preview("demand.csv", data)
    assert r.status == "READY_FOR_APPROVAL" and r.blocking_error_count == 0
    s = r.sheets[0]
    assert [(x.product_id, x.period, x.demand_qty, x.unit) for x in s.records] == [
        ("0001001", "2026-01", "1200", "EA"), ("0001001", "2026-02", "900.5", "EA"), ("0001002", "2026-01", "300", "EA")]
    assert s.records[0].location.row == 2 and s.mapping[0].method == "ALIAS_STRONG"
    assert r.file.sha256 == hashlib.sha256(data).hexdigest() and r.file.meta["encoding"] == "utf-8"


def test_always_requires_approval_never_auto():
    r = run_preview("d.csv", csv_bytes("FG001,A,2026-01,1,EA\n"))
    assert r.status == "READY_FOR_APPROVAL" and r.approval_required is True and r.auto_approve is False


def test_clean_xlsx_with_merged_two_level_header():
    rows = [["제품", None, "수요", None], ["제품코드", "제품명", "기준월", "수요량"], ["0001001", "히터", "2026-01", 100], ["0001001", "히터", "2026-02", 120.5]]
    r = run_preview("d.xlsx", xlsx_bytes(rows, merged=["A1:B1", "C1:D1"]))
    s = r.sheets[0]
    assert s.header_rows == [1, 2]
    assert r.status == "HAS_ERRORS"                          # 단위 열이 없고 default_unit 도 없음
    assert [i.code for i in s.issues] == ["UNIT_COLUMN_MISSING"]
    r2 = run_preview("d.xlsx", xlsx_bytes(rows, merged=["A1:B1", "C1:D1"]), default_unit="개")
    assert r2.status == "READY_FOR_APPROVAL"
    assert [x.demand_qty for x in r2.sheets[0].records] == ["100", "120.5"]
    assert any(i.code == "UNIT_COLUMN_MISSING" and i.severity == "WARNING" for i in r2.sheets[0].issues)


def test_xlsx_dates_and_numbers():
    import datetime as dt
    rows = [["제품코드", "기준월", "수요량", "단위"], ["FG001", dt.datetime(2026, 3, 1), 1500, "EA"], ["FG001", dt.datetime(2026, 4, 15), 10, "EA"]]
    r = run_preview("d.xlsx", xlsx_bytes(rows))
    s = r.sheets[0]
    assert [x.period for x in s.records] == ["2026-03"]
    assert ("PERIOD_NOT_MONTH_START", "B3") in codes(r)


@pytest.mark.parametrize("enc,delim", [("cp949", ","), ("utf-8-sig", ","), ("utf-8", ";"), ("utf-8", "\t")])
def test_encodings_and_delimiters(enc, delim):
    text = (HEADER + "FG001,히터,2026-01,5,EA\n").replace(",", delim)
    r = run_preview("d.csv", text.encode(enc))
    assert r.status == "READY_FOR_APPROVAL" and r.sheets[0].records[0].product_id == "FG001"
    assert r.file.meta["delimiter"] == delim


def test_different_header_names_via_aliases():
    r = run_preview("d.csv", "품번,품명,년월,수량(EA),UOM\nA-1,볼트,2026/05,7,ea\n".encode())
    s = r.sheets[0]
    assert r.status == "READY_FOR_APPROVAL"
    assert {m.field: m.method for m in s.mapping}["demand_qty"] == "ALIAS_WEAK"      # '수량' 은 약한 별칭
    assert s.records[0].review_required is True                                       # 약한 매핑이면 검토 표시


# ---------------------------------------------------------------- 제품코드·품명 충돌
def test_code_name_conflict_reports_every_involved_row_and_does_not_pick():
    data = csv_bytes("0001001,히터 A,2026-01,10,EA\n0001001,히터 B,2026-02,10,EA\n0001002,히터 A,2026-01,10,EA\n")
    r = run_preview("d.csv", data)
    assert r.status == "HAS_ERRORS"
    c = codes(r)
    assert {x for x in c if x[0] == "CODE_NAME_CONFLICT"} == {("CODE_NAME_CONFLICT", "B2"), ("CODE_NAME_CONFLICT", "B3")}
    assert {x for x in c if x[0] == "NAME_CODE_CONFLICT"} == {("NAME_CODE_CONFLICT", "A2"), ("NAME_CODE_CONFLICT", "A4")}
    assert r.sheets[0].records == []        # 충돌 행은 후보로도 내보내지 않는다
    assert r.sheets[0].records == [] and r.sheets[0].valid_records == 0


def test_same_name_with_spacing_or_case_is_not_a_conflict():
    r = run_preview("d.csv", csv_bytes("FG001,Heater  A,2026-01,1,EA\nFG001,heater a,2026-02,1,EA\n"))
    assert r.status == "READY_FOR_APPROVAL"


# ---------------------------------------------------------------- 선행 0
def test_leading_zero_text_cell_preserved_in_xlsx():
    rows = [["제품코드", "기준월", "수요량", "단위"], ["0001001", "2026-01", 1, "EA"]]
    r = run_preview("d.xlsx", xlsx_bytes(rows))
    assert r.sheets[0].records[0].product_id == "0001001" and r.status == "READY_FOR_APPROVAL"


def test_numeric_code_cell_with_zero_format_is_restored_with_warning():
    rows = [["제품코드", "기준월", "수요량", "단위"], [1001, "2026-01", 1, "EA"]]
    r = run_preview("d.xlsx", xlsx_bytes(rows, formats={"A2": "0000000"}))
    s = r.sheets[0]
    assert s.records[0].product_id == "0001001" and s.records[0].review_required is True
    assert [i.code for i in s.issues] == ["CODE_RESTORED_FROM_FORMAT"]


def test_numeric_code_cell_without_format_warns_lost_zeros():
    rows = [["제품코드", "기준월", "수요량", "단위"], [1001, "2026-01", 1, "EA"]]
    r = run_preview("d.xlsx", xlsx_bytes(rows))
    assert r.sheets[0].records[0].product_id == "1001"
    assert [i.code for i in r.sheets[0].issues] == ["CODE_NUMERIC_CELL"]


def test_leading_zero_variants_warn_and_are_not_merged():
    r = run_preview("d.csv", csv_bytes("0001001,히터 A,2026-01,1,EA\n1001,히터 B,2026-02,1,EA\n"))
    assert {i.code for i in r.sheets[0].issues} == {"CODE_VARIANT"}
    assert [x.product_id for x in r.sheets[0].records] == ["0001001", "1001"]


# ---------------------------------------------------------------- 비정상 값
@pytest.mark.parametrize("row,code,ref", [
    ("FG001,A,2026-13,1,EA", "PERIOD_INVALID", "C2"),
    ("FG001,A,2026-02-30,1,EA", "PERIOD_INVALID", "C2"),
    ("FG001,A,내년,1,EA", "PERIOD_INVALID", "C2"),
    ("FG001,A,,1,EA", "PERIOD_MISSING", "C2"),
    ("FG001,A,2026-01,-5,EA", "QTY_NEGATIVE", "D2"),
    ("FG001,A,2026-01,abc,EA", "QTY_INVALID", "D2"),
    ("FG001,A,2026-01,1e3,EA", "QTY_INVALID", "D2"),
    ("FG001,A,2026-01,NaN,EA", "QTY_INVALID", "D2"),
    ("FG001,A,2026-01,１２３,EA", "QTY_INVALID", "D2"),          # 전각 숫자
    ("FG001,A,2026-01,10개,EA", "QTY_INVALID", "D2"),
    ("FG001,A,2026-01,1.23456,EA", "QTY_RANGE", "D2"),
    ("FG001,A,2026-01,,EA", "QTY_MISSING", "D2"),
    ("FG001,A,2026-01,1,상자", "UNIT_UNKNOWN", "E2"),
    ("FG001,A,2026-01,1,", "UNIT_MISSING", "E2"),
    (",A,2026-01,1,EA", "CODE_MISSING", "A2"),
    ("FG 001,A,2026-01,1,EA", "CODE_INVALID", "A2"),
    ("한글코드,A,2026-01,1,EA", "CODE_INVALID", "A2"),
])
def test_invalid_values_reported_with_cell_position(row, code, ref):
    r = run_preview("d.csv", csv_bytes(row + "\n"))
    assert r.status == "HAS_ERRORS"
    assert (code, ref) in codes(r)
    assert r.sheets[0].records == []


def test_period_forms_and_excel_float_period_rejected():
    r = run_preview("d.csv", csv_bytes("A,n1,2026.1,1,EA\nB,n2,2026년 3월,1,EA\nC,n3,2026-04-01,1,EA\n"))
    assert [x.period for x in r.sheets[0].records] == ["2026-01", "2026-03", "2026-04"]
    rows = [["제품코드", "기준월", "수요량", "단위"], ["A", 2026.1, 1, "EA"]]
    r2 = run_preview("d.xlsx", xlsx_bytes(rows))
    assert ("PERIOD_AMBIGUOUS", "B2") in codes(r2)


def test_unit_dimension_mismatch_is_error_and_same_dimension_is_warning():
    r = run_preview("d.csv", csv_bytes("FG001,A,2026-01,1,EA\nFG001,A,2026-02,1,kg\n"))
    assert {i.code for i in r.sheets[0].issues} == {"UNIT_DIMENSION_MISMATCH"}
    r2 = run_preview("d.csv", csv_bytes("M1,A,2026-01,1,kg\nM1,A,2026-02,1000,g\n"))
    assert r2.status == "READY_FOR_APPROVAL" and {i.code for i in r2.sheets[0].issues} == {"UNIT_MIXED"}


def test_ragged_row_reported_not_dropped_silently():
    r = run_preview("d.csv", csv_bytes("FG001,A,2026-01,1,EA\nFG002,B,2026-01,1\n"))
    assert ("ROW_COLUMN_COUNT", None) in codes(r) and r.status == "HAS_ERRORS"
    assert r.sheets[0].issues[0].location.row == 3


# ---------------------------------------------------------------- 중복
def test_identical_duplicate_warns_and_keeps_first():
    r = run_preview("d.csv", csv_bytes("FG001,A,2026-01,5,EA\nFG001,A,2026-01,5.0,EA\n"))
    assert r.status == "READY_FOR_APPROVAL" and len(r.sheets[0].records) == 1
    assert ("DUPLICATE_ROW_IDENTICAL", "A3") in codes(r)


def test_conflicting_duplicate_is_error_on_both_rows():
    r = run_preview("d.csv", csv_bytes("FG001,A,2026-01,5,EA\nFG001,A,2026-01,6,EA\n"))
    assert r.status == "HAS_ERRORS" and r.sheets[0].records == []
    assert {x[1] for x in codes(r) if x[0] == "DUPLICATE_KEY_CONFLICT"} == {"D2", "D3"}


def test_empty_lines_ignored_and_row_numbers_follow_the_file():
    r = run_preview("d.csv", ("\n" + HEADER + "FG001,A,2026-01,1,EA\n\n\nFG002,B,2026-01,x,EA\n").encode())
    assert r.sheets[0].header_rows == [2]
    assert r.sheets[0].issues[0].location.cell_ref == "D6"


# ---------------------------------------------------------------- 매핑 보류
def test_missing_required_column_holds_and_skips_validation():
    r = run_preview("d.csv", "제품코드,제품명,수요량,단위\nFG001,A,1,EA\n".encode())
    s = r.sheets[0]
    assert r.status == "MAPPING_HOLD" and s.status == "HOLD" and not s.validated
    assert [h.code for h in s.holds] == ["REQUIRED_FIELD_MISSING"] and s.holds[0].field == "period"
    assert s.records == [] and s.issues == []


def test_ambiguous_columns_hold_with_candidates_then_user_override_resolves():
    data = "제품코드,기준월,수요량,판매량,단위\nFG001,2026-01,1,2,EA\n".encode()      # 수요량(강)·판매량(약) → 강한 쪽 선택
    assert run_preview("d.csv", data).status == "READY_FOR_APPROVAL"
    data2 = "제품코드,기준월,수량,주문수량,단위\nFG001,2026-01,1,2,EA\n".encode()     # 약한 별칭 둘 → 보류
    r = run_preview("d.csv", data2)
    h = r.sheets[0].holds[0]
    assert r.status == "MAPPING_HOLD" and h.code == "AMBIGUOUS_COLUMNS" and h.candidates == ["수량", "주문수량"]
    r2 = run_preview("d.csv", data2, column_overrides={"demand_qty": "주문수량"})
    assert r2.status == "READY_FOR_APPROVAL" and r2.sheets[0].records[0].demand_qty == "2"
    assert {m.field: m.method for m in r2.sheets[0].mapping}["demand_qty"] == "USER_OVERRIDE"
    r3 = run_preview("d.csv", data2, column_overrides={"demand_qty": "D"})
    assert r3.sheets[0].records[0].demand_qty == "2"
    r4 = run_preview("d.csv", data2, column_overrides={"demand_qty": "없는열"})
    assert r4.status == "MAPPING_HOLD" and r4.sheets[0].holds[0].code == "OVERRIDE_NOT_FOUND"


def test_unrelated_sheet_is_ignored_but_wrong_file_is_hold():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "설명"
    ws.append(["이 파일은 수요 데이터입니다"])
    ws2 = wb.create_sheet("수요")
    for row in [["제품코드", "기준월", "수요량", "단위"], ["FG001", "2026-01", 1, "EA"]]:
        ws2.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    r = run_preview("d.xlsx", buf.getvalue())
    assert r.status == "READY_FOR_APPROVAL" and [s.status for s in r.sheets] == ["IGNORED", "MAPPED"]
    assert run_preview("x.csv", b"a,b\n1,2\n").status == "MAPPING_HOLD"


def test_hold_sheet_blocks_whole_file_even_if_other_sheet_is_fine():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "A"
    for row in [["제품코드", "기준월", "수요량", "단위"], ["FG001", "2026-01", 1, "EA"]]:
        ws.append(row)
    ws2 = wb.create_sheet("B")
    for row in [["제품코드", "수요량"], ["FG002", 1]]:
        ws2.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    assert run_preview("d.xlsx", buf.getvalue()).status == "MAPPING_HOLD"


# ---------------------------------------------------------------- 파일 오류
@pytest.mark.parametrize("name,data,code,status", [
    ("a.txt", b"x", "UNSUPPORTED_TYPE", 415),
    ("a.xls", b"x", "UNSUPPORTED_TYPE", 415),
    ("a.csv", b"", "FILE_EMPTY", 422),
    ("a.xlsx", b"not a zip", "XLSX_CORRUPT", 422),
    ("a.xlsx", b"PK\x03\x04garbage", "XLSX_CORRUPT", 422),
    ("a.csv", b"\xff\xfe\x00bad\x81", "ENCODING_UNKNOWN", 422),
    ("a.csv", b"onlyonecolumn\nvalue\n", "DELIMITER_UNKNOWN", 422),
])
def test_file_errors(name, data, code, status):
    with pytest.raises(FileError) as e:
        run_preview(name, data)
    assert (e.value.code, e.value.http_status) == (code, status)


def test_too_large_and_too_many_rows(monkeypatch):
    monkeypatch.setattr(parsing, "MAX_BYTES", 10)
    with pytest.raises(FileError) as e:
        parse_file("a.csv", b"x" * 11)
    assert e.value.code == "FILE_TOO_LARGE"
    monkeypatch.setattr(parsing, "MAX_BYTES", 10_000)
    monkeypatch.setattr(parsing, "MAX_ROWS", 2)
    with pytest.raises(FileError) as e:
        parse_file("a.csv", csv_bytes("A,n,2026-01,1,EA\nB,n,2026-01,1,EA\n"))
    assert e.value.code == "TOO_MANY_ROWS"


def test_filename_path_is_stripped():
    assert parse_file("C:\\Users\\someone\\secret\\d.csv", csv_bytes("A,n,2026-01,1,EA\n")).filename == "d.csv"


def test_source_file_on_disk_is_not_modified(tmp_path):
    p = tmp_path / "d.csv"
    p.write_bytes(csv_bytes("FG001,A,2026-01,1,EA\n"))
    before = (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
    run_preview(p.name, p.read_bytes())
    assert (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns) == before


def test_units_match_migration_seed():
    src = (Path(__file__).resolve().parents[1] / "migrations" / "versions" / "0001_initial_schema.py").read_text(encoding="utf-8")
    block = src.split('UNIT_SEED = """')[1].split('"""')[0]
    seeded = {m.group(1): m.group(2) for m in re.finditer(r"\('([A-Za-z]+)',\s*'([A-Z]+)',", block)}
    assert seeded == UNITS
