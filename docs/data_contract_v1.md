# 데이터 계약 v1 (2026-10-30)

> 상태: **초안 v1.0** — 예측 담당·업무 규칙/UX 담당 확인 전. 기계 판독본: [contracts/data_contract_v1.schema.json](../contracts/data_contract_v1.schema.json) (코드 [backend/app/contracts.py](../backend/app/contracts.py)에서 생성, 테스트가 동기화 검사). DB 구현: [backend/app/models.py](../backend/app/models.py), 마이그레이션 `0001`.
> 책임: 데이터 처리·검증·DB·API = 백엔드/데이터 담당. 예측 필드 의미 = 예측·최적화 담당. 업무 규칙·KPI = 업무 규칙/UX 담당. 아래 ⚠는 담당자 확인이 필요한 가정이다.

## 1. 공통 규칙

| 항목 | 규칙 |
|---|---|
| 식별자 | `^[A-Za-z0-9][A-Za-z0-9_.-]*$`, 1~40자. 대소문자 구분. 앞뒤 공백 제거 후 비교. 제품(`product_id`)·자재(`material_id`)·BOM(`bom_id`)·자원(`resource_id`)은 각자 별도 공간(같은 코드가 제품과 자재에 모두 있을 수 있음) |
| 기간 | 월은 문자열 `YYYY-MM` (예 `2026-11`). 날짜는 ISO `YYYY-MM-DD`. 월 경계에 시간대 없음 |
| 수량 | 10진수(`NUMERIC(18,4)`). 쉼표·단위 문자열·NaN·Infinity·지수 표기 거부 — **쉼표/한글 단위 정규화는 파서 단계 책임**이고 계약은 정규화 이후 값만 받는다 |
| 단위 | `unit` 테이블의 `unit_code`만 허용. 기본 시드: `EA`(COUNT) `kg`·`g`·`t`(MASS) `h`·`min`(TIME). 행의 `unit`이 품목의 `base_unit`과 다르면 같은 dimension일 때만 환산, 다르면 검증 ERROR(단위 불일치). v1은 환산 후 저장하지 않고 입력 단위를 보존 |
| 출처 | 모든 업무 행은 `batch_id`로 `import_batch.source_type`에 연결: `PUBLIC_DEMAND`(공개 수요 데이터, 원 출처·라이선스를 `source_note`에 기록) / `SYNTHETIC`(합성 BOM·재고·생산능력) / `CLIENT_UPLOAD`(수요기업 실제 파일). 예측 결과에는 어떤 출처가 섞였는지 `v_forecast_input_monthly_demand.source_type`으로 추적 |

## 2. 예측 모듈 입력·출력 (예측·최적화 담당)

**입력 — 제품별 월별 수요** (`monthly_demand`, 예측 모듈은 뷰 `v_forecast_input_monthly_demand`만 읽는다)

| 필드 | 타입 | 규칙 |
|---|---|---|
| `product_id` | string | `product`에 존재 |
| `period` | `YYYY-MM` | (product_id, period) 유일 |
| `demand_qty` | decimal ≥ 0 | |
| `unit` | string | `unit`에 존재 |

**출력 — 예측** (`demand_forecast`)

| 필드 | 타입 | 규칙 |
|---|---|---|
| `product_id`, `target_month`(`YYYY-MM`) | | |
| `horizon` | int 1~36 | ⚠ "trained_until 다음 달 = 1"로 가정. 희원 모듈의 정의 확인 필요 |
| `p50`, `p80`, `p90` | decimal ≥ 0 | **p50 ≤ p80 ≤ p90** (DB CHECK + 계약 검증) |
| `p50_method` | string ≤60 | ⚠ 값 목록 미정(열린 문자열). 확정되면 열거형으로 |
| `quantile_model_version` | string ≤60 | |
| `trained_until` | date | 학습에 쓴 마지막 데이터 기준일 |

유일 키: (product_id, target_month, horizon, quantile_model_version, trained_until) — 같은 모델 실행을 두 번 저장하지 않는다. 모델 버전이 다르면 공존한다.

## 3. 업무 데이터 최소 모델

| 테이블 | 키 | 핵심 필드 / 제약 |
|---|---|---|
| `product` | product_id | product_name, product_type(FINISHED/SEMI_FINISHED), base_unit, safety_stock_qty ≥ 0 |
| `material` | material_id | material_name, base_unit, safety_stock_qty ≥ 0 |
| `sales_order` | order_id | product_id, order_qty > 0, unit, order_date, due_date ≥ order_date, status(OPEN/CLOSED/CANCELLED) |
| `bom_header` | bom_id; (product_id, bom_version) 유일 | status(DRAFT/APPROVED), is_active, valid_from/valid_to, base_qty > 0. **활성 버전은 같은 제품에서 유효기간이 겹칠 수 없음**(PostgreSQL EXCLUDE), 미승인 BOM은 활성 불가 |
| `bom_line` | (bom_id, line_no) | component_type(MATERIAL/PRODUCT)에 따라 `component_material_id` 또는 `component_product_id` 정확히 하나, qty_per > 0, unit, scrap_rate [0,1). 계약에서는 `component_id` 한 필드이고 저장 시 두 열로 갈린다 |
| `inventory` | (item_type, item_id, as_of_date) | on_hand_qty ≥ allocated_qty ≥ 0, unit. item_id는 조건부 참조라 FK 없이 검증 단계에서 확인 |
| `production_capacity` | resource_id | regular_hours_per_day, overtime_hours_per_day_max, 합 ≤ 24 (PoC E01 단위: 시간/일) |

v1에서 **제외**: 라우팅(제품 1개당 소요시간), 공급사·리드타임, 비용. 라우팅이 없으면 시간 기준 능력을 수량 기준으로 환산할 수 없다(PoC E11도 같은 미정 사항) → 예측 담당·업무 규칙 담당과 환산 방식 합의 필요(⚠ 다음 계약 v1.1).

## 4. 승인·검증 게이트 (예측 모듈에 나쁜 데이터를 넘기지 않는 설계)

배치 상태: `UPLOADED → VALIDATED → APPROVED` 또는 `REJECTED`.

1. 업무 테이블에 행을 쓰려면 그 행의 `batch_id`가 **APPROVED** 여야 한다 — 애플리케이션 검증 + DB 트리거(`require_approved_batch`) 이중 장치. (PoC E10: 한쪽만으로는 뚫린 경로가 실제로 있었음.)
2. APPROVED 배치는 `blocking_error_count = 0`, `approved_by`·`approved_at` 필수(CHECK). 승인 후 상태·출처·해시는 변경 불가(트리거).
3. 예측 모듈은 뷰 `v_forecast_input_monthly_demand`만 읽는다: 승인되고 오류 0인 배치의 행만 노출.
4. **중복 업로드**: `source_sha256`이 같은 비반려 배치가 있으면 재업로드 거부(부분 유니크 인덱스). 반려 후에는 재업로드 가능.
5. 승인자는 역할 코드(`approved_by`)만 기록, 실명 금지.

## 5. 충돌·변경 처리 정책 (v1은 규칙만, 구현은 업로드 API 단계)

| 상황 | 정책 |
|---|---|
| 같은 코드·다른 품명, 같은 품명·다른 코드 | 검증 ERROR(충돌). 자동 덮어쓰기 금지, 승인자가 원본 확인 후 재반입 (PoC M-24, E10 M13 교훈) |
| 코드 한쪽만 맞고 품명이 다른 단일 근거 오기 | 규칙으로 못 잡음 → 자동 승인 기본 OFF, 모든 배치 사용자 승인 필수 |
| 단위 불일치 | 알 수 없는 단위 ERROR, 같은 dimension 다른 단위는 환산 경고, dimension 불일치 ERROR |
| BOM 버전 변경 | 새 `bom_version` 행으로 추가, 기존 활성 버전의 `valid_to`를 먼저 닫아야 함(겹침 EXCLUDE). 구버전 입력 덮어쓰기 금지 |
| 동일 키 재반입(다른 파일) | v1 기본키 충돌로 거부. 갱신 시맨틱(UPDATE 승인)은 업로드 API 단계에서 정의 |

## 6. 미확정 (담당자 확인 필요)

- ⚠ 예측 담당: horizon 정의, p50_method 값 목록, 입력 최소 월 수·결측월 처리(0 채움 여부)
- ⚠ 업무 규칙/UX 담당: 승인 권한·1인/2인 승인, 필수 납기·업무 제약, 안전재고 포함 여부 KPI
- 라우팅·공급사·비용 모델 (v1.1)
- 공개 수요 데이터셋의 선택과 라이선스 (검색기 담당·백엔드 담당 협의)
