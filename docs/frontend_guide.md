# 프론트엔드 연동 안내

React 화면을 별도 프로젝트로 만드는 담당자를 위한 문서다. 이 저장소에는 프론트엔드 코드가 없다.

- 필드 규칙 상세: [api_contract_batches.md](api_contract_batches.md) (업로드·승인), [api_contract_exports.md](api_contract_exports.md) (Excel 출력, 초안)
- 기계 판독 계약: [contracts/openapi.json](../contracts/openapi.json) (타입 생성에 사용 가능), 서버 실행 시 `/docs`
- 응답 예시: [contracts/examples/responses/](../contracts/examples/responses/)

## 1. 지금 동작하는 것과 아닌 것

**먼저 읽어 주세요.** PostgreSQL 저장 경로는 아직 실제 DB 에서 실행된 적이 없다.

| 기능 | 기본 설정에서 | 비고 |
|---|---|---|
| `/health` | 동작 | DB 가 없으면 503 `degraded` 가 정상 |
| `POST /ingest/preview` | **동작** | 저장 없는 검증. 기존 제품 대조 없음 (`master_data_checked: false`) |
| `POST /exports/plans/xlsx` | **동작** | DEMO/UNVERIFIED 출력. 공식 문서 아님 |
| `/batches*`, `/products`, `/materials`, `/boms`, `/inventory`, `/demand/monthly` | **501** `PERSISTENCE_DISABLED` | 서비스 로직은 테스트 전용 인메모리 저장소로만 검증. 영구 저장은 미검증 |
| 예측, 계획 생성·저장, 승인된 계획의 정식 출력, 로그인·권한 | **없음** | 구현 전 |

따라서 지금은 `/ingest/preview` 와 `/exports/plans/xlsx` 만 실제 서버로 붙일 수 있다. 나머지는 §7 의 응답 예시로 화면을 만들고, DB 검증 후 연결한다. 501 은 "준비 안 됨" 화면으로 처리한다.

## 2. 서버 실행

```powershell
cd backend
..\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

기본 주소 `http://127.0.0.1:8000`. 설치는 [README](../README.md) 참고.

## 3. 엔드포인트 전체

| 메서드 | 경로 | 요청 | 성공 | 예시 파일 |
|---|---|---|---|---|
| GET | `/health` | | 200 | |
| POST | `/ingest/preview` | multipart: `file`, `entity`, `default_unit`, `column_overrides`, `sheet` | 200 | `29_preview_without_persistence` |
| POST | `/batches` | multipart: `file`, `entity`, `default_unit`, `column_overrides`, `sheet`, `source_type`, `source_note`, `created_by` | 201 | `01`, `03`, `05`, `07`, `09`, `13` |
| GET | `/batches` | `status`, `limit`, `offset` | 200 | `20_list_batches` |
| GET | `/batches/{batch_id}` | | 200 | |
| POST | `/batches/{batch_id}/revalidate` | JSON `expected_version`, `column_overrides`, `default_unit`, `sheet` | 200 | `10_revalidate_ready` |
| POST | `/batches/{batch_id}/approve` | JSON `approved_by`, `expected_result_digest` | 200 | `02`, `04`, `06`, `08`, `11` |
| POST | `/batches/{batch_id}/reject` | JSON `rejected_by`, `reason` | 200 | `15_reject` |
| GET | `/products` | `product_id`, `q`, `limit`, `offset` | 200 | `21_list_products` |
| GET | `/materials` | `material_id`, `q`, `limit`, `offset` | 200 | `22_list_materials` |
| GET | `/boms` | `product_id`, `bom_id`, `active_only`, `limit`, `offset` | 200 | `23_list_boms` |
| GET | `/inventory` | `item_type`, `item_id`, `as_of_date`, `limit`, `offset` | 200 | `24_list_inventory` |
| GET | `/demand/monthly` | `product_id`, `period_from`, `period_to`, `limit`, `offset` | 200 | `25_list_demand` |
| POST | `/exports/plans/xlsx` | JSON 계획 결과 | 200 파일 | §6 |

`entity`: `monthly_demand`(기본), `product`, `material`, `bom`, `inventory`. **참조되는 쪽을 먼저 승인한다: 제품·자재 → BOM·재고·월별 수요.**

수량은 전부 문자열이다(`"2.5"`). 소수 정밀도를 지키기 위함이니 화면에서 숫자로 바꿔 계산하지 말고 표시만 한다.

## 4. 상태 전이

```
                 POST /batches
                      │
       ┌──────────────┼───────────────┐
       ▼              ▼               ▼
 MAPPING_HOLD ◄─► HAS_ERRORS ◄─► READY_FOR_APPROVAL
       │              │               │        │
       │   revalidate 로 셋 사이를 오간다   │ approve
       │              │               │        ▼
       └────── reject ┴───────────────┘    APPROVED (최종)
                      ▼
                  REJECTED (최종, 같은 파일 재업로드 가능)
```

| 상태 | 화면 | 다음 행동 |
|---|---|---|
| `MAPPING_HOLD` | `validation.sheets[].holds[]` 와 후보 열 `candidates` 표시 | 사용자가 열 선택 → `revalidate` |
| `HAS_ERRORS` | `issues[]` 를 시트·`location.cell_ref` 와 함께 표시, 승인 비활성 | 원본 수정 후 새 업로드, 또는 반려. `PRODUCT_NOT_REGISTERED`·`MATERIAL_NOT_REGISTERED` 면 마스터 승인 후 `revalidate` |
| `READY_FOR_APPROVAL` | 경고와 레코드 미리보기 표시 | 사용자 확인 후 `approve` |
| `APPROVED`, `REJECTED` | 읽기 전용 | 없음 |

승인 버튼은 `approval_blockers` 가 빈 배열일 때만 활성화한다.

**반드시 지킬 것**

- 승인 요청에는 화면에 보여 준 배치의 `result_digest` 를 그대로 보낸다. 결과가 바뀌면 `RESULT_CHANGED`: 다시 조회해 사용자에게 보여 주고 다시 승인받는다. 자동 재시도하지 않는다.
- `revalidate` 에는 조회한 `version` 을 `expected_version` 으로 보낸다. `VERSION_CONFLICT` 면 다시 조회한다.
- `column_overrides` 는 전체 교체다. 이전 지정을 유지하려면 함께 보낸다.
- 자동 승인이 없다. `approval_required: true`, `auto_approve: false` 는 항상 고정이다.
- 승인자는 역할 코드(`REVIEWER-01`)이며 로그인이 없어 현재는 입력값이다. 실명을 넣으면 `ACTOR_REQUIRED` 다.
- `issues`·`records` 는 앞부분만 올 수 있다. 전체 건수는 `issues_total`, `records_total` 이다.
- `validation.sheets[].records[]` 의 형태는 `entity` 에 따라 다르다.

## 5. 공통 오류

모든 오류는 같은 형식이다. `code` 로 분기하고 `message` 는 그대로 보여 줘도 된다.

```json
{"detail": {"code": "RESULT_CHANGED", "message": "화면에서 본 검증 결과와 현재 결과가 다릅니다. …", "details": {"current_result_digest": "…"}}}
```

| HTTP | code | 처리 |
|---|---|---|
| 422 | `REQUEST_INVALID` | `details[].loc` 로 입력 위치 표시 |
| 422 | `UNSUPPORTED_ENTITY`, `BAD_COLUMN_OVERRIDES`, `SOURCE_NOTE_REQUIRED`, `ACTOR_REQUIRED`, `REJECT_REASON_REQUIRED` | 입력 수정 |
| 422 | `FILE_EMPTY`, `XLSX_CORRUPT`, `ENCODING_UNKNOWN`, `DELIMITER_UNKNOWN`, `NO_VISIBLE_SHEET` | 파일 확인 |
| 415 | `UNSUPPORTED_TYPE` | CSV, XLSX 만 |
| 413 | `FILE_TOO_LARGE`, `TOO_MANY_ROWS`, `EXPORT_TOO_LARGE` | 크기 줄이기 |
| 404 | `BATCH_NOT_FOUND` | 목록으로 |
| 409 | `DUPLICATE_UPLOAD` | `details.batch_id` 로 이동 |
| 409 | `VERSION_CONFLICT`, `RESULT_CHANGED` | 다시 조회 |
| 409 | `BATCH_FINALIZED`, `ALREADY_APPROVED`, `BATCH_REJECTED` | 읽기 전용으로 전환 |
| 409 | `APPROVAL_BLOCKED_HOLD`, `APPROVAL_BLOCKED_ERRORS`, `EMPTY_BATCH` | `approval_blockers` 로 미리 막을 수 있음 |
| 409 | `KEYS_ALREADY_APPROVED` | `details.keys` 표시. 덮어쓰기 없음 |
| 501 | `PERSISTENCE_DISABLED` | "준비 안 됨" 표시 |
| 503 | `DB_UNAVAILABLE` | 재시도 안내 |
| 500 | `PERSISTENCE_ERROR` | 변경 없음. 재시도 가능 |

검증 이슈 코드(`issues[].code`)와 매핑 보류 코드(`holds[].code`)는 [api_contract_batches.md](api_contract_batches.md) §9.

## 6. 파일 다운로드

`POST /exports/plans/xlsx` 는 JSON 이 아니라 파일을 돌려준다. `Content-Disposition` 에서 파일명을 읽는다(한글은 `filename*`). 다른 출처에서 호출하면 §8 의 CORS 설정이 필요하다.

```js
// 예시 — 프로젝트 구조에 맞게 옮겨 쓴다
async function downloadPlanXlsx(planJson) {
  const res = await fetch(`${API_BASE}/exports/plans/xlsx`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(planJson),
  });
  if (!res.ok) {
    const { detail } = await res.json();          // 공통 오류 형식
    throw new Error(`${detail.code}: ${detail.message}`);
  }
  const header = res.headers.get("Content-Disposition") ?? "";
  const utf8 = header.match(/filename\*=UTF-8''([^;]+)/);
  const ascii = header.match(/filename="([^"]+)"/);
  const name = utf8 ? decodeURIComponent(utf8[1]) : ascii?.[1] ?? "DEMO_UNVERIFIED_plan_export.xlsx";

  const url = URL.createObjectURL(await res.blob());
  const a = Object.assign(document.createElement("a"), { href: url, download: name });
  a.click();
  URL.revokeObjectURL(url);
}
```

화면에 이 파일이 **DEMO/UNVERIFIED** 이며 공식 문서가 아님을 표시한다(`X-Export-Status: DEMO_UNVERIFIED`). 파일명의 `DEMO_UNVERIFIED_` 접두사를 화면에서 지우지 않는다. 요청 본문 한도는 5MB, 한도를 넘으면 `413 EXPORT_TOO_LARGE`, JSON 중첩은 16단계까지다.

## 7. 개발용 Fixture

모두 합성 데이터다. 실제 기업 데이터가 아니다.

| 파일 | 용도 |
|---|---|
| `contracts/examples/sample_products.csv` 등 `sample_*.csv` | 업로드 화면 시험. `sample_demand_hold.csv` 는 매핑 보류, `sample_demand_errors.csv` 는 검증 오류 |
| `contracts/examples/plan_export_sample.json` | Excel 출력 요청 본문 |
| `contracts/examples/responses/NN_*.json` | 요청·HTTP 상태·응답 쌍. 화면 mock 으로 사용 |

`responses/` 는 손으로 쓴 것이 아니라 합성 CSV 로 실제 API 코드를 호출해 생성했고 테스트가 최신 여부를 검사한다. 단 `/batches` 계열은 **테스트 전용 인메모리 저장소** 결과다: 응답 형식의 예시이지 영구 저장이 된다는 뜻이 아니다. `batch_id`, `created_at` 은 고정값으로 바꿔 두었다.

바로 해 볼 수 있는 호출:

```powershell
curl.exe -F "file=@contracts/examples/sample_demand_errors.csv" http://127.0.0.1:8000/ingest/preview
curl.exe -F "file=@contracts/examples/sample_products.csv" -F "entity=product" http://127.0.0.1:8000/ingest/preview
curl.exe -X POST http://127.0.0.1:8000/exports/plans/xlsx -H "Content-Type: application/json" --data-binary "@contracts/examples/plan_export_sample.json" -OJ
```

저장소에 임시 저장 모드를 두지 않은 이유: 재시작하면 사라지는 데이터가 저장된 것처럼 보이면 승인 흐름을 오해하게 된다. 화면 개발 중에는 `responses/` 를 MSW 같은 mock 도구로 돌려준다.

## 8. CORS

기본은 꺼져 있다. 개발 서버 주소를 서버의 `.env` 에 넣고 재시작한다.

```
CORS_ALLOW_ORIGINS=http://localhost:5173
```

- 쉼표로 여러 출처 지정. `*` 는 서버가 시작할 때 거부한다.
- 허용: GET, POST, `Content-Type` 헤더. credentials(쿠키) 없음.
- 다운로드 파일명을 읽을 수 있도록 `Content-Disposition`, `X-Export-Status` 를 노출한다.
- multipart 업로드는 `Content-Type` 을 직접 지정하지 말고 브라우저에 맡긴다.

## 9. 확정 전 사항

- Excel 입력 JSON 은 초안이다: [api_contract_exports.md](api_contract_exports.md) §6.
- `GET /boms`, `/inventory`, `/demand/monthly` 는 계획 담당과 확정 전인 제안 계약이다.
- 승인자 입력은 로그인이 생기면 서버가 채우도록 바뀐다.
