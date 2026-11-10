# 다중 CAPA 데이터 계약 초안

작성: 2026-11-10 · 상태: **검토용 / 미확정 / 실행 계약 아님**

이 문서는 데이터 구조 제안이다. 엔진·API·DB·JSON Schema 구현이나 기존 계약 변경을 의미하지 않는다. 필드명과 열거형도 승인 전 제안이며 현재 서버에 전송할 수 없다. 계산·업무 결정을 기다리는 항목은 [담당자 확인 문서](multi_capacity_decision_questions.md)의 H-/N- 번호로 연결한다. 답변 전 임의의 기본 정책으로 구현하지 않는다.

## 1. 조사한 현재 계약과 충돌

확인한 소스: [데이터 계약 v1](data_contract_v1.md), [계획 계약 v0.2 문서](engine_forecast_plan_contract.md), [출력 계약](api_contract_exports.md), [행 모델](../backend/app/contracts.py), [DB 모델](../backend/app/models.py), [출력 모델](../backend/app/exports/contract.py), [승인 수요 예측 API](api_contract_forecast_runs.md), [계획 요청/응답 예시](../contracts/examples/engine/), [engine 상태](../engine/README.md), 프론트 [계획 타입](../../decision-supporter-frontend/src/types/engine.ts)·계획 비교·진단·승인 화면.

| 항목 | 실제 현재 상태 | 새 계약에서 필요한 구분 |
|---|---|---|
| 생산능력 | CapacityRow/production_capacity는 resource_id당 정규·잔업 시간/일, 합 ≤24. 기간·자원 종류·인원·설비 수 없음 | 기간별 수량/인시/설비시간을 각각 보존. 집계 인시 80h를 기존 하루 24h 모델에 넣지 않음 |
| 업로드 | 실제 ingest entity는 product/material/bom/inventory/monthly_demand뿐 | capacity 테이블 존재를 CAPA 업로드·승인 구현 완료로 해석하지 않음 |
| 수량→시간 | 계획 문서 §2는 월 N개를 정규시간/N으로 환산하는 방안 제안 | 수량 한도를 시간으로 대체하지 않음. 제품 혼합·공유 설비에 적용할 실측 라우팅 없이는 환산 불가 |
| 라우팅 | 문서의 product_id/resource_id/run_time_per_unit_h는 신규 제안. engine.plan 없음 | 공정 순서, 공유 자원, 대체 설비, 동시 인력·설비, 셋업을 별도 표현 |
| 달력 | calendar.working_days는 월별 일수. 없으면 평일 수 가정이라는 기존 제안 | 휴일·교대·고장·인원 차이를 표현할 수 없음. 실제 달력과 추정 달력을 구분하고 자동 평일 보충은 미확정 |
| 단위 | unit은 COUNT/MASS/TIME, 같은 차원 환산. 품목 입력 단위 보존 | h만으로 경과시간·인시·설비시간을 구분할 수 없음. 의미 차원 time_basis 추가 필요 |
| BOM/재고 | 승인 배치, 유효 BOM, 품목별 재고/안전재고 존재 | BOM·재고를 CAPA 배열에 복사하지 않고 승인 스냅샷 참조. 리드타임/입고예정은 미구현 |
| 안전재고·제품 종류 | 기존 모델은 안전재고 생략 시 0, product_type은 선택 | 기존 0을 사용자가 확인한 0이라고 단정하지 않음. 원본/검증 근거가 없으면 origin=UNKNOWN |
| 예측 ID | 현재 DB의 execution_id는 UUID, 엔진 run_id는 FRUN 문자열 | 계획의 실제 참조 키는 execution_id. FRUN만으로 실행을 조회하지 않음 |
| 예측 문서 시차 | data_contract_v1 문서는 예전 유일 키·열린 p50_method를 기술 | 실제 0005/예측 API의 실행별 결과·MA12/LGBM_L1이 기준. 기존 문서를 이번에 수정하지는 않음 |
| 다중 제약 출력 | ConstraintResult는 QUANTITY/WORK_HOURS/EQUIPMENT_HOURS 등을 constraints[]에 병렬 표현 가능 | 배열을 재사용하되 선택·출처·공정·자원별 기여·미적용 사유는 추가 상세 구조 필요 |
| 출력 확장 | plan-export-0.1-draft는 extra=forbid. forecast_basis는 최상위 하나 | 새 필드를 기존 export JSON에 바로 넣으면 422. 풍부한 계획 응답과 기존 export 투영을 분리 |
| 판정/계획안 | 기존 계획 문서의 HARD 우선 판정·P80 안정형·잔업 금지 비용형 등은 제안. 비용절감형 정의 두 가지가 충돌 | 문서·Fixture의 가정을 팀의 승인된 업무 규칙으로 승격하지 않음 |
| 프론트 | KPI 코드·표시 순서와 PLAN_BASIS 하드코딩. 진단 타입은 resource_id/source가 없고 period/unit을 필수 문자열로 가정 | 백엔드의 nullable/optional과 불일치. 선택 기준·자원·공정·계산 범위·미적용을 표시할 확장 필요 |
| 승인 | 현재 계획 UNVERIFIED 체크 승인과 이력은 브라우저 Mock | 정식 승인 허용 규칙으로 재사용하지 않음. N-04 결정 필요 |

## 2. 설계의 중심: 독립 한도 + 공유 자원 소비 + 선택 이력

서로 다른 제약을 한 숫자에 합치거나 `min(수량 환산값들)`로 치환하지 않는다. 다음 세 종류를 분리한다.

1. **사실 데이터**: 제품별 수량 한도, 자원별 가용시간, 공정별 자원 소요, 달력, BOM·재고·비용.
2. **정책/선택**: 사용자가 선택한 제약 목록, 강도, 동시 적용 방식, 결측 처리, 계획안 목적·KPI 정의.
3. **평가 증거**: 어떤 버전의 사실과 선택으로 한도·사용량·미확인을 계산했는지, 공유 자원의 사용량에 어느 제품·공정이 기여했는지.

여러 제약의 **동시 보존**은 구조 요구사항이다. 여러 선택을 AND로 강제할지, 선택 프로필로 나눌지, 우선순위/예외를 허용할지는 H-01/N-01의 미확정 정책이다. 대체 설비의 CHOOSE_ONE과 사용자 CAPA 선택의 결합 방식도 별개다.

월 합계가 맞는다는 사실만으로 교대 내 동시 작업·공정 선후·필수 납기까지 검증했다고 표시하지 않는다. 월 집계와 상세 일정의 검증 범위를 구분한다(H-03/H-04).

## 3. 공통 표기·타입·출처

아래 배열은 **논리 입력 묶음 이름**이며 새 DB 테이블이나 업로드 entity 확정안이 아니다.

### 3.1 공통 타입

| 타입 | 초안 정의 |
|---|---|
| Code | 기존 규칙 재사용: ASCII 영문/숫자 시작, 영문·숫자·`_ . -`, 1~40자. 문자열 코드와 선행 0 보존. 네임스페이스별 구분 |
| Version | 1~40자 버전 코드. 같은 id+version의 내용은 불변이라는 스냅샷 제안 |
| Qty | JSON 10진 문자열, 비음수, 유한값, 총 18자리/소수 4자리 이내. 기존 품목 수량 계약 범위 유지 |
| Measure | 시간·속도·계수·단가용 10진 문자열 제안: 총 18자리/소수 6자리 이내. 각 필드의 ≥0 또는 >0 검증 적용. 기존 DB 정밀도 변경 승인을 의미하지 않음 |
| Integer | 개수·순번. 물리 설비/인원 수는 정수. 부분 근무를 0.5대 설비로 표시하지 않음 |
| Month / Date | YYYY-MM / YYYY-MM-DD. 월 범위는 양 끝 포함. 유효일은 [valid_from, valid_to]이며 null 종료일은 무기한 |
| Timestamp | offset 포함 ISO 시각. 달력의 time_zone과 일치 여부 검사. 교대 종료일이 다음 날이면 종료 시각에도 다음 날짜 명시 |
| Hash64 / Digest | 원본 SHA256·기존 result_digest는 64자리 hex. 실행 input_digest는 `sha256:` 접두사를 가진 별도 형식. 둘을 혼용하지 않음 |
| R/C/O | R=필수, C=조건부 필수, O=선택. O의 null/생략은 미확인이지 0/무제한/기본 자원이라는 뜻이 아님 |

필수 열의 누락·음수·잘못된 타입·알 수 없는 참조/단위는 검증 오류다. **행의 필수 열 누락**과 **계획 기간 일부의 행 부재**는 구분한다. 후자는 선택한 제약의 coverage gap으로 기록하고 H-10/N-03에서 실행 거부 또는 UNVERIFIED 평가 방식을 정한다. 기존 승인 오류 0 게이트를 완화하지 않는다.

### 3.2 단위와 의미 차원

- 기존 units[]/품목 base_unit을 재사용한다. kg↔g, h↔min처럼 같은 물리 차원만 Decimal로 환산한다. 입력 단위·원값·환산계수·기준 단위를 함께 추적한다.
- 시간에는 `time_basis=ELAPSED|PERSON_HOUR|MACHINE_HOUR`를 추가 제안한다. 표기 단위 h/min과 의미 차원은 별개다. PERSON_HOUR와 MACHINE_HOUR를 서로 환산하지 않는다.
- 경과시간→인시/설비시간은 명시된 투입 수·달력/동시성 근거가 있어야 한다. 이미 집계된 인시를 다시 인원 수로 곱하지 않는다.
- 속도는 `생산수량 / 기준시간` 두 필드로 표현한다. `EA/h`를 현재 TIME 단위 테이블에 임의 등록하지 않는다. 서로 다른 제품의 EA를 합산해도 되는지는 별도 집계 정책이다.
- 반올림은 입력 정규화·부하 계산·실행 판정·표시를 구분한다. 허용 오차, 정수 올림/내림, 반복 소수 처리는 H-04/H-12 결정 전 고정하지 않는다. 표시 반올림값으로 제약을 판정하지 않는 방향을 제안한다.

### 3.3 source_manifest[]와 공통 행 근거

모든 신규 사실 행은 `source_ref:R Code`, `source_row_key:R Code`, `location:O {sheet,row,column}`를 가진다. location은 원본 위치이며 외부 PC 절대 경로·개인 실명은 넣지 않는다.

| manifest 필드 | 필수/타입 | 의미·검증 |
|---|---|---|
| source_ref, dataset_kind, dataset_version | R Code/Code/Version | 아래 사실 배열의 출처를 식별. 같은 버전에 내용이 다르면 충돌 |
| origin | R enum | EXISTING_APPROVED / SYNTHETIC_DRAFT / SCENARIO_OVERRIDE 제안. draft/override를 승인된 원본으로 위장하지 않음 |
| batch_id, batch_version | C UUID / Integer≥1 | 승인 데이터이면 실제 서버 배치 참조. 미래 CAPA 승인 방식은 N-09 확정 필요 |
| source_sha256, result_digest | C Hash64 | 승인 데이터이면 원본·검증 지문. 서버가 조회해 바인딩하며 클라이언트 선언을 신뢰하지 않음 |
| data_classification | R enum | 기존 SYNTHETIC/PUBLIC_DEMAND/CLIENT_UPLOAD/MIXED 구분 유지 |
| base_source_ref, change_note | C Code / string | 시나리오 override라면 원본 참조와 변경 사유. 원본 갱신은 아님 |

승인 상태·승인자·승인 시각은 서버가 생성하는 사실이다. 새 CAPA의 업로드/승인 API가 존재하지 않으므로 지금 APPROVED 자료가 있다고 가정하지 않는다. 기존 데이터의 필드별 기본값 사용 여부를 근거로 복원할 수 없으면 `value_origin=UNKNOWN`으로 기록할 확장안을 검토한다.

## 4. 자원·공정·집계 범위

### 4.1 resources[]

| 컬럼 | 필수/타입 | 의미·검증·누락 처리 |
|---|---|---|
| resource_id, resource_name | R Code / string≤200 | 기존 resource_id 의미를 유지할 자원 마스터. 이름으로 자동 매칭하지 않음 |
| resource_kind | R enum | WORKCENTER/LABOR_POOL/EQUIPMENT/EQUIPMENT_POOL 제안 |
| time_basis | R enum | 3.2의 의미 차원. 종류와의 허용 조합을 명시적으로 검증 |
| parallel_units | C Integer≥1 | 단일 물리 설비는 1, 집계 풀은 실제 동시 사용 가능한 수. FTE 추정과 구분 |
| calendar_id | O Code | 기본 달력 참조. 없고 시간 예산도 없으면 가용시간 미확인 |
| valid_from, valid_to | R Date / O Date | 동일 자원 버전의 유효기간. 역전/중복 유효 버전 검사 |

`resource_members[]`: `group_resource_id:R`, `member_resource_id:R`, `valid_from:R`, `valid_to:O`. 둘 다 resources를 참조하고 자기 참조·순환·같은 물리 설비의 중복 소속 사용을 검사한다. 상위 작업장과 하위 설비의 예산은 독립 제약일 수 있으나 시간을 가용량으로 두 번 더하지 않는다. 풀/개별 설비 중 실제 할당 수준은 H-05에서 정한다.

`operations[]`: `operation_id:R Code`, `operation_name:R string≤200`. 제품 라우팅의 공정 마스터다. resource_id와 같은 코드 공간으로 합치지 않는다. 미등록 공정은 오류다.

### 4.2 capacity_scopes[] / scope_products[]

| 컬럼 | 필수/타입 | 의미 |
|---|---|---|
| scope_id, scope_kind | R Code / enum | PRODUCT / PRODUCT_SET / RESOURCE_OUTPUT 제안 |
| product_id | C Code | PRODUCT이면 기존 승인 product 참조 |
| resource_id | C Code | RESOURCE_OUTPUT이면 자원 참조 |
| aggregation_basis | R enum | PRODUCT_NATIVE / SHARED_NATIVE_UNIT / EQUIVALENT_OUTPUT 제안 |
| aggregation_policy_id | C Code | 복수 제품의 수량을 합산하려면 합의된 집계 정책 필요 |

PRODUCT_SET/RESOURCE_OUTPUT의 대상 제품은 `scope_products[]{scope_id,product_id}`로 명시한다. 빈 범위·중복 멤버는 오류다. 혼합 단위는 자동 합산하지 않는다. EQUIVALENT_OUTPUT은 제품별 양의 환산계수·기준 수량·등가 단위 정의가 추가로 필요하며, H-06/N-01 합의 전 사용 불가다. 기존 EA가 같다는 이유만으로 다른 제품의 생산능력을 동일하게 취급하지 않는다.

## 5. 월 수량 한도·달력·자원 가용시간

### 5.1 quantity_limits[]

| 컬럼 | 필수/타입 | 검증·누락 의미 |
|---|---|---|
| capacity_id, scope_id | R Code | 한도 정의 ID와 4.2 범위 참조 |
| period | R Month | 월 단위. (capacity_id,period) 유일 |
| limit_qty, unit | R Qty / Code | 명시적 0은 생산 불가 한도. scope의 단위·집계 방식과 일치해야 함 |
| accounting_point | R enum | FINISHED_OUTPUT / OPERATION_OUTPUT 제안. 어떤 생산량을 세는지 필수 |
| operation_id | C Code | OPERATION_OUTPUT이면 필수. 중간 공정 통과량을 완제품 수량에 다시 더하지 않음 |
| includes_overtime | R bool | 수량 한도가 잔업 포함인지 원본 사실로 구분. 잔업 허용 여부 자체는 계획 정책 |

기간 행이 없으면 한도 미확인이다. 0이나 무제한으로 채우지 않는다. 수량 한도만 알 때 가상 run_time을 생성하지 않는다.

### 5.2 calendars[] / calendar_shifts[] / resource_shift_assignments[]

| 논리 행 | 필수 컬럼 | 선택·조건부 컬럼 | 검증·기준 |
|---|---|---|---|
| calendars | calendar_id, time_zone, valid_from | valid_to | 지역 달력의 유효기간. time_zone 예: Asia/Seoul |
| calendar_shifts | calendar_id, date, shift_id, day_type | regular_start/end, break_intervals[], overtime_windows[] | day_type=WORKDAY/HOLIDAY/SHUTDOWN 제안. 작업 구간이 있으면 시작·종료 둘 다 필수, 종료>시작 |
| resource_shift_assignments | resource_id, calendar_id, date, shift_id, available_units | assignment_group_id | 자원/교대 참조, available_units는 0 이상 정수. 단일 설비는 0 또는 1 |
| resource_unavailability | unavailability_id, resource_id, starts_at, ends_at, reason | affected_units | 고장·보전·휴무 구간. affected_units 누락 시 몇 대/몇 명 제외인지 미확인 |

구간은 offset 포함 Timestamp이며 날짜·달력 시간대와 교대의 익일 종료를 검증한다. 휴무를 나타내는 명시적 행과 날짜 자체가 누락된 경우를 구분한다. 서로 다른 사람이 배치된 교대의 겹침까지 무조건 거부하지는 않는다. 동일 물리 단위의 겹침/사용 가능 수 초과 여부를 확인할 근거가 없으면 동시성 검증이 미완료다.

이 표의 모든 `*_id`는 Code, `date/valid_from/valid_to`는 Date, `time_zone`은 IANA 시간대 문자열이다. `regular_start/end`, `starts_at/ends_at`, 휴게·잔업 구간의 `start/end`는 Timestamp, `available_units/affected_units`는 Integer≥0이다. 각 휴게·잔업 구간에는 Code형 interval_id를 둔다. 교대 키는 (calendar_id,date,shift_id), 배치 키는 (resource_id,calendar_id,date,shift_id,assignment_group_id)이며 복수 배치가 없으면 assignment_group_id는 생략한다. 같은 키의 중복 행은 오류이며 인원 수를 더하는 방법으로 합치지 않는다.

휴게·보전·잔업 구간의 중복 차감을 피할 원본 구간과 식별자를 보존한다. 실제 차감·우선순위·휴일 잔업 허용·가동률 계산은 H-02/N-08에서 정한다. 달력 없이 월~금으로 채우는 것은 현재 초안의 확정 규칙이 아니다.

### 5.3 hours_budgets[]

| 컬럼 | 필수/타입 | 의미·검증 |
|---|---|---|
| capacity_id, resource_id, period | R Code/Code/Month | 자원·월별 한도. 동일 capacity_id/resource_id/period는 한 권위 출처만 사용 |
| availability_mode | R enum | EXPLICIT_PERIOD / DERIVE_CALENDAR 제안 |
| time_basis, unit | R enum / Code | ELAPSED/PERSON_HOUR/MACHINE_HOUR와 h/min. resources와 의미 차원 일치 |
| regular_hours | C Measure≥0 | EXPLICIT_PERIOD이면 필수. DERIVE_CALENDAR에서는 직접 입력하지 않고 계산 결과에 별도 기록 |
| overtime_hours_limit | O Measure≥0 | 모르면 null. 선택한 계획이 잔업을 사용하면 누락을 coverage gap으로 처리 |
| calendar_id | C Code | DERIVE_CALENDAR이면 달력·자원 배치·해당 기간 커버가 필요 |
| availability_basis | R enum | GROSS/NET 제안. 보전/휴게 등이 이미 차감된 값인지 추적 |
| loss_policy_id | O Code | 가동 손실 적용 근거. 시간과 속도에 같은 손실을 중복 적용하지 않음 |

명시적 월 시간과 달력 계산치를 더하지 않는다. 둘을 제공하려면 어느 것이 한도의 권위 출처인지 또는 서로 다른 독립 한도인지 명시한다. 불일치는 진단 대상으로 남기고 작은 값/큰 값으로 조용히 대체하지 않는다(H-02/H-05).

단일 설비의 경과시간은 실제 구간 길이 이내여야 한다. 풀의 인시/설비시간에는 물리 수를 고려하므로 기존 CapacityRow의 하루 24h 규칙을 집계 시간에 그대로 적용할 수 없다. 월별 숫자만으로 근무 시간대의 실제 겹침·공정 일정을 검증했다고 표시하지 않는다.

## 6. 설비 속도와 제품별 공정·설비 라우팅

### 6.1 equipment_rates[]

| 컬럼 | 필수/타입 | 의미·검증 |
|---|---|---|
| rate_id, resource_id, product_id, operation_id | R Code | 해당 설비/공정에서 해당 제품의 속도. 승인 제품·공정·설비 참조 |
| output_qty, output_unit | R Qty>0 / Code | 속도의 생산량 분자. 품목 기준 단위로 환산 가능해야 함 |
| per_time, time_unit | R Measure>0 / Code | 속도의 시간 분모. 0 속도/0 분모는 오류 |
| rate_scope | R enum | PER_RESOURCE_UNIT / TOTAL_RESOURCE_POOL 제안. 풀 전체 속도인지 설비 한 대 속도인지 구분 |
| speed_kind | R enum | RATED / EFFECTIVE 제안. 명판 속도와 검증된 실효 속도를 구분 |
| efficiency_factor, loss_policy_id | O Measure(0,1] / Code | 사용하려면 손실 정책 필요. null을 1로 가정하지 않음 |
| valid_from, valid_to | R Date / O Date | 제품/설비/공정의 적용 기간. 여러 유효 속도가 있으면 명시적 선택 없이는 모호성 오류 |

속도는 독립적인 가용시간이 아니다. 같은 속도 행을 여러 제품에 복제해 각각 설비 전체 시간을 부여하면 안 된다. 실효 속도에 효율을 또 곱하거나 NET 시간에서 같은 손실을 또 차감하지 않도록 계보를 남긴다.

### 6.2 routing_headers[] / routing_steps[]

| 행 | 필수 컬럼 | 선택·조건부 컬럼 | 검증 |
|---|---|---|---|
| routing_headers | routing_id, routing_version, product_id, valid_from | valid_to | 제품별 적용 라우팅 버전 선택이 유일해야 함. 날짜 중간 변경의 처리 방식은 H-04 |
| routing_steps | routing_id, step_id, operation_id, predecessor_step_ids[], reference_qty, qty_unit | setup_policy_id | (routing_id,step_id) 유일. 선행 목록 []는 명시적 시작 공정. 다른 라우팅 참조·순환·없는 선행은 오류 |

reference_qty는 양의 Qty다. 단위 소요시간의 분모가 제품 1개인지 묶음 100개인지 보존한다. 라우팅은 기존 BOM과 별개다. 반제품 생산 경로도 별도 제품 라우팅으로 연결하며 BOM 소요량과 공정 통과량을 혼동하지 않는다.

구조는 복수 공정·대체/동시 자원을 표현하지만, 병렬 공정·배치 생산·재작업·셋업 순서 최적화를 현재 엔진이 지원한다는 뜻은 아니다. 지원 범위 밖 패턴은 UNSUPPORTED로 남기고 선택 제약을 만족했다고 처리하지 않는다(H-04/H-10).

### 6.3 routing_requirement_groups[] / routing_resource_requirements[]

| 컬럼 | 필수/타입 | 의미 |
|---|---|---|
| routing_id, step_id, requirement_group_id | R Code | 공정 내 요구 그룹. 예: 설비 선택 그룹과 작업 인력 그룹을 각각 유지 |
| relation | R enum | 그룹 내 ALL_REQUIRED / CHOOSE_ONE 제안. 원본 공정의 동시/대체 관계이지 전역 CAPA 선택 정책이 아님 |
| requirement_id, resource_id | R Code | 그룹의 자원 후보. 실제 선택/배분은 출력에서 기록 |
| usage_model | R enum | TIME_PER_REFERENCE_QTY / EQUIPMENT_RATE_REF 제안 |
| run_time, time_unit, usage_time_basis | C Measure>0 / Code / enum | TIME_PER_REFERENCE_QTY이면 필수. 6.2 reference_qty를 처리할 소요시간 |
| rate_id | C Code | EQUIPMENT_RATE_REF이면 6.1의 해당 제품·공정·자원 속도 참조. inline run_time과 동시에 주지 않음 |
| resource_units | C Integer≥1 | 경과시간을 집계 인시/설비시간으로 해석할 때 필요한 실제 동시 투입 수. 이미 집계된 소요시간에는 다시 곱하지 않음 |
| setup_time, setup_time_unit | O Measure≥0 / Code | 명시적 0과 미확인을 구분. 사용 횟수/묶음 크기/교체 기준은 setup_policy_id와 H-04 결정 필요 |

필수 필드/속도 참조의 XOR, 시간/수량 차원, 원본/선택 자원 수의 일관성을 검증한다. ELAPSED 기반 소요와 PERSON_HOUR/MACHINE_HOUR 기반 소요를 동일식으로 취급하지 않는다. 작업자 3명이 2h 사용하는 경우와 이미 6인시로 적힌 경우를 구분할 데이터가 있어야 한다.

대체 설비 M1/M2는 각자의 가용량을 가진다. 후보 목록이 있다는 이유로 두 설비의 시간을 합치거나 둘 다 쓴 것으로 계산하지 않는다. 어느 제품·공정 물량이 어느 후보에 배분됐는지 출력한다. 공정의 인력과 설비가 동시에 필요한 경우 각 요구와 부하를 모두 남긴다.

## 7. BOM·재고·안전재고·비용

### 7.1 기존 입력 재사용

| 입력 | 유지 필드·참조 | 검증·누락 처리 |
|---|---|---|
| products/materials | 기존 id/base_unit/safety_stock_qty. product_type 선택성 유지 | 역할을 알 수 없는 제품을 임의로 완제품으로 지정하지 않음. 계획 대상은 planning_scope에서 명시 |
| boms | 승인 batch, bom_id/version, 유효기간, base_qty, lines의 구성품·단위·스크랩 | 계획 생산일/월의 유효 BOM이 유일해야 함. 순환/단위 불일치 오류. 월중 버전 교체 방식 H-07 |
| inventory | item_type/item_id/as_of_date/on_hand_qty/allocated_qty/unit | 기준일별 가용량과 데이터 신선도 기준 추적. 없으면 재고 0 가정 금지 |
| safety_stock | 기존 품목 안전재고 + 별도 정책 버전/기간 override 제안 | HARD/SOFT·포함 여부·기간말 기준은 H-07/N-02 결정. 누락 기본값 0의 근거 불명은 UNKNOWN |
| 조달 근거 | 미래 확장: 품목, 확정 입고량/예정일, 승인 출처, 리드타임·외주 조건 | 현재 계약에 없으므로 있다고 가정하지 않음. 자재 부족을 VIOLATED/UNVERIFIED 중 무엇으로 처리할지는 미확정 |

MATERIAL_BALANCE와 SAFETY_STOCK은 수량/시간 한도와 별개의 constraint definition이다. BOM의 구성품 수요를 합산한 뒤 공유 자재 재고를 한 번만 사용하는 추적이 필요하다. 구체적인 월 이월·입고 시점·안전재고 계산식은 H-07/H-09 결정 대상이다.

### 7.2 cost_rates[] (선택 묶음)

| 컬럼 | 필수/타입 | 검증·누락 의미 |
|---|---|---|
| cost_rate_id, target_kind, target_id | R Code / enum / Code | MATERIAL/RESOURCE/ITEM_HOLDING/OPERATION_SETUP 제안. 해당 마스터 참조 |
| cost_component, currency | R Code / Code | 정규/잔업/자재/보관/셋업을 구별. 통화가 다르면 합의된 환산 데이터 없이는 합계 산출 불가 |
| amount, per_quantity, quantity_unit | R Measure≥0 / Measure>0 / Code | 예: KRW per kg. 금액 0은 명시적 무료, null은 미확인 |
| time_class, billing_basis | R enum / enum | REGULAR/OVERTIME/NOT_APPLICABLE 및 PER_ITEM_UNIT/PER_PERSON_HOUR/PER_MACHINE_HOUR/PER_SETUP/PER_ITEM_MONTH 제안 |
| valid_from, valid_to | R Date / O Date | 가격 유효기간. 일/월 비용과 인시/설비시간 비용을 섞지 않음 |

비용 자료가 없으면 관련 KPI는 null/미산출이다. 0원으로 총비용을 만들지 않는다. 다만 비용이 목적함수에 필수라면 해당 계획안의 생성 가능 여부를 따로 판정해야 한다(H-08/H-10/N-02). 상위 작업장 비용과 하위 설비 비용의 중복 청구 여부도 cost_component/정책으로 구별한다. 이 절은 단가 구조이며 KPI 산식을 확정하지 않는다.

## 8. 선택·적용·검증 범위 계약

### 8.1 constraint_definitions[] / constraint_selections[]

| 정의 필드 | 필수/타입 | 설명 |
|---|---|---|
| definition_id, label | R Code / string≤200 | 원래 한도/규칙 식별자 |
| kind | R enum | QUANTITY_LIMIT/WORKCENTER_TIME/LABOR_TIME/EQUIPMENT_TIME/MATERIAL_BALANCE/SAFETY_STOCK 제안 |
| scope_id / resource_id / item reference | C Code/복합 참조 | kind별 대상. 품목은 item_type+item_id로 공간 구분 |
| capacity_id 또는 rule_ref | C Code | 실제 한도 데이터나 자재/안전재고 정책 참조 |
| period_from, period_to | R Month | 적용 기간, 계획 horizon과 겹쳐야 함 |

| 선택 필드 | 필수/타입 | 설명 |
|---|---|---|
| selection_id, definition_id | R Code | 사용자 선택과 원본 정의를 별도로 추적 |
| selected | R bool | 미선택도 응답에 보존. 서버 필수 제약을 임의로 해제할 수 있는지는 N-01 |
| strength | C enum | 선택 시 HARD/SOFT, 반드시 승인된 규칙/정책 근거 필요. UI 임의 기본 HARD 아님 |
| plans | R enum[] | BASELINE/STABLE/COST_SAVING 중 적용 대상. 빈 대상은 오류 |
| period_from, period_to | R Month | 정의 범위 안에서 선택. 계획 범위 커버 여부 별도 검사 |
| policy_ref | C Code | 강도·결측·예외·동시 적용 정책 근거. 미확정이면 실행용으로 사용할 수 없음 |

`capacity_application_policy`에는 `policy_id/version`, `combination_mode`(UNDECIDED/ALL_SELECTED/SEPARATE_PROFILES/기타 합의안), `missing_input_policy`, `verification_scope`, `required_checks[]`를 둔다는 제안이다. enum 확정도 H-01/H-10/N-01/N-03 답변 후다. UNDECIDED는 초안 표현용이며 계산에 사용할 기본값이 아니다.

### 8.2 coverage 및 평가 추적

선택 목록과 자원/제품/기간을 펼친 **expected evaluation 집합**을 먼저 기록하는 방안을 제안한다. 결과 행이 없다는 이유로 충족으로 간주하지 않는다.

`constraint_evaluations[]`의 제안 필드:

| 필드 | 의미 |
|---|---|
| evaluation_id, plan_id, selection_id, definition_id | 평가 한 행·안·선택·원본 정의 식별. 평가 ID는 안 내에서 유일 |
| application_state | APPLIED / NOT_SELECTED / MISSING_INPUT / UNSUPPORTED 제안. 기존 status 3종과 별개 |
| dimension, strength, resource_id, product_id/step_id, period | 무엇을 어떤 단위·강도로 어느 기간에 평가했는지 |
| status | APPLIED이고 평가 가능할 때 SATISFIED/VIOLATED. 미확인인 선택 제약은 정책에 따라 UNVERIFIED 또는 요청 거부. 미선택에 SATISFIED를 붙이지 않음 |
| limit_value, used_value, unit, time_basis | 원래 차원의 한도와 합계 부하. 알 수 없으면 null. 서로 다른 차원을 더하지 않음 |
| input_refs[], conversion_refs[], rule_version | source_ref/row key/품목·라우팅·달력/단위 환산 및 규칙 버전 |
| reason_codes[], note, missing_fields[] | 위반·미확인·지원 불가·미적용 사유 |
| contributions[] | 제품·공정·자원 할당별 생산량/소요시간/정규·잔업 분리. 합계 used_value와 대조 가능해야 함 |

결측 데이터를 0 한도로 해석하면 가짜 INFEASIBLE, 무제한으로 해석하면 가짜 FEASIBLE이 된다. 두 경우 모두 피한다. 구조 오류/계산 실패와 업무 제약 위반도 구분한다.

기존 계획 초안의 집계 방식을 **유지한다고 팀이 확인하는 경우** HARD 위반 우선 INFEASIBLE, 그렇지 않고 필수 검증 미확인이 있으면 UNVERIFIED, 필요한 모든 검증이 충족돼야 FEASIBLE로 제안한다. HARD/SOFT의 대상·필수 검증 목록·미선택 처리·판정 범위는 아직 확정되지 않았다. 선택 결합 정책 자체가 미정이면 최종 실행 가능 판정을 내리지 않는다.

UNVERIFIED 계획의 승인 가능 여부는 판정과 다른 업무 결정이다. FEASIBLE도 자동 승인 사유가 아니다. 승인 API·권한·서명·정식 출력은 이번 계약의 구현 완료 항목이 아니다.

## 9. 기존 plan_request / plan_response와 연결

### 9.1 요청 확장안

버전 후보: `plan-input-0.2-multi-capacity-draft`. 기존 request_id/as_of_date/horizon/scenario/products/materials/units/boms/inventory/costs/policy/evidence를 최대한 유지한다.

- `forecast`에 **execution_id(UUID)**와 저장된 예측 input_digest를 추가한다. 기존 run_id(FRUN)와 rows는 백엔드가 저장된 실행에서 구성한다. 호출자가 rows를 바꾸는 우회 경로를 두지 않는다. 승인된 것은 입력 수요이며, 예측 실행 자체가 정식 계획 승인을 받았다는 뜻은 아니다.
- 실행 상태 FAILED/INPUT_INVALID/DATA_INSUFFICIENT는 계획 수요로 사용할 수 없다. PARTIAL은 planning_scope의 독립 수요 대상·예측 월 커버를 확인한 후 누락 처리 정책에 따른다(H-11/N-05). 제외 제품을 0 수요로 채우지 않는다.
- `planning_scope.product_ids[]`와 요구 기간을 명시한다. forecast target_month 밖으로 horizon을 확장하거나 날짜를 바꿔 끼우지 않는다.
- `multi_capacity`에 §§3~8의 사실 배열·제약 정의·선택·정책 참조를 추가한다. 계획 입력 digest는 예측 execution_id/digest와 공급·CAPA·정책 스냅샷을 함께 포함한다.
- `capacity[]`, `routings[]`, `calendar.working_days`는 호환용 표현으로 남길 수 있으나 새 데이터와 중복 합산하지 않는다. 신·구 두 권위 값이 다르면 거부/명시적 해소하며 조용히 하나를 택하지 않는다.
- 기존 단일 공정 legacy 입력을 변환할 때는 표현 가능한 시간 제약만 만든다. 없는 설비·인원·속도·휴일을 생성하지 않고 미지원/누락으로 기록한다.
- what-if는 같은 예측 execution_id를 참조하고 변경한 사실/선택/정책과 원본 입력 digest를 별도로 기록한다. 원본 승인 데이터는 수정하지 않는다. 재예측 여부는 별도 행동이다.

연결 구조 발췌(배열 상세는 생략한 **설명용 JSON**, 현행 서버 요청이 아님):

```json
{
  "contract_version": "plan-input-0.2-multi-capacity-draft",
  "forecast": {
    "execution_id": "00000000-0000-4000-8000-000000000001",
    "run_id": "FRUN-example",
    "input_digest": "sha256:<saved-forecast-digest>",
    "rows": []
  },
  "multi_capacity": {
    "contract_version": "multi-capacity-0.1-draft",
    "constraint_selections": [
      {"selection_id":"S-Q","definition_id":"Q-P1","selected":true,"strength":null,"plans":["BASELINE"],"policy_ref":null},
      {"selection_id":"S-M","definition_id":"H-M1","selected":true,"strength":null,"plans":["BASELINE"],"policy_ref":null}
    ],
    "capacity_application_policy": {"combination_mode":"UNDECIDED"}
  }
}
```

null strength/policy와 빈 rows는 미확정 구조를 설명하는 표기이며 실행 가능한 입력이 아니다.

### 9.2 응답과 export 호환

버전 후보는 `plan-output-0.2-multi-capacity-draft`다. 기존 status/input_digest/engine/scenario/errors/warnings/comparison/export를 유지하고, 바깥에 forecast_execution_id, policy_snapshot, capacity_selection_snapshot, constraint_evaluations, coverage, resource_allocations를 추가하는 제안이다.

- engine status는 계산 처리 결과, plan.feasibility는 업무 제약 판정이다. status=OK여도 INFEASIBLE/UNVERIFIED일 수 있다.
- export는 우선 **기존 plan-export-0.1-draft 그대로** 유지한다. 바깥의 새 필드를 export로 그대로 전달하지 않는다.
- constraint_evaluations의 적용된 평가를 export.plans[].constraints[]에 투영한다. constraint_id는 평가 행 ID로 하여 같은 원본 정의를 여러 월/제품에 펼쳐도 중복되지 않게 한다. 최대60자를 지키며 임의 절단으로 충돌시키지 않는다.
- dimension은 기존 권장 QUANTITY/WORK_HOURS/EQUIPMENT_HOURS/MATERIAL/OTHER 등에 맞춘다. time_basis·원본 정의·선택·기여 내역은 바깥에 유지한다.
- 미선택 항목을 기존 export의 SATISFIED로 위장하지 않는다. 선택 목록 전체를 출력하려면 향후 export 새 버전이 필요하다. assumptions/note/source는 사람을 위한 보충 설명이며 기계 판독용 추적의 대안이 아니다.
- 같은 kind/resource/month라도 별개의 독립 제약이면 별도 평가 행으로 남긴다. 공유 설비의 합계 부하와 제품별 기여도 버리지 않는다.
- plan-export 최상위 forecast_basis.quantile 하나로는 안별 P50/P80/P90을 표현할 수 없다. 안별 forecast_quantile/demand_multiplier/목적/제약 프로필을 바깥에 유지하고, 현행 export에는 quantile=null과 명시적 assumptions 등으로 호환 투영하는 방안을 검토한다. 정식 열 확장은 N-06/N-11 합의 후다.
- KPI의 code+unit+계산 기준 버전을 비교 키로 검토한다. null을 0으로 만들거나 비교 기준이 다른 행을 같은 숫자로 순위 매기지 않는다. KPI 식·순위·추천은 H-09/N-06 미확정이다.

## 10. 공유 설비 병목을 보존하는 예

아래는 합성 설명값이다. 여러 CAPA를 동시에 HARD로 적용한다는 **H-01/N-01 결정이 이뤄진 경우**의 확인용이며 이번에 그 방침을 확정하는 것은 아니다.

| 사실/후보 | 값 |
|---|---|
| P1·P2의 개별 월 수량 상한 | 각각 70 EA |
| 공유 설비 M1의 월 시간 상한 | 100 MACHINE_HOUR, 입력 단위 h |
| 두 제품의 같은 설비 공정 | 각각 1 EA당 1 MACHINE_HOUR |
| 인력 풀 C1의 상한/소비 | 90 PERSON_HOUR, 제품별 0.5 PERSON_HOUR/EA |
| 후보 생산 | P1=60 EA, P2=60 EA |

개별 수량은 60≤70인 두 건이다. M1은 60+60=120h로 100h를 초과한다. C1은 30+30=60인시로 90인시 이내다. **수량 두 건·설비 한 건·인력 한 건의 평가를 각각 보존**하고 M1에 대한 P1=60/P2=60의 기여를 표시한다.

M1을 제품마다 100개까지라고 따로 환산해 검사하면 공유하는 같은 100h를 두 번 할당하게 된다. 반대로 인시와 설비시간 중 작은 숫자만 남겨도 원인을 설명할 수 없다. 전체 결합 정책이 미확정이면 개별 사실 평가를 제시하더라도 최종 FEASIBLE을 내리지 않는다.

## 11. 확인 사례와 구현 시작 조건

계약 확정 후의 테스트 후보다. 이번에는 구현·실행하지 않는다.

| 사례 | 보존할 결과/확인할 점 |
|---|---|
| 위 공유 설비 예 | 수량 상한과 공유 설비 부하를 모두 보존. 확정된 강도/결합 규칙에 따른 판정 |
| 수량만/시간만/복수 선택 | 미선택·선택·부족·미지원을 구별. 선택을 조용히 누락하지 않음 |
| h/min, kg/g, 의미가 다른h | 같은 차원만 환산. 인시/설비시간 혼동과 계수 이중 적용 거부 |
| 휴일/명시0/기간 행 부재/겹친 휴지 | 0·결측·중복 차감을 구별하고 미합의 평일 보충을 하지 않음 |
| M1/M2 대체, 설비+인력 동시 요구 | 선택 후보와 공유 사용량·동시성 검증 범위를 추적 |
| BOM 월중 변경, 재고 불명/오래됨, 기본 안전재고0 | 적용 근거·결측·명시0 추적. 미합의 보충 없음 |
| PARTIAL 예측/기간 밖 요청/실행ID 불일치 | 대상 범위 누락을 잡고 다른 실행이나0 수요로 채우지 않음 |
| 비용 결측, 통화/시간 기준 불일치 | KPI는 미산출. 목적의 전제가 없는 안을 만들 수 있는지는 결정 사항 |
| HARD 위반과 다른 미확인 동시 존재 | 두 사유 모두 보존. 확정한 집계 규칙으로 판정 |
| 현행 export/프론트 투영 | 추가 필드422, nullable, 평가ID, 표시되지 않는 KPI/자원 항목의 호환 검사 |

구현 시작 전 H-01/H-04/H-07/H-08/H-09/H-10과 N-01/N-02/N-03/N-04/N-06의 결정 기록, 대상 기업의 익명화 표본, 단위·시간 단위별 손계산 예, 선택/미선택/불명 UI안을 준비한다. 미응답 의미를 상수나 기본값으로 넣지 않는다.

이후 순서 제안은 결정 기록 승인 → 논리 계약/기계 Schema 버전 확정 → 정규화·검증 → 승인 게이트를 갖춘 데이터 반입 → 계획 순수 함수·독립 계산 검증 → 저장/API → 화면 → 정식 계획 승인·출력이다. 신규 DDL/API/엔진 작업은 이번 문서 작성 범위 밖이다.
