# 배치 API 계약 v0.2 — 업로드 · 검증 · 승인 (monthly_demand, product)

> 화면(업로드·검증·승인) 담당과 예측 담당이 읽는 문서다. 기계 판독본은 서버의 `/openapi.json`(`/docs`)이며 이 문서와 어긋나면 OpenAPI 가 기준이다.
> **상태: 서비스 로직은 DB 없이 테스트로 검증했다. PostgreSQL 저장 경로는 작성만 했고 미검증**이다. 기본 설정(`PERSISTENCE=disabled`)에서 아래 `/batches*`, `/products`, `/demand/monthly` 는 모두 `501 PERSISTENCE_DISABLED` 를 돌려준다(저장되는 척하지 않음). `POST /ingest/preview` 는 저장 없이 검증만 하며 항상 동작한다.

## 1. 흐름

제품 마스터와 월별 수요는 **같은 API** 를 쓰고 `entity` 로 구분한다(`monthly_demand` 기본, `product`). 제품을 먼저 승인해야 수요가 그 제품을 참조할 수 있다.

```
POST /batches (파일, entity)    → 201 BatchDetail  원본 바이트 보관 + 파싱·매핑·검증. 업무 테이블에는 아무것도 쓰지 않음
   status = MAPPING_HOLD        → 사용자가 열을 지정 → POST /batches/{id}/revalidate (반복 가능)
   status = HAS_ERRORS          → 승인 불가. 원본 파일을 고쳐 새로 업로드하거나 반려.
                                  미등록 제품이 원인(PRODUCT_NOT_REGISTERED)이면 제품 승인 후 같은 배치를 revalidate
   status = READY_FOR_APPROVAL  → 화면에서 검증 결과 확인
POST /batches/{id}/approve      → 명시적 승인 (승인자 역할 코드 + 본 결과의 result_digest). 한 트랜잭션으로 entity 의 업무 테이블에 반영
POST /batches/{id}/reject       → 반려 (사유 필수)
GET  /products                  → 승인된 제품 마스터
GET  /demand/monthly            → 승인된 월별 수요 (예측 모듈과 같은 원천: 승인·오류 0 배치의 행만)
```

승인 후(APPROVED)·반려 후(REJECTED)에는 수정할 수 없다(최종 상태). 같은 entity 에 같은 파일(SHA-256)은 반려되지 않은 배치가 있으면 다시 업로드할 수 없다(`DUPLICATE_UPLOAD`). 한 통합문서를 `sheet` 로 나눠 제품·수요로 각각 올리는 것은 허용한다.

## 2. 상태 (`BatchStatus`)

| status | 의미 | 화면 동작 |
|---|---|---|
| `MAPPING_HOLD` | 열 매핑이 불확실(필수 열 없음·후보 둘 이상). 검증 안 함 | `validation.sheets[].holds[]`(후보 열 `candidates`) 보여주고 열 선택 → `revalidate` |
| `HAS_ERRORS` | 검증 ERROR ≥ 1 | `validation.sheets[].issues[]` 를 시트·행·열(`location.cell_ref`)과 함께 표시. 승인 버튼 비활성 |
| `READY_FOR_APPROVAL` | ERROR 0 (경고는 있을 수 있음) | 경고 확인 후 승인 가능 |
| `APPROVED` | 승인·반영 완료 (최종) | 읽기 전용 |
| `REJECTED` | 반려 (최종) | 읽기 전용, `reject_reason` 표시 |

`approval_blockers[]` 가 비어 있을 때만 승인 요청을 보낸다(비어 있어도 서버가 다시 검사하며 `KEYS_ALREADY_APPROVED` 는 승인 요청 시점에만 알 수 있다).

## 3. 엔드포인트

| 메서드 경로 | 요청 | 성공 응답 |
|---|---|---|
| `POST /ingest/preview` | multipart: `file`, 선택 `entity`, `default_unit`, `column_overrides`(JSON), `sheet` | 200 PreviewOut (저장 없음, 기존 제품 대조 없음 → `master_data_checked: false`) |
| `POST /batches` | multipart: `file`, 선택 `entity`(monthly_demand\|product), `default_unit`(수요 전용), `column_overrides`, `sheet`, `source_type`(CLIENT_UPLOAD\|PUBLIC_DEMAND\|SYNTHETIC), `source_note`, `created_by` | 201 BatchDetail |
| `GET /batches?status=&limit=&offset=` | | 200 BatchPage (최신순) |
| `GET /batches/{batch_id}` | | 200 BatchDetail |
| `POST /batches/{batch_id}/revalidate` | JSON `{expected_version, column_overrides, default_unit, sheet}` (설정 **전체 교체**) | 200 BatchDetail (version+1) |
| `POST /batches/{batch_id}/approve` | JSON `{approved_by, expected_result_digest}` | 200 BatchDetail (APPROVED) |
| `POST /batches/{batch_id}/reject` | JSON `{rejected_by, reason}` | 200 BatchDetail (REJECTED) |
| `GET /products?product_id=&q=&limit=&offset=` | `product_id` 정확히 일치, `q` 품명 부분 일치 | 200 ProductPage |
| `GET /demand/monthly?product_id=&period_from=&period_to=&limit=&offset=` | | 200 DemandPage |

예시 — 업로드 후 응답(요약):

```json
{
  "batch_id": "0b6c…", "entity": "monthly_demand", "status": "MAPPING_HOLD", "version": 1,
  "file": {"name": "demand.csv", "sha256": "…", "size_bytes": 102, "format": "csv"},
  "result_digest": "9f1a…",
  "approval_blockers": [{"code": "APPROVAL_BLOCKED_HOLD", "message": "열 매핑이 보류 상태입니다. 열을 지정해 다시 검증하세요."}],
  "validation": {"status": "MAPPING_HOLD", "approval_required": true, "auto_approve": false,
    "sheets": [{"name": "", "status": "HOLD",
      "holds": [{"code": "AMBIGUOUS_COLUMNS", "field": "demand_qty", "candidates": ["수량", "주문수량"], "message": "…"}]}]}
}
```

예시 — 재검증 → 승인:

```http
POST /batches/0b6c…/revalidate   {"expected_version": 1, "column_overrides": {"demand_qty": "주문수량"}}
→ 200 {"status": "READY_FOR_APPROVAL", "version": 2, "result_digest": "c41e…", "approval_blockers": []}

POST /batches/0b6c…/approve      {"approved_by": "REVIEWER-01", "expected_result_digest": "c41e…"}
→ 200 {"status": "APPROVED", "approved_by": "REVIEWER-01", "approved_at": "2026-11-02T01:23:45Z", "approved_record_count": 3}
```

검증 이슈(`validation.sheets[].issues[]`):

```json
{"severity": "ERROR", "code": "QTY_INVALID", "message": "수량 형식 오류: 'x' …", "field": "demand_qty",
 "location": {"sheet": "", "row": 2, "col": 4, "cell_ref": "D2"}, "raw_value": "x", "evidence": {}}
```

- CSV 는 `sheet` 가 빈 문자열. `row` 는 원본 파일의 행 번호(헤더 포함, 1부터), `cell_ref` 는 스프레드시트 표기(열 문자+행).
- 응답이 크면 `issues`·`records` 는 앞부분만 담긴다. 전체 건수는 `issues_total`, `records_total`.

## 4. 제품 마스터 (`entity=product`)

필드: 필수 `product_id`, `product_name`, `base_unit`(제품코드·제품명·단위 열) / 선택 `product_type`(FINISHED·SEMI_FINISHED, 완제품·반제품, FG·SF), `safety_stock_qty`. 선택 값이 없으면 `product_type` 은 null, `safety_stock_qty` 는 0 이며 0 으로 채운 사실을 경고(`SAFETY_STOCK_DEFAULTED`)로 알린다. 제품코드는 문자열 그대로 보존한다(선행 0 유지, 자동 생성·자동 병합 없음).

```bash
curl.exe -F "file=@제품.csv" -F "entity=product" http://127.0.0.1:8000/batches
```
```json
{"batch_id": "7d21…", "entity": "product", "status": "READY_FOR_APPROVAL", "version": 1, "result_digest": "a3f0…", "approval_blockers": [],
 "validation": {"master_data_checked": true, "sheets": [{"status": "MAPPED", "valid_records": 2, "records": [
   {"product_id": "0001001", "product_name": "히터 A", "base_unit": "EA", "product_type": "FINISHED", "safety_stock_qty": "10",
    "location": {"sheet": "", "row": 2, "col": null, "cell_ref": null}, "review_required": false}]}]}}
```
```http
POST /batches/7d21…/approve   {"approved_by": "REVIEWER-01", "expected_result_digest": "a3f0…"}
→ 200 {"status": "APPROVED", "approved_record_count": 2}

GET /products?q=히터
→ 200 {"items": [{"product_id": "0001001", "product_name": "히터 A", "base_unit": "EA", "product_type": "FINISHED",
                  "safety_stock_qty": "10", "source_type": "CLIENT_UPLOAD", "batch_id": "7d21…"}], "total": 1, "limit": 100, "offset": 0}
```

`validation.sheets[].records[]` 의 형태는 `entity` 에 따라 다르다(수요: `period`·`demand_qty`·`unit`, 제품: `base_unit`·`product_type`·`safety_stock_qty`).

기존 제품과의 관계 (덮어쓰기는 지원하지 않는다):

| 업로드 행 | 결과 |
|---|---|
| 새 코드 | 등록 대상 |
| 같은 코드, 값이 모두 같음 | 경고 `PRODUCT_ALREADY_REGISTERED`, 등록 대상에서 제외. 전부 제외되면 `EMPTY_BATCH` |
| 같은 코드, 값이 다름 | 오류 `EXISTING_PRODUCT_CONFLICT` (필드별, `evidence.existing`) |
| 새 코드, 등록된 품명과 같음 | 오류 `EXISTING_NAME_CONFLICT` (`evidence.existing_product_id`) |
| 선행 0·대소문자만 다른 등록 코드 존재 | 경고 `CODE_VARIANT` (`evidence.similar_codes`). 자동으로 합치지 않음 |

월별 수요 쪽 대조(업로드·재검증·승인 때마다 승인된 제품 기준):

| 수요 행 | 결과 |
|---|---|
| 미등록 제품코드 | 오류 `PRODUCT_NOT_REGISTERED` (`evidence.similar_codes` 에 비슷한 등록 코드). 승인 대기 중인 제품은 등록으로 보지 않음 |
| 등록 품명과 파일 품명이 다름 | 오류 `EXISTING_PRODUCT_CONFLICT` |
| 단위 종류가 기준 단위와 다름 / 같은 종류의 다른 단위 | 오류 `UNIT_DIMENSION_MISMATCH` / 경고 `UNIT_DIFFERS_FROM_BASE` |

## 5. 낙관적 동시성·승인 바인딩

- 조회한 `version` 을 `revalidate` 의 `expected_version` 으로 보낸다. 어긋나면 `409 VERSION_CONFLICT`.
- 승인은 사용자가 **화면에서 본 결과**에만 유효하다: 조회한 `result_digest` 를 `expected_result_digest` 로 보낸다. 그 사이 재검증 등으로 바뀌었으면 `409 RESULT_CHANGED` → 다시 조회해 확인 후 승인.
- 서버는 승인 시 저장된 원본을 다시 검증해 같은 `result_digest` 가 나오는지 확인한다(저장된 요약을 믿지 않음). 승인 대기 중 다른 배치가 같은 제품을 먼저 등록해 결과가 달라져도 `RESULT_CHANGED` 로 멈춘다.
- 승인자·반려자는 **역할 코드**(`^[A-Za-z0-9][A-Za-z0-9_.-]{1,59}$`, 예: `REVIEWER-01`). 실명·공백·한글은 `422 ACTOR_REQUIRED`. 인증/권한 체계가 정해지면 로그인 사용자에서 서버가 채우도록 바꾼다(미정).

## 6. 오류 형식과 코드

모든 오류: `{"detail": {"code": "…", "message": "한국어 메시지", "details": …}}`. 화면은 `code` 로 분기하고 `message` 는 그대로 표시해도 된다. FastAPI 기본 422 도 같은 형식(`REQUEST_INVALID`, `details` = `[{loc, message}]`)으로 통일했다.

| code | HTTP | 의미 / 화면 처리 |
|---|---|---|
| `PERSISTENCE_DISABLED` | 501 | 서버에 배치 저장이 꺼져 있음(개발 환경). `/ingest/preview` 만 사용 |
| `DB_UNAVAILABLE` | 503 | DB 연결 실패 → 재시도 안내 |
| `PERSISTENCE_ERROR` | 500 | 저장 실패, **모든 변경 취소됨**(상태 그대로) → 재시도 가능 |
| `REQUEST_INVALID` / `UNSUPPORTED_ENTITY` / `BAD_COLUMN_OVERRIDES` / `SOURCE_NOTE_REQUIRED` | 422 | 입력 수정 |
| `UNSUPPORTED_TYPE` | 415 | CSV/XLSX 만 |
| `FILE_TOO_LARGE` / `TOO_MANY_ROWS` | 413 | 10MB / 100,000행 한도 |
| `FILE_EMPTY` / `XLSX_CORRUPT` / `ENCODING_UNKNOWN` / `DELIMITER_UNKNOWN` / `NO_VISIBLE_SHEET` | 422 | 파일 자체 문제 → 파일 확인 |
| `BATCH_NOT_FOUND` | 404 | |
| `DUPLICATE_UPLOAD` | 409 | 같은 파일이 이미 있음. `details.batch_id` 로 이동 |
| `VERSION_CONFLICT` | 409 | 다시 조회 후 재시도 (`details.current_version`) |
| `BATCH_FINALIZED` | 409 | 이미 승인/반려된 배치는 수정 불가 |
| `ACTOR_REQUIRED` / `REJECT_REASON_REQUIRED` | 422 | 승인자/반려자/사유 입력 |
| `APPROVAL_BLOCKED_HOLD` / `APPROVAL_BLOCKED_ERRORS` / `EMPTY_BATCH` | 409 | 승인 불가 상태 (`approval_blockers` 와 같은 코드) |
| `RESULT_CHANGED` | 409 | 결과가 바뀜 → 다시 조회·확인 |
| `ALREADY_APPROVED` / `BATCH_REJECTED` | 409 | 중복 승인/반려된 배치. 재반영하지 않음 |
| `KEYS_ALREADY_APPROVED` | 409 | 이미 승인된 (제품, 월) 존재(`details.keys`). 덮어쓰기 미지원 |

검증 이슈 코드(`issues[].code`, 파싱·매핑·검증 단계): `CODE_NAME_CONFLICT`, `NAME_CODE_CONFLICT`, `PRODUCT_NOT_REGISTERED`, `EXISTING_PRODUCT_CONFLICT`, `EXISTING_NAME_CONFLICT`, `DUPLICATE_PRODUCT_ID`, `NAME_MISSING`, `NAME_INVALID`, `PRODUCT_TYPE_UNKNOWN`, `PRODUCT_ALREADY_REGISTERED`(경고), `SAFETY_STOCK_DEFAULTED`(경고), `DEFAULT_UNIT_IGNORED`(경고), `LOOKS_LIKE_OTHER_ENTITY`(경고), `UNIT_DIFFERS_FROM_BASE`(경고), `CODE_VARIANT`(경고), `CODE_NUMERIC_CELL`(경고), `CODE_RESTORED_FROM_FORMAT`(경고), `CODE_MISSING`, `CODE_INVALID`, `PERIOD_*`(MISSING·INVALID·AMBIGUOUS·NOT_MONTH_START), `QTY_*`(MISSING·INVALID·NEGATIVE·RANGE), `UNIT_*`(UNKNOWN·MISSING·COLUMN_MISSING·DIMENSION_MISMATCH·MIXED), `DUPLICATE_ROW_IDENTICAL`(경고), `DUPLICATE_KEY_CONFLICT`, `ROW_COLUMN_COUNT`, `CONTRACT_VIOLATION`. 매핑 보류 코드(`holds[].code`): `REQUIRED_FIELD_MISSING`, `AMBIGUOUS_COLUMNS`, `OVERRIDE_NOT_FOUND`, `DUPLICATE_HEADER`.

## 7. 팀 간 경계

- **예측 담당**: `GET /demand/monthly` 응답(`product_id`, `period`, `demand_qty`(문자열), `unit`, `source_type`, `batch_id`)과 DB 뷰 `v_forecast_input_monthly_demand` 는 **백엔드 쪽 제안 계약**이다. 예측 모듈이 원하는 입력 형태·`horizon` 정의가 다르면 이 계약을 조정한다(확정 전).
- **화면 담당**: 위 계약만으로 업로드·열 지정·검증 결과·승인 화면을 만들 수 있게 했다. 화면 로직(테이블 편집 UI 등)은 이 저장소 백엔드가 구현하지 않는다. 필드 수정은 "원본 파일 수정 후 재업로드" 또는 "열 지정(revalidate)" 두 가지뿐이며 셀 단위 편집 API 는 없다(필요하면 요구사항 합의 후 추가).
- **검색기 담당**: 이 API 와 무관. 후보 제안이 필요해지면 `holds[].candidates` 확장 지점에 붙인다.
