# 계획 결과 Excel 출력 API — 계약 초안 (plan-export-0.1-draft)

> 화면 담당과 예측·계획 담당이 읽는 문서다. 기계 판독본: 서버의 `/openapi.json`, [contracts/plan_export_draft.schema.json](../contracts/plan_export_draft.schema.json). 샘플: [contracts/examples/plan_export_sample.json](../contracts/examples/plan_export_sample.json) (합성 데이터).
> **상태: 초안.** 예측·계획 엔진과 계획 저장·승인 기능은 아직 없다. 이 API 의 출력은 전부 **DEMO/UNVERIFIED** 이며 공식 문서가 아니다. §6 의 항목은 담당자 확인 후 바뀔 수 있다.

## 1. 하는 일과 하지 않는 일

| 한다 | 하지 않는다 |
|---|---|
| 제출된 JSON 을 형식 검증 후 XLSX 5시트로 표시 | 예측·계획 계산, 합계·부족량·순위 계산 |
| 없는 값을 `미산출` 로 표시 | 없는 값을 0 으로 채움 |
| 입력끼리 모순되면 경고 표시 | 값 수정, 실행 가능성 재판정 |
| 모든 시트·파일명·인쇄물에 DEMO 표시 | 승인 여부 확인, 내용의 사실 검증, 파일 저장 |

DB 를 쓰지 않으므로 `PERSISTENCE` 설정과 무관하게 동작한다.

## 2. 요청

`POST /exports/plans/xlsx`, `Content-Type: application/json`, 본문은 `PlanExportRequest`.

- 수량은 **10진 문자열** (`"1200.5"`). JSON 숫자, 쉼표, 지수 표기는 422.
- 값이 없으면 `null` 또는 생략.
- 알 수 없는 필드는 422. 승인 상태를 요청에 넣을 수 없다.

```jsonc
{
  "contract_version": "plan-export-0.1-draft",     // 필수, 고정값
  "title": "합성 데이터 계획안 비교 예시",            // 필수
  "export_name": "plan_compare_sample",            // 선택, 파일명
  "data_classification": "SYNTHETIC",              // 필수: SYNTHETIC | PUBLIC_DEMAND | CLIENT_UPLOAD | MIXED
  "engine": "fixture",                             // 선택
  "generated_at": "2026-11-02T09:00:00+09:00",     // 선택
  "horizon": {"period_from": "2026-12", "period_to": "2027-02"},   // 필수
  "forecast_basis": {"quantile": "P80", "p50_method": null, "quantile_model_version": null, "trained_until": null},  // 선택
  "plans": [{                                      // 필수, 1~3건, plan_type 중복 불가
    "plan_id": "PLAN-A",
    "plan_type": "BASELINE",                       // BASELINE(기준형) | STABLE(안정형) | COST_SAVING(비용절감형)
    "feasibility": "UNVERIFIED",                   // FEASIBLE | INFEASIBLE | UNVERIFIED
    "kpis": [{"code": "TOTAL_COST", "label": "총 비용", "value": "45000000.50", "unit": "KRW"}],
    "production": [{"product_id": "0001001", "product_name": "합성 제품 A", "period": "2026-12", "planned_qty": "400", "unit": "EA",
                    "demand_qty": "380.5", "production_mode": "INHOUSE", "ending_inventory_qty": "120", "note": null}],
    "materials": [{"material_id": "M001", "material_name": "합성 자재 강판", "period": "2026-12", "required_qty": "1000.25",
                   "available_qty": "800", "shortage_qty": "200.25", "unit": "kg", "note": null}],
    "constraints": [
      {"constraint_id": "CAP-QTY-L1", "label": "라인 1 월 생산 수량", "dimension": "QUANTITY", "strength": "HARD", "status": "SATISFIED",
       "resource_id": "L1", "period": "2026-12", "limit_value": "500", "used_value": "400", "unit": "EA", "source": "합성 생산능력", "note": null},
      {"constraint_id": "CAP-HRS-L1", "label": "라인 1 작업시간", "dimension": "WORK_HOURS", "strength": "HARD", "status": "SATISFIED",
       "resource_id": "L1", "period": "2026-12", "limit_value": "176", "used_value": "150.5", "unit": "h", "source": "합성 생산능력", "note": null}
    ],
    "assumptions": ["합성 수치이며 실제 기업 데이터가 아니다"]
  }],
  "evidence": [{"source_type": "SYNTHETIC", "name": "fixture", "batch_id": null, "as_of": "2026-11-02", "description": "출력 형식 확인용"}]
}
```

**다중 CAPA**: 수량·작업시간·설비를 `constraints[]` 에 나란히 넣는다. 차원은 `dimension`(권장: `QUANTITY`, `WORK_HOURS`, `EQUIPMENT_HOURS`, `MATERIAL`, `DUE_DATE`, `OTHER`, 자유 문자열)으로 구분하고 새 종류가 생겨도 계약이 바뀌지 않는다.

## 3. 응답

### 성공 200

| 헤더 | 값 |
|---|---|
| `Content-Type` | `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet` |
| `Content-Disposition` | `attachment; filename="DEMO_UNVERIFIED_plan_compare_sample.xlsx"; filename*=UTF-8''DEMO_UNVERIFIED_plan_compare_sample.xlsx` |
| `X-Export-Status` | `DEMO_UNVERIFIED` |
| `Cache-Control` | `no-store` |

한글 파일명은 `filename*` 에 UTF-8 로 들어가고 `filename` 은 ASCII 대체값이다. 파일명은 문자·숫자·`_`·`-` 만 남기며 `DEMO_UNVERIFIED_` 접두사는 뺄 수 없다.

### 오류

형식은 다른 API 와 같다: `{"detail": {"code", "message", "details"}}`.

| HTTP | code | 언제 |
|---|---|---|
| 422 | `REQUEST_INVALID` | 필드 형식·필수값·중복 `plan_type`·알 수 없는 필드 |
| 422 | `REQUEST_INVALID` | JSON 중첩 16단계 초과, 깨진 JSON, UTF-8 이 아닌 본문 |
| 413 | `EXPORT_TOO_LARGE` | 출력 50,000행 초과(`details.rows`, `details.max_rows`) 또는 실제 수신 본문 5MB 초과 (`Content-Length` 유무·값과 무관) |

```json
{"detail": {"code": "REQUEST_INVALID", "message": "요청 형식이 올바르지 않습니다.",
            "details": [{"loc": ["body", "plans", "0", "production", "0", "planned_qty"], "message": "Input should be a valid string"}]}}
```

```json
{"detail": {"code": "EXPORT_TOO_LARGE", "message": "출력 행이 너무 많습니다 (최대 50000행).", "details": {"rows": 61000, "max_rows": 50000}}}
```

### 호출 예시

```bash
curl.exe -X POST http://127.0.0.1:8000/exports/plans/xlsx -H "Content-Type: application/json" --data-binary "@contracts/examples/plan_export_sample.json" -OJ
```

브라우저에서는 응답을 Blob 으로 받아 저장하고 파일명은 `Content-Disposition` 에서 읽는다. 다른 출처에서 호출하면 CORS 허용이 필요하다.

## 4. 시트

모든 시트: 1행 DEMO 배너, 2행 제목·데이터 구분·기간, 4행 열 제목, 5행부터 데이터, 인쇄 머리글·바닥글에 DEMO 표시.

| 시트 | 열 |
|---|---|
| 계획안 비교 | 항목, 단위, 기준형, 안정형, 비용절감형 (제출된 계획안만). 행: 계획안 ID, 실행 가능성, KPI |
| 제품별 생산계획 | 계획안, 제품코드, 제품명, 기간, 계획수량, 단위, 수요기준량, 예측 기준, 생산구분, 기말재고, 비고 |
| 자재 소요량 및 부족량 | 계획안, 자재코드, 자재명, 기간, 소요량, 가용량, 부족량, 단위, 비고 |
| 적용 제약조건 | 계획안, 제약 ID, 제약 항목, 차원, 강도, 상태, 자원, 기간, 한도, 사용량, 단위, 출처, 비고 |
| 계산 근거·데이터 출처 | 출력 등급, 입력 계약 버전, 데이터 구분, 생성 엔진·시각, 계획 기간, 예측 기준, 가정, 출처 목록(구분, 이름, 배치 ID, 기준 시점, 설명) |

값 표시: 없는 값은 `미산출`, 제출한 `0` 은 0. 유효숫자 15자리까지는 숫자 셀, 넘으면 값이 바뀌지 않도록 문자열 셀. 같은 KPI 라도 단위가 다르면 다른 행이다. 수식 시작 문자로 시작하는 문자열은 앞에 `'` 가 붙는다.

## 5. CORS

기본은 꺼져 있다. 화면을 다른 출처에서 띄우면 서버 `.env` 에 지정한다.

```
CORS_ALLOW_ORIGINS=http://localhost:5173
```

쉼표로 여러 출처를 쓸 수 있고 `*` 는 서버가 시작할 때 거부한다. 허용: GET·POST, `Content-Type` 헤더, credentials 없음. `Content-Disposition`, `X-Export-Status` 를 노출한다.

## 6. 확정이 필요한 항목

**예측·최적화 담당**
- 계획 결과가 계획 단위별로 이 구조로 나올 수 있는지 (`production`, `materials`, `constraints`)
- `plan_type` 3종의 정의, `feasibility` 판정 기준 (PoC E11 의 FEASIBLE/INFEASIBLE/UNVERIFIED 를 임시로 사용)
- `forecast_basis` 가 계획안별로 달라지는지 (현재는 요청 전체에 1개. 안정형이 P90, 기준형이 P50 이면 계획안별로 옮겨야 한다)
- `shortage_qty` 의 산식과 부호, `available_qty` 의 기준일
- 다중 CAPA 의 `dimension` 목록과 `limit_value`·`used_value` 의 단위

**업무 규칙·UX 담당**
- 계획안 비교 KPI 의 `code`·`label`·산식·표시 순서
- 시트·열 이름과 순서, 표시 언어
- 제품 수량의 표시 단위, 금액 단위

**공통**
- 정식 출력 API 의 경로와 승인 기준 (저장된 계획안 ID 로 조회, 승인 기록 포함, DEMO 표시 제거)
- 인증·요청 빈도 제한

## 7. 현재 한계

- 인증이 없어 호출자가 누구든 임의 내용을 DEMO 표시와 함께 출력할 수 있다.
- 받은 파일의 배너를 지우는 것은 막을 수 없다: 위변조 방지가 아니라 오인 방지다.
- 측정(개발 PC, 1회): 약 50,000행 생성 10초, 파일 1.7MB. 샘플과 같은 행 형태의 JSON 은 50,000행에 약 10.9MB 라서 **실제로는 본문 5MB 제한이 먼저 걸린다**(행이 이 형태면 약 23,000행). 운영 전 필요한 규모를 정해 한도를 조정한다.
- 실제 Excel 프로그램으로 열어 확인하지 않았다 (자동 테스트는 openpyxl 로 다시 읽어 검증).
