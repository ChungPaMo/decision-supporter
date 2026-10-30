"""CSV/XLSX 파싱. 바이트를 받아 메모리에서만 처리한다 (원본 파일을 열어 쓰지 않으므로 수정할 수 없다).

PoC 재사용: src/parsers/tabular.py 의 인코딩 판별(BOM→UTF-8→CP949)과 XLSX 1행 병합 헤더 복원.
달라진 점: 구분자는 '헤더 줄의 열 수가 가장 많은 후보'로 고르고(행마다 열 수가 같아야 한다는 PoC 조건 제거),
열 수가 어긋난 행은 파서가 버리지 않고 검증 단계가 행 위치와 함께 보고한다. 값은 가공하지 않고 문자열로 보존한다.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import os
import re
import zipfile
from dataclasses import dataclass, field
from decimal import Decimal

import openpyxl
from openpyxl.utils import get_column_letter

MAX_BYTES = 10 * 1024 * 1024
MAX_ROWS = 100_000
ENCODINGS = ("utf-8", "cp949")
DELIMITERS = (",", ";", "\t", "|")


class FileError(Exception):
    """파일 전체를 처리할 수 없는 오류. API 가 {code, message} 로 돌려준다."""

    def __init__(self, code: str, message: str, http_status: int = 422) -> None:
        super().__init__(message)
        self.code, self.message, self.http_status = code, message, http_status


@dataclass(slots=True)
class Cell:
    value: str | None          # 표시 문자열(공백 보존). 빈 셀은 None
    kind: str                  # empty | text | number | date | bool
    number_format: str | None = None


@dataclass(slots=True)
class RawRow:
    row: int                   # 원본 시트의 행 번호(1부터, 헤더 포함)
    cells: list[Cell]


@dataclass
class HeaderCol:
    col: int                   # 1부터
    name: str | None           # 열 이름(2단 헤더면 아래 행)
    group: str | None = None   # 2단 헤더의 위 행(병합 그룹)

    @property
    def labels(self) -> list[str]:
        out = [s for s in (self.name, f"{self.group} {self.name}" if self.group and self.name else None, self.group) if s]
        return list(dict.fromkeys(out))

    @property
    def display(self) -> str:
        return " / ".join(s for s in (self.group, self.name) if s) or f"(열 {get_column_letter(self.col)})"


@dataclass
class ParsedSheet:
    name: str
    header_rows: list[int]
    header: list[HeaderCol]
    rows: list[RawRow]
    width: int


@dataclass
class ParsedFile:
    filename: str
    format: str                # csv | xlsx
    size_bytes: int
    sha256: str
    sheets: list[ParsedSheet]
    meta: dict = field(default_factory=dict)


def col_letter(col: int) -> str:
    return get_column_letter(col)


def _empty(v) -> bool:
    return v is None or (isinstance(v, str) and v.strip() == "")


def parse_file(filename: str, data: bytes) -> ParsedFile:
    name = os.path.basename((filename or "").replace("\\", "/"))      # 경로·사용자명 제거
    ext = os.path.splitext(name)[1].lower()
    if ext not in (".csv", ".xlsx"):
        raise FileError("UNSUPPORTED_TYPE", f"지원하지 않는 파일 형식입니다 ({ext or '확장자 없음'}). CSV, XLSX 만 가능합니다.", 415)
    if not data:
        raise FileError("FILE_EMPTY", "빈 파일입니다.")
    if len(data) > MAX_BYTES:
        raise FileError("FILE_TOO_LARGE", f"파일이 너무 큽니다 (최대 {MAX_BYTES // 1048576}MB).", 413)
    sha = hashlib.sha256(data).hexdigest()
    if ext == ".csv":
        sheets, meta = _read_csv(data)
        fmt = "csv"
    else:
        sheets, meta = _read_xlsx(data)
        fmt = "xlsx"
    return ParsedFile(name, fmt, len(data), sha, sheets, meta)


# ---------------------------------------------------------------- CSV
def detect_encoding(data: bytes) -> str:
    if data.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    for enc in ENCODINGS:
        try:
            data.decode(enc)
            return enc
        except UnicodeDecodeError:
            continue
    raise FileError("ENCODING_UNKNOWN", "인코딩을 판별할 수 없습니다 (UTF-8, CP949 만 지원).")


def detect_delimiter(text: str) -> str:
    first = next((ln for ln in text.splitlines() if ln.strip()), "")
    best = None
    for cand in DELIMITERS:
        n = len(next(csv.reader([first], delimiter=cand), []))
        if n > 1 and (best is None or n > best[1]):
            best = (cand, n)
    if best is None:
        raise FileError("DELIMITER_UNKNOWN", "구분자(쉼표, 세미콜론, 탭, |)를 찾지 못했습니다. 헤더 줄에 열이 2개 이상 필요합니다.")
    return best[0]


def _read_csv(data: bytes) -> tuple[list[ParsedSheet], dict]:
    enc = detect_encoding(data)
    text = data.decode(enc)
    delim = detect_delimiter(text)
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delim)
    records: list[tuple[int, list[str]]] = []
    prev = 0
    for rec in reader:
        start = prev + 1                      # 따옴표 안 줄바꿈이 있어도 레코드 시작 줄 번호를 쓴다
        prev = reader.line_num
        if len(records) >= MAX_ROWS:
            raise FileError("TOO_MANY_ROWS", f"행이 너무 많습니다 (최대 {MAX_ROWS}행).", 413)
        records.append((start, rec))
    cells_rows = [(n, [Cell(v if v != "" else None, "text" if v != "" else "empty") for v in rec]) for n, rec in records]
    sheet = _build_sheet("", cells_rows, merged_header=None)
    return [sheet], {"encoding": enc, "delimiter": delim}


# ---------------------------------------------------------------- XLSX
def _cell_from_openpyxl(c) -> Cell:
    v = c.value
    if v is None or (isinstance(v, str) and v == ""):
        return Cell(None, "empty")
    if isinstance(v, bool):
        return Cell("TRUE" if v else "FALSE", "bool")
    if isinstance(v, (dt.datetime, dt.date)):
        d = v.date() if isinstance(v, dt.datetime) and v.time() == dt.time(0) else v
        return Cell(d.isoformat(), "date", c.number_format)
    if isinstance(v, int):
        return Cell(str(v), "number", c.number_format)
    if isinstance(v, (float, Decimal)):
        s = format(Decimal(repr(v)) if isinstance(v, float) else v, "f")      # 지수 표기 제거, 부동소수 오차는 repr 로 차단
        if "." in s:
            s = s.rstrip("0").rstrip(".")
        return Cell(s or "0", "number", c.number_format)
    return Cell(str(v), "text")


def _read_xlsx(data: bytes) -> tuple[list[ParsedSheet], dict]:
    if not zipfile.is_zipfile(io.BytesIO(data)):
        raise FileError("XLSX_CORRUPT", "XLSX 파일이 아니거나 손상되었습니다 (.xls 는 지원하지 않습니다).")
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
    except Exception as exc:      # openpyxl 은 손상 유형별로 다양한 예외를 낸다
        raise FileError("XLSX_CORRUPT", f"XLSX 를 읽을 수 없습니다 ({type(exc).__name__}).") from exc
    sheets, hidden = [], []
    for ws in wb.worksheets:
        if ws.sheet_state != "visible":
            hidden.append(ws.title)
            continue
        if ws.max_row and ws.max_row > MAX_ROWS:
            raise FileError("TOO_MANY_ROWS", f"시트 '{ws.title}' 의 행이 너무 많습니다 (최대 {MAX_ROWS}행).", 413)
        rows = [(r[0].row, [_cell_from_openpyxl(c) for c in r]) for r in ws.iter_rows() if r]
        merged = [(m.min_row, m.min_col, m.max_col) for m in ws.merged_cells.ranges if m.max_row == m.min_row and m.max_col > m.min_col]
        sheets.append(_build_sheet(ws.title, rows, merged_header=merged))
    wb.close()
    if not sheets:
        raise FileError("NO_VISIBLE_SHEET", "읽을 수 있는 시트가 없습니다.")
    return sheets, {"hidden_sheets_skipped": hidden}


# ---------------------------------------------------------------- 공통: 헤더 구성
def _build_sheet(name: str, rows: list[tuple[int, list[Cell]]], merged_header) -> ParsedSheet:
    idx = next((i for i, (_, cells) in enumerate(rows) if any(c.kind != "empty" and (c.value or "").strip() for c in cells)), None)
    if idx is None:
        return ParsedSheet(name, [], [], [], 0)
    hrow_no, hcells = rows[idx]
    header_rows = [hrow_no]
    groups: dict[int, str] = {}
    two_level = False
    if merged_header:
        spans = [(c0, c1) for (r, c0, c1) in merged_header if r == hrow_no]
        if spans and idx + 1 < len(rows):
            two_level = True
            for c0, c1 in spans:
                g = hcells[c0 - 1].value if c0 - 1 < len(hcells) else None
                for c in range(c0, c1 + 1):
                    groups[c] = (g or "").strip()
            for c, cell in enumerate(hcells, start=1):         # 병합되지 않은 위 행 값도 그룹으로
                if c not in groups and cell.value and cell.value.strip():
                    groups[c] = cell.value.strip()
    if two_level:
        nrow_no, ncells = rows[idx + 1]
        header_rows.append(nrow_no)
        width = max(len(ncells), len(hcells))
        header = [HeaderCol(c, (ncells[c - 1].value or "").strip() or None if c - 1 < len(ncells) else None, groups.get(c) or None)
                  for c in range(1, width + 1)]
        body = rows[idx + 2:]
    else:
        width = len(hcells)
        header = [HeaderCol(c, (cell.value or "").strip() or None) for c, cell in enumerate(hcells, start=1)]
        body = rows[idx + 1:]
    raw_rows = [RawRow(n, cells) for n, cells in body if any(c.kind != "empty" and (c.value or "").strip() for c in cells)]
    width = max([width] + [len(r.cells) for r in raw_rows]) if raw_rows else width
    while len(header) < width:           # 헤더보다 넓은 데이터 열
        header.append(HeaderCol(len(header) + 1, None))
    return ParsedSheet(name, header_rows, header, raw_rows, width)
