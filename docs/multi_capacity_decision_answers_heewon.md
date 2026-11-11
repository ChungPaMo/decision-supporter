# 다중 CAPA 결정 답변서 — 이희원 (H-01 ~ H-12)

작성: 2026-11-11 · 상태: **계산 기준 확정안 (이희원) / 대영·마나미 확인 전** · 규칙 버전: `plan-rules-0.3-draft`
대응 문서: [질문지](multi_capacity_decision_questions.md) · [다중 CAPA 계약 초안](multi_capacity_contract_draft.md) · [엔진 계약 v0.2](engine_forecast_plan_contract.md)

이 문서는 질문지 H-01~H-12에 대한 계산 관점의 답이다. 필드 이름은 다중 CAPA 계약 초안의 이름을 그대로 쓴다. 숫자 예시는 모두 합성 값이다.

**읽는 법**

- 모든 항목은 계산 담당이 정한 기준이다. 대영 확인 후 확정한다.
- 정마나미의 N 항목과 겹치는 부분(H-01, H-07~H-11)도 **계산 기준은 여기서 정했다.** 이의가 있으면 알려 주면 해당 항목만 조정한다. 택하지 않은 대안은 "검토한 대안"으로 남겼다.
- 화면 문구·표시 순서·승인 권한처럼 계산과 무관한 것은 정하지 않고 정마나미 몫으로 남겼다(§3).
- 계획안 3종의 근거는 과업지시서(과제 7)의 "예측결과와 간단한 규칙·제약조건을 결합하여 기준형, 안정형, 비용절감형 등 2~3개의 계획대안을 생성"이다.

---

## 0. MVP 지원 범위 요약

엔진의 MVP 역할은 **"월 단위로 합계를 내서 한도와 비교하는 검증"**이다. 일정을 짜거나 자원을 자동으로 나눠 주지 않는다.

| 구분 | 내용 |
|---|---|
| 지원 | 월 단위 집계 검증 |
| 지원 | 제약 3종: 월 수량 한도 / 인시 / 설비시간. 선택한 것은 **모두** 만족해야 함(AND) |
| 지원 | 순차 공정(공정을 차례로 거치는 라우팅). 공정별 부하를 자원별로 합산 |
| 지원 | 공유 자원: 여러 제품의 사용량을 자원별로 합산해 한도와 한 번 비교 |
| 지원 | 월 가용시간: 사용자가 월별로 명시 입력한 값(`EXPLICIT_PERIOD`) |
| 지원 | 수량 한도 대상: 제품 하나(`PRODUCT`), 같은 단위 제품 묶음의 합계(`PRODUCT_SET`) |
| 미지원 | 일·교대별 동시성, 공정 선후 일정 검증 |
| 미지원 | 대체 설비 자동 선택, 물량 분할 배분 |
| 미지원 | 셋업 순서·횟수 최적화 |
| 미지원 | 병렬 공정, 재작업, 배치 생산 |
| 미지원 | 달력에서 가용시간 자동 계산(`DERIVE_CALENDAR`) |
| 미지원 | 등가 생산량 환산(`EQUIVALENT_OUTPUT`), 공정 통과량 한도(`OPERATION_OUTPUT`) |

미지원 패턴이 입력에 있으면 조용히 무시하지 않는다. 해당 평가 행을 `application_state = UNSUPPORTED`로 남기고, 그 계획안은 `FEASIBLE`이 될 수 없다(§H-10).

**핵심 원칙: 줄이지 않고 위반을 보고한다.** 한도가 모자라도 엔진은 생산량을 자동으로 줄이거나 제품끼리 나누지 않는다. 목표 생산량 그대로 사용량을 합산해 `VIOLATED`로 판정하고, 초과량과 제품별 기여량을 보여 준다. 무엇을 줄일지는 사람이 정한다.

### 기존 엔진 계약 v0.2에서 바뀌는 것

| 기존 (v0.2) | 변경 | 이유 |
|---|---|---|
| "월 N개만 알면 `run_time = 월 정규시간 ÷ N`으로 환산" (§2) | **폐기.** 수량 한도는 수량 그대로 검사 | 제품이 섞이거나 설비를 나눠 쓰면 가상 소요시간이 틀린 답을 만든다 |
| "근무일 없으면 평일 수로 계산 + 경고" (§4, §5) | **폐기.** 월 가용시간은 명시 입력만. 없으면 미확인 | 추정 달력으로 FEASIBLE을 내면 안 된다 |
| "한 자원을 여러 제품이 쓰면 product_id 순 배분" (§6.2-2) | **폐기.** 합산 후 위반 보고 | 코드 순서는 업무 근거가 없다 |
| `prod_m = min(목표, 능력 상한)` 으로 생산을 깎고 결품 계산 (§6.2-2) | **폐기.** `prod_m = 목표`로 두고 능력 제약을 `VIOLATED`로 보고 | 위 원칙 |
| `capacity[]`의 시간/일 × 근무일 | `hours_budgets[]`의 월 정규시간·잔업 한도 | 계약 초안 §5.3 |
| `routings[] {product, resource, run_time_per_unit_h}` | `routing_steps` + `routing_resource_requirements` | 인시와 설비시간을 구분 |

§7의 예시 6건(결품 27·56·144 EA 등)은 옛 규칙의 결과이므로 계획 엔진 구현 때 새 규칙으로 다시 만든다.

---

## 1. 공통 기호와 제약식

| 기호 | 의미 |
|---|---|
| `m` | 계획 월 (YYYY-MM) |
| `prod(p, m)` | 제품 p의 m월 계획 생산량. 양품 완제품(또는 반제품) 수량, 품목 기준 단위 |
| `ref(p, s)` | 라우팅 공정 s의 기준 수량 `reference_qty` |
| `rt(p, s, r)` | 기준 수량을 처리하는 데 자원 r이 쓰는 시간 `run_time` (h로 환산) |
| `k(p, s, r)` | 투입 수 계수. `usage_time_basis = ELAPSED`이면 `resource_units`, 그 밖이면 1 |
| `REG(r, m)`, `OT(r, m)` | 자원 r의 m월 정규시간, 잔업 한도 (`hours_budgets`) |
| `QL(c, m)` | 수량 한도 c의 m월 한도 (`quantity_limits.limit_qty`) |

**(1) 생산량** — 엔진 계약 v0.2 §6.2-1 유지, 능력에 맞춰 깎지 않음

```
need(p, m) = max(0, ⌈ d(p, m) + α·SS(p) − I(p, m−1) ⌉)    # EA는 정수 올림. α = 1(기준형·안정형), 0(비용절감형, §H-08)
prod(p, m) = need(p, m)
I(p, m)    = I(p, m−1) + prod(p, m) − d(p, m)
```

**(2) 자원 부하** — 인시·설비시간 공통

```
load(r, m) = Σ_{p, s}  prod(p, m) ÷ ref(p, s) × rt(p, s, r) × k(p, s, r)  [+ 셋업, §H-04]
```

**(3) 시간 제약** — 자원 r, 월 m마다 한 행

```
limit(r, m) = REG(r, m) + ( 그 안이 잔업 허용 ? OT(r, m) : 0 )
SATISFIED  ⇔  load(r, m) ≤ limit(r, m)
```

**(4) 수량 제약** — 한도 c, 월 m마다 한 행

```
used(c, m) = Σ_{p ∈ scope(c)} prod(p, m)
SATISFIED  ⇔  used(c, m) ≤ QL(c, m)
```

**(5) 그룹(작업장·풀) 제약** — 그룹 g에 한도가 있을 때

```
load(g, m) = Σ_{r ∈ members(g)} load(r, m) + (g에 직접 걸린 부하)
SATISFIED  ⇔  load(g, m) ≤ limit(g, m)
```

비교는 모두 `≤`이고 같으면 충족이다. 허용 오차는 0이다(§H-04 반올림).

---

## H-01. 여러 CAPA를 동시에 고를 때 — 계산 기준 확정안 (관련: N-01)

**결정: `combination_mode = ALL_SELECTED`** (정책 ID `CAP-ALL-SELECTED`, 버전 `0.1-draft`)

- 선택한 제약마다 평가 행을 따로 만든다. 수량·인시·설비시간을 하나의 숫자로 합치거나 `min()`으로 줄이지 않는다.
- 선택한 CAPA 제약은 모두 HARD로 본다. 하나라도 `VIOLATED`면 그 안은 `INFEASIBLE`이다.
- 선택하지 않은 제약은 `application_state = NOT_SELECTED`로 응답에 남긴다. `SATISFIED`를 붙이지 않고 판정에도 넣지 않는다.
- 응답에 `verification_scope`를 넣어 "무엇을 검사했고 무엇을 안 했는지"를 항상 함께 낸다.
- 조합별 별도 프로필(`SEPARATE_PROFILES`)과 우선순위 완화는 MVP에서 계산하지 않는다. 여러 조합을 비교하고 싶으면 선택을 바꿔 요청을 여러 번 보낸다(같은 `execution_id`).
- **라우팅의 `CHOOSE_ONE`(M1 또는 M2)은 전역 CAPA 선택과 다른 규칙이다.** 전자는 "이 공정을 어느 설비에서 하나"이고 후자는 "어떤 종류의 한도를 검사하나"다. 서로 영향을 주지 않는다. 대체 설비 처리는 §H-05.

**2제품 / 1공유 설비 예** (계약 초안 §10의 값, 2026-12, 잔업 불가 안)

| 사실 | 값 |
|---|---|
| Q-P1, Q-P2 (제품별 월 수량 한도) | 각 70 EA |
| M1 (공유 설비) 정규시간 | 100 MACHINE_HOUR |
| C1 (인력 풀) 정규시간 | 90 PERSON_HOUR |
| 소요 | 두 제품 모두 1 EA당 M1 1 설비시간, C1 0.5 인시 |
| 생산량 | P1 60 EA, P2 60 EA |

평가 행(선택했을 때의 결과):

| 평가 | 사용 | 한도 | 결과 |
|---|---|---|---|
| Q-P1 | 60 EA | 70 EA | SATISFIED |
| Q-P2 | 60 EA | 70 EA | SATISFIED |
| M1 | 60 + 60 = 120 h | 100 h | **VIOLATED** (초과 20 h) |
| C1 | 30 + 30 = 60 인시 | 90 인시 | SATISFIED |

선택 조합별 최종 판정(자재 등 다른 제약은 모두 충족이라고 가정):

| 선택 | 판정 | 검사하지 않은 것 |
|---|---|---|
| 수량만 | FEASIBLE | 인시, 설비시간 |
| 인시만 | FEASIBLE | 수량, 설비시간 |
| 설비시간만 | INFEASIBLE (M1) | 수량, 인시 |
| 수량 + 인시 | FEASIBLE | 설비시간 |
| 수량 + 설비시간 | INFEASIBLE (M1) | 인시 |
| 인시 + 설비시간 | INFEASIBLE (M1) | 수량 |
| 3종 모두 | INFEASIBLE (M1) | 없음 |
| 아무것도 선택 안 함 | **UNVERIFIED** (`NO_CAPACITY_CHECK`) | 전부 |

**정한 것 (N-01 관련).** 화면에서 검사 범위를 어떤 문구로 보여 줄지는 정마나미 몫이다.

| 쟁점 | 검토한 값 | 결정 |
|---|---|---|
| 결합 방식 | (가) 선택한 것 모두 AND (나) 조합별 프로필 (다) 우선순위 완화 | (가). (나)는 요청을 여러 번 보내면 같은 효과. (다)는 완화 기준이 필요해 MVP 밖 |
| 필수 제약 | (가) 최소 1종은 선택해야 함 (나) 특정 종류는 항상 필수 (다) 제한 없음 | (가). 0종 선택은 계산은 하되 UNVERIFIED |
| "수량만 선택 → FEASIBLE" 표시 | (가) FEASIBLE + 검사 범위 표시 (나) 3종을 다 검사해야 FEASIBLE | (가). 단, 화면에서 범위가 안 보이면 오해 소지가 있어 문구는 N-01·N-07 |

---

## H-02. 가용시간·달력·잔업·손실의 권위 출처 — 계산 기준 확정안

- **권위 출처는 `hours_budgets`의 `EXPLICIT_PERIOD` 한 가지다.** `DERIVE_CALENDAR` 행은 `UNSUPPORTED`로 평가한다.
- 같은 자원·월에 한도 행이 2개 이상이면 입력 오류다(`HOURS_BUDGET_DUPLICATE`). 작은 값·큰 값을 엔진이 고르지 않는다. 명시 값과 달력 값은 더하지 않는다.
- 기존 `calendar.working_days`와 "평일 수 추정"은 쓰지 않는다. 한도 행이 없는 월은 **미확인**이다(§H-10).
- **정규와 잔업 두 칸만 구분한다.** 휴일 근무·휴일 잔업은 사용자가 `overtime_hours_limit`에 넣어 입력한다. 엔진은 휴일을 따로 계산하지 않는다.
- **잔업 한도가 null일 때**: 부하가 정규시간 이하면 `SATISFIED`(잔업이 필요 없음). 정규시간을 넘는데 그 안이 잔업을 허용하면 `UNVERIFIED`(`OVERTIME_LIMIT_MISSING`). 0으로도 무제한으로도 보지 않는다.
- **손실은 엔진이 추가로 곱하지 않는다.** 입력 값을 그대로 쓴다.
  - 속도가 `RATED`이고 `efficiency_factor`가 있으면 실효 속도 = 정격 × 효율로 한 번만 적용한다.
  - 속도가 `EFFECTIVE`인데 `efficiency_factor`도 있으면 입력 오류다(`LOSS_DOUBLE_COUNT`).
  - 효율이 null이면 1로 가정하지 않고 "손실 미반영"으로 기록한다(`LOSS_NOT_APPLIED` 경고).
  - `availability_basis = GROSS`이면 손실이 빠지지 않은 한도임을 평가 행 note에 남기고 경고(`GROSS_AVAILABILITY`)를 낸다. 판정은 낮추지 않는다(가능 + 경고). 낮추면 대부분의 기업 자료가 전부 UNVERIFIED가 되기 때문이다.

**달력 손계산 예 (참고용 — MVP 엔진은 계산하지 않음)**

MVP에서는 사용자가 아래처럼 계산한 월 합계를 입력한다. 나중에 `DERIVE_CALENDAR`를 지원할 때의 정답 기준으로도 쓴다. 규칙은 "근무 구간에서 휴게를 빼고, 정지 구간은 **합집합**으로 한 번만 뺀다"이다.

| 날 | 근무·정지 | 계산 | 가용 |
|---|---|---|---|
| 정상일 | 08:00–17:00, 휴게 12:00–13:00 | 9 − 1 | 8 h |
| 휴일(명시 행) | 근무 구간 없음 | — | 0 h |
| 익일 종료 교대 | 22:00–익일 06:00, 휴게 02:00–02:30 | 8 − 0.5 | 7.5 h |
| 겹치는 정지 | 정상일 + 보전 10:00–12:30 + 고장 11:30–14:00 | 정지 합집합 10:00–14:00. 근무와 겹치는 부분은 10–12(2 h), 13–14(1 h) = 3 h. 휴게와 겹친 12–13은 이미 뺀 시간이라 다시 빼지 않음. 8 − 3 | 5 h |

- 설비가 2대인 풀에서 1대만 위 정지를 겪으면 8 × 2 − 3 × 1 = 13 설비시간이다.
- 교대가 월을 넘으면(말일 22:00 시작) 자정에서 나눠 각 월에 넣는다(위 예: 앞 달 2 h, 다음 달 5.5 h).
- 날짜 행이 아예 없는 날은 휴일이 아니라 "미확인"이다.

---

## H-03. 인시·설비시간과 검증 시간 단위 — 계산 기준 확정안

**세 가지 시간의 뜻** (`time_basis`)

| 값 | 뜻 | 예: 작업자 3명, 설비 2대가 2시간 작업 |
|---|---|---|
| `ELAPSED` | 시계로 흐른 시간 | 2 h |
| `PERSON_HOUR` | 사람 수 × 시간 | 6 인시 |
| `MACHINE_HOUR` | 설비 대수 × 시간 | 4 설비시간 |

**입력 규칙 (이중 곱 방지)**

| 소요시간 입력 방식 | `resource_units` | 엔진 계산 |
|---|---|---|
| `usage_time_basis = ELAPSED` | **필수** | 시간 × `resource_units` |
| `PERSON_HOUR` 또는 `MACHINE_HOUR` (이미 집계된 값) | **입력 금지** | 그대로 사용. `resource_units`가 같이 오면 입력 오류 `RESOURCE_UNITS_WITH_AGGREGATED_TIME` |

- 자원의 `time_basis`와 변환 후 소요의 의미가 달라야 할 이유가 없다. 인력 풀(`PERSON_HOUR`)에 설비시간 소요를 걸면 입력 오류다(`TIME_BASIS_MISMATCH`).
- `PERSON_HOUR`와 `MACHINE_HOUR`는 서로 환산하지 않고 더하지도 않는다. h↔min만 환산한다(÷60).
- 정적 검사 하나는 월 집계에서도 한다: `resource_units > 자원의 parallel_units`이면 그 공정은 어떤 일정으로도 불가능하므로 `VIOLATED`(`UNITS_EXCEED_POOL`).

**검증 범위 (`verification_scope`)**

| 검사함 | 검사 안 함 (응답에 `not_verified[]`로 명시) |
|---|---|
| 월별 자원 부하 합계 ≤ 월 한도 | 일·교대별 동시 사용 |
| 월별 수량 합계 ≤ 월 한도 | 공정 선후 관계에 따른 실제 완료 시점 |
| 투입 수 ≤ 풀 크기 (정적) | 월 안의 납기일, 셋업 순서 |

MVP의 `FEASIBLE`은 **"월 집계 기준으로 선택한 한도를 넘지 않는다"**는 뜻이다. "이 일정대로 실행할 수 있다"는 뜻이 아니다. 응답의 `verification_scope.time_grain = "MONTH"`로 고정해 낸다.

**동시성 반례 (월 합계는 맞지만 실제로는 불가능)**

- P1의 공정 10(M1) → 공정 20(M2), 각 1 h/EA. M1·M2 모두 월 160 h. 생산 160 EA.
- 월 집계: M1 160 ≤ 160, M2 160 ≤ 160 → 둘 다 SATISFIED.
- 실제: M2는 M1이 끝낸 것만 작업할 수 있다. M1이 마지막 1개를 월말 160시간째에 끝내면 M2는 그 뒤 1 h가 더 필요하다. 월 안에 끝나지 않는다.
- MVP 판정: `FEASIBLE` + `not_verified = ["OPERATION_SEQUENCE", "INTRA_MONTH_CONCURRENCY"]`. 이 한계를 화면에 어떻게 알릴지는 N-07.

---

## H-04. 공정·라우팅·속도·셋업의 최소 계산 범위 — 계산 기준 확정안

**지원 조합**

| 패턴 | MVP | 처리 |
|---|---|---|
| 순차 공정 (선행 0~1개) | 지원 | 공정별 부하를 자원별로 합산. 순서는 부하 계산에 영향 없음 |
| 한 공정에 설비 + 인력 동시 (`ALL_REQUIRED`) | 지원 | 요구마다 부하를 따로 계산해 각 자원에 더함 |
| 대체 설비 (`CHOOSE_ONE`) | **지정한 경우만** | §H-05 |
| 병렬 공정 (선행이 2개 이상이거나 갈라짐) | 미지원 | 해당 제품이 쓰는 자원 평가 `UNSUPPORTED` |
| 분할 생산, 재작업, 배치 생산 | 미지원 | 〃 |
| 반제품 공정 | 지원 | 반제품도 자기 라우팅을 가짐. 종속수요에서 나온 생산량으로 부하 계산 |

**자원 소비 계수**

- `run_time`은 **`reference_qty`만큼 처리하는 시간**이다. 1개당이 아니다. 계수 = `run_time ÷ reference_qty`.
- 속도 참조(`EQUIPMENT_RATE_REF`)일 때 필요한 설비시간:
  - `PER_RESOURCE_UNIT`(설비 한 대의 속도): `prod × per_time ÷ output_qty`
  - `TOTAL_RESOURCE_POOL`(풀 전체가 같이 돌 때의 속도): `prod × per_time ÷ output_qty × parallel_units`
- inline `run_time`과 `rate_id`가 같이 오면 입력 오류다(`USAGE_MODEL_CONFLICT`). 하나를 고르지 않는다.

**셋업**

- 순서·횟수 최적화는 하지 않는다. 대신 **월 셋업 횟수를 명시 입력**받는다(신규 필드 제안 `setups_per_period`, 정수 ≥ 0, 제품·공정·자원·월별).
- 부하에 `setup_time × setups_per_period`를 더한다.
- `setup_time > 0`인데 횟수가 없으면 그 자원 평가는 `UNVERIFIED`(`SETUP_COUNT_MISSING`).
- `setup_time`이 null이면 셋업을 빼고 계산하고 `not_verified`에 `SETUP`을 넣는다. 0으로 확인된 것과 구분한다.

**버전 선택**

- 라우팅·속도는 **그 달 1일에 유효한 버전이 정확히 1개**일 때만 쓴다(BOM과 같은 규칙). 0개면 `ROUTING_MISSING`, 2개 이상이면 `ROUTING_VERSION_AMBIGUOUS`.
- 월 중간에 버전이 바뀌어도 월을 나누지 않는다. 1일 기준 버전을 쓰고 `ROUTING_CHANGES_MID_MONTH` 경고를 낸다.

**반올림**

| 단계 | 규칙 |
|---|---|
| 입력 | 10진 문자열 그대로. 엔진이 고쳐 쓰지 않음 |
| 생산량 | 정수 단위(EA)는 `need`에서 올림(⌈ ⌉). 그 밖은 소수 4자리 |
| 부하 계산 | 반올림 없이 분수로 정확히 계산 |
| 판정 | 반올림 전 값으로 `≤` 비교. 허용 오차 0 |
| 표시 | 시간·수량 소수 4자리 `ROUND_HALF_UP`. 표시 값으로 다시 판정하지 않음 |

경계 예: "3 EA당 1 h"인 공정으로 300 EA → 정확히 100 h → 한도 100 h에 `SATISFIED`. 이것을 1개당 0.3333 h로 미리 반올림하면 99.99 h, 0.3334 h면 100.02 h(`VIOLATED`)가 된다. 그래서 `reference_qty`를 유지한다.

---

## H-05. 공유 설비·자원 풀·상하위 한도 — 계산 기준 확정안

- **부하는 각각 검사하고 가용량은 더하지 않는다.** 설비 M1의 부하는 M1 한도와 비교하고, M1이 속한 작업장 WC1의 한도와도 비교한다. WC1의 한도에 M1·M2의 시간을 더해 넣지 않는다.
- 같은 설비가 두 그룹에 속하면 그 부하는 두 그룹 평가에 한 번씩 들어간다. 같은 그룹에 같은 설비가 두 번 등록되면 입력 오류다(`MEMBER_DUPLICATE`).
- 그룹 안의 그룹(중첩)은 MVP에서 1단계만 지원한다. 2단계 이상은 `UNSUPPORTED`.
- **자원 배분은 하지 않는다.** `product_id` 순 배분은 폐기한다. 한도를 넘으면 `VIOLATED`이고, `contributions[]`에 제품·공정별 사용량을 모두 남겨 원인을 보여 준다.
- **대체 설비**: 자동 선택을 하지 않는다.
  - 후보가 1개이거나, 요청에서 제품·공정마다 설비 하나를 지정(`assigned_resource_id`, 신규 필드 제안)하면 그 설비에 전량을 싣는다.
  - 후보가 2개 이상인데 지정이 없으면 후보 설비들의 평가는 `UNSUPPORTED`(`ALTERNATIVE_NOT_ASSIGNED`).
  - 한 공정의 물량을 두 설비에 나누는 것은 미지원이다.

**상하위 한도 예** (2026-12, 잔업 불가)

| 자원 | 한도 | 부하 | 결과 |
|---|---|---|---|
| M1 (설비) | 100 h | P1 60 h + P2 30 h = 90 h | SATISFIED |
| M2 (설비) | 100 h | P3 70 h | SATISFIED |
| WC1 (작업장, 구성원 M1·M2) | 150 h | 90 + 70 = 160 h | **VIOLATED** (초과 10 h) |

설비 하나씩은 통과하지만 작업장 한도를 넘는다. WC1 평가 행의 `contributions`는 P1 60 / P2 30 / P3 70이고 합이 `used_value` 160과 같아야 한다. 이 "기여 합 = 사용량" 대조는 엔진 테스트에서 항상 확인한다.

---

## H-06. 월 생산수량 한도의 대상과 합산 기준 — 계산 기준 확정안

- **세는 수량(`accounting_point`)**: MVP는 `FINISHED_OUTPUT`만 지원한다. 그 품목의 **계획 생산량(양품 수량)**을 센다. 공정 통과량(`OPERATION_OUTPUT`)은 `UNSUPPORTED`.
- 공정 수율은 MVP에서 다루지 않는다. 스크랩은 BOM의 `scrap_rate`(자재 소요)에만 반영된다.
- **대상(`scope_kind`)**

  | 값 | MVP | 규칙 |
  |---|---|---|
  | `PRODUCT` | 지원 | 제품 하나의 월 생산량 ≤ 한도 |
  | `PRODUCT_SET` + `SHARED_NATIVE_UNIT` | 지원 | 묶음 안 제품의 기준 단위가 모두 같을 때만 합산. 다르면 입력 오류 `SCOPE_UNIT_MIXED` |
  | `PRODUCT_SET` + `EQUIVALENT_OUTPUT` | 미지원 | 환산계수 제공 주체가 정해지지 않음 |
  | `RESOURCE_OUTPUT` | 미지원 | 필요하면 그 자원을 쓰는 제품을 `PRODUCT_SET`으로 명시해 입력 |

- "같은 EA"라는 이유로 엔진이 제품을 알아서 묶지 않는다. 묶음은 `scope_products[]`에 명시된 것만이다.
- **잔업 포함 여부(`includes_overtime`)**
  - `true`인 한도를 잔업 불가 안에 적용하면, 정규만으로 가능한 수량을 알 수 없다 → 그 평가는 `UNVERIFIED`(`QTY_LIMIT_INCLUDES_OVERTIME`).
  - `false`인 한도를 잔업 허용 안에 적용하면 그대로 검사한다(보수적). note에 "정규 기준 한도"라고 남긴다.
- **수량 한도와 시간 한도가 같은 사실을 이중으로 제한하는 경우**: 의도된 것으로 본다. 둘 다 선택했으면 둘 다 검사한다(AND). 하나로 합치지 않는다.
- **"시간 ÷ N" 가상 소요시간 생성: 채택하지 않는다.** 수량만 아는 기업은 수량 한도만 선택하면 되고, 그때 시간 제약은 `NOT_SELECTED`로 표시된다.

**묶음 예**: Q-SET-L1 = {P1, P2} 월 110 EA. P1 60 + P2 60 = 120 EA > 110 → `VIOLATED`(초과 10 EA). 제품별 한도 70은 둘 다 통과하지만 묶음 한도에 걸린다.

---

## H-07. 자재 부족·안전재고·기준일 — 계산 기준 확정안 (관련: N-02, N-03, N-08)

**계산식은 엔진 계약 v0.2 §6.2-3·4를 유지한다(제안).**

```
구성품 소요 = 상위 생산량 × qty_per ÷ base_qty × (1 + scrap_rate)     # 기준 단위로 환산
가용(i)     = on_hand − allocated                                      # as_of_date 기준
부족(i, m)  = max(0, 누적 소요(i, ~m) + α·SS(i) − 가용(i))          # α는 §1과 같음
```

- 다단 BOM은 저수준 코드 순으로 전개하고, 품목마다 수요를 합친 뒤 재고를 **한 번만** 뺀다.
- 공유 자재도 같은 방식이다. 제품끼리 자재를 나눠 주지 않고 합산 소요와 가용을 한 번 비교한다.
- BOM은 그 달 1일에 유효한 승인 BOM이 1개일 때만 쓴다. 월 중간 변경은 나누지 않고 경고(`BOM_CHANGES_MID_MONTH`).
- 단위는 같은 차원끼리만 환산한다.

**판정 — 결정: (가)**

| 상황 | (가) 채택 | (나) 검토한 대안: PoC T-01 방식 |
|---|---|---|
| 소요 + SS ≤ 가용 | SATISFIED | SATISFIED |
| 부족이 있고 입고예정·리드타임 자료가 없음 | **UNVERIFIED** + 조달 필요량 표시 | VIOLATED |
| 부족이 있고 "조달 불가"가 자료로 확인됨 | VIOLATED | VIOLATED |
| 재고 행 자체가 없음 | UNVERIFIED (`INVENTORY_MISSING`). 0 가정 금지 | 같음 |

- (가)의 이유: 지금 계약에는 입고예정·리드타임이 없다. 부족은 "조달해야 할 양"일 뿐이고 조달이 되는지는 엔진이 알 수 없다. 모르는 것을 위반으로 부르면 거의 모든 안이 INFEASIBLE이 된다.
- (가)의 약점: 조달 자료가 생기기 전에는 자재 때문에 `VIOLATED`가 나오는 경우가 없다. 자재가 부족한 안은 전부 UNVERIFIED에 머문다. 이 안을 승인할 수 있는지는 N-04다.
- **후속 제안(대영)**: 자재 업로드에 입고 예정(품목·수량·예정일)과 납품 기간(리드타임)을 추가하면 "입고분으로 채워짐 → SATISFIED", "지금 주문해도 못 맞춤 → VIOLATED"까지 판정할 수 있다. MVP 범위에는 넣지 않는다.
- 실시간 재고 관리는 범위 밖이다. 재고는 기준일의 값 하나를 쓰고, 최신 값은 다시 업로드받는다.

**안전재고 — 결정**

| 쟁점 | 검토한 값 | 결정 |
|---|---|---|
| 강도 | SOFT / HARD | SOFT. HARD로 하면 초기 재고가 낮은 달에 바로 INFEASIBLE |
| 적용 시점 | 월말 재고 기준 / 월중 최저 | 월말(월 단위 엔진이라 월중은 알 수 없음) |
| 생산 필요량에 포함 | 포함 / 제외 | 기준형·안정형은 포함, 비용절감형은 제외(§H-08) |
| 값의 근거를 모를 때(생략 기본값 0) | (가) 0으로 계산하고 SS 평가를 "근거 불명"으로 표시 (나) SS 평가 생략 | (가). 판정에는 영향 없음(SOFT) |

**기준일**: 재고는 `as_of_date` 값 하나로 계산한다. 기준일과 계획 시작일 사이가 벌어졌을 때 "오래됨"으로 볼 기준 일수는 엔진이 정하지 않는다(N-08). 기준이 정해지면 `INVENTORY_STALE` 경고에 쓴다.

**예**: P1은 M-A 2 kg/EA(스크랩 5%), P2는 M-A 1 kg/EA. 생산 60·60 EA.
소요 = 60 × 2 × 1.05 + 60 × 1 = 126 + 60 = 186 kg. 가용 = 200 − 20 = 180 kg. SS 10 kg.
부족 = 186 + 10 − 180 = **16 kg** → UNVERIFIED, 조달 필요 16 kg. (기준형 기준. 비용절감형은 SS를 넣지 않으므로 186 − 180 = 6 kg)

---

## H-08. 기준형·안정형·비용절감형의 목적함수 — 계산 기준 확정안 (관련: N-02)

**먼저 밝힐 점: MVP 엔진은 최적화를 하지 않는다.** 세 안은 "목적함수"가 아니라 **설정 묶음(프로필)**이다. 가중합도, 결품 페널티의 금액 환산도 없다.

| 축 | BASELINE 기준형 | STABLE 안정형 | COST_SAVING 비용절감형 |
|---|---|---|---|
| 수요 분위수 | P50 | P80 (설정으로 P90) | P50 |
| 잔업 | 허용 | 허용 | **불가** |
| 안전재고를 필요량에 포함 (제품·자재) | 포함 | 포함 | **제외** — 있는 재고를 먼저 쓴다 |
| 생산 방식 | 월 필요량만큼 | 월 필요량만큼 | 월 필요량만큼 |
| CAPA·자재 제약 | 세 안 공통 | 공통 | 공통 |

기준형과 안정형은 분위수만 다르다(v0.2 그대로. 안정형은 설정으로 P90 가능).

**결정: 비용절감형 = 잔업 불가 + 재고 활용.** "덜 만들고, 덜 사고, 잔업비를 쓰지 않는다"는 간단한 규칙이다. 이름은 과업지시서의 "비용절감형"을 그대로 쓴다.

- 생산 필요량과 자재 부족 계산에서 안전재고를 빼고(§1의 α = 0) 잔업 한도를 쓰지 않는다.
- 안전재고 평가(SOFT)는 그대로 해서 미달 수량을 경고로 보여 준다. 비용을 아끼는 대가가 화면에 보여야 하기 때문이다.

**세 안 비교 예** (제품 A·B 각각: P50 55, P80 65, 안전재고 10, 기초 가용 5 EA. M1 1 h/EA, 정규 100 h, 잔업 한도 20 h)

| 안 | 제품당 생산 | M1 부하 / 한도 | 판정 | 월말 재고 |
|---|---|---|---|---|
| 안정형 | 65 + 10 − 5 = 70 | 140 / 120 | INFEASIBLE (초과 20 h) | 10 (P80 실현 시) |
| 기준형 | 55 + 10 − 5 = 60 | 120 / 120 | FEASIBLE (잔업 20 h) | 10 |
| 비용절감형 | 55 − 5 = 50 | 100 / 100 | FEASIBLE (잔업 0) | 0 → 안전재고 미달 10 (SOFT 경고) |

**비용절감형 — 검토한 대안 (채택: C)**

| 안 | 정의 | 필요한 입력 | 계산 관점 의견 |
|---|---|---|---|
| A | 잔업 불가 + 정규시간 초과분을 앞 달로 선생산(평준화) | 없음 | 공유 자원에서는 "어느 제품을 앞당길지"가 다시 배분 문제가 된다. 폐기한 `product_id` 순 배분과 같은 문제라, A를 고르면 이동 규칙(비례 / 사용자 우선순위)을 추가로 정해야 한다 |
| A′ | 잔업 불가만. 생산은 월 필요량 그대로 | 없음 | 가장 단순하지만 기준형과 생산량이 같아 비교가 되지 않는다. 정규시간을 넘으면 그대로 `VIOLATED`로 보고. "줄이지 않고 보고" 원칙과 일치 |
| B | 재고 활용 최대·추가 조달 최소 (안전재고를 생산 필요량에서 제외) | 없음 | 계산은 쉽다. 생산·자재 소요가 줄지만 안전재고 미달이 늘어난다. 잔업과는 무관한 축 |
| **C (채택)** | A′ + B | 없음 | 생산량·재고·자재 소요·잔업이 기준형과 모두 달라져 안끼리 비교가 된다. 제품 간 배분 문제가 없다. 대가는 안전재고 미달 |

- A′만 택하지 않은 이유: 기준형과 생산량·재고·자재가 같아져, 과업지시서가 요구하는 "계획대안별 예상 생산량, 재고수준, 자재부족 비교"가 되지 않는다.
- A(선생산 평준화)는 이동 규칙이 정해져야 구현할 수 있어 후속으로 둔다. 비용 자료를 써서 가장 싼 안을 찾는 최적화도 후속이다.
- **비용 입력이 없을 때**: 안은 만든다. 프로필이 비용 값을 쓰지 않기 때문이다. 비용 KPI만 `null`이 된다.
- 비용 자료가 없으면 화면에서 "비용은 계산되지 않았고, 이 안은 잔업과 생산량을 줄이는 규칙으로 만든 안"임을 알려야 한다. 문구는 정마나미 몫이다.
- **"실행 가능한 계획대안 산출"(과업지시서)과의 관계**: 조건이 빡빡하면 세 안이 모두 INFEASIBLE일 수 있다. 엔진은 초과량과 제품별 내역을 보여 주고, 사용자가 시나리오 분석(조건 변경 후 재계산)으로 실행 가능한 안에 도달한다. 한도에 맞춰 같은 비율로 줄인 안을 엔진이 직접 만드는 기능은 후속 항목이다.
- 결품·비용·재고·잔업은 합치지 않고 KPI로 나란히 낸다(§H-09). 순위는 내지 않는다.

---

## H-09. KPI 산식과 안 간 비교 기준 — 계산 기준 확정안 (관련: N-06)

**KPI 사전 (제안, 계산 버전 `kpi-0.3-draft`)**

| code | 단위 | 산식 | 집계 범위 | 변경 |
|---|---|---|---|---|
| `TOTAL_PRODUCTION` | 품목 단위 | Σ 완제품 `prod` | 제품별. 합계는 단위가 같을 때만 | 유지 |
| `PLANNED_DEMAND` | 품목 단위 | Σ 그 안의 수요(분위수 × 배수) | 〃 | 유지. **안마다 기준이 달라 안 간 비교 불가** |
| `DEMAND_COVERAGE_P80` | % | `min(1, (I_0 + Σprod) ÷ Σ P80 수요) × 100` | 제품별 | 유지. 공통 분모라 비교 가능 |
| `END_INVENTORY_AT_P50` | 품목 단위 | P50 수요가 실현될 때 기간 말 재고 | 제품별 | 유지. 비교 가능 |
| `SAFETY_STOCK_SHORTFALL` | 품목 단위 | Σ_월 max(0, SS − 월말 재고) | 제품별 | 유지 |
| `OVERTIME_PERSON_HOURS` | 인시 | Σ max(0, 부하 − 정규) — 인시 자원만 | 자원별, 합계 | `OVERTIME_HOURS`를 분리 |
| `OVERTIME_MACHINE_HOURS` | 설비시간 | 〃 — 설비시간 자원만 | 자원별, 합계 | 〃 |
| `CAPACITY_EXCESS_PERSON_HOURS` / `_MACHINE_HOURS` | 인시 / 설비시간 | Σ max(0, 부하 − 한도) | 자원별, 합계 | **신규** |
| `QUANTITY_LIMIT_EXCESS` | 품목 단위 | Σ max(0, 사용 − 수량 한도) | 한도별 | **신규** |
| `MATERIAL_SHORTAGE_ITEMS` | 건 | 부족이 한 번이라도 난 **고유 자재 품목 수** | 기간 전체 | 정의 명시 |
| `MATERIAL_COST` 등 비용 5종 | 통화 | v0.2 §6.6과 같음. 정규·잔업 비용은 인시/설비시간별 단가로 따로 계산 | 통화별 | 유지 |
| ~~`STOCKOUT_QTY`~~ | — | — | — | **제외** (아래) |

- `STOCKOUT_QTY`: 새 규칙은 생산을 깎지 않으므로 계획상 결품이 생기지 않는다. 대신 "모자라는 정도"는 `CAPACITY_EXCESS_*`와 `QUANTITY_LIMIT_EXCESS`로 나타난다. 항상 0인 값은 오해를 부르므로 이 코드는 뺀다. 화면에서는 초과 항목을 "이만큼 모자라 결품 위험이 있음"으로 설명하면 된다.
- 품목×월 단위 자재 부족 건수는 KPI가 아니라 상세 행(`materials[]`)에서 본다.

**비교 규칙 (제안)**

- 비교 키는 `code + unit + 계산 버전`이다. 셋 중 하나라도 다르면 같은 줄에 놓지 않는다.
- 인시와 설비시간, 통화가 다른 금액, 단위가 다른 수량은 더하지 않는다.
- **null 규칙**: 합계의 구성 항목이 하나라도 `null`이면 합계도 `null`이다. `null`을 0으로 바꾸지 않는다. 한쪽이 `null`인 차이도 `null`이다.
- 분위수가 다른 안끼리는 공통 분모 KPI(`DEMAND_COVERAGE_P80`, `END_INVENTORY_AT_P50`)와 사용량 KPI만 비교한다.
- 선택한 CAPA나 기간이 다른 결과는 같은 비교 묶음에 넣지 않는다(다른 요청으로 취급).
- `ranking = null`은 유지한다. 엔진은 순위·추천을 내지 않는다.

**정마나미 몫으로 남는 것**: KPI 표시 순서, INFEASIBLE·UNVERIFIED 안을 비교표에 함께 놓을지, 비용 KPI가 null인 안의 표시 문구.

---

## H-10. 데이터 부족·미지원·계산 실패의 분리 — 계산 기준 확정안 (관련: N-03, N-04)

세 층으로 나눈다. 층마다 뜻이 다르므로 섞지 않는다.

**① 요청 전체 — `status`**

| status | 뜻 | 계획안 |
|---|---|---|
| `OK` | 계산을 끝냄. 안이 실행 가능한지는 별개 | 있음 |
| `INPUT_INVALID` | 구조·타입·참조·단위·중복 오류. 고쳐서 다시 보내야 함 | 없음 |
| `DATA_INSUFFICIENT` | 계획 수요 자체를 만들 수 없음(예측 행 없음, 대상 제품 범위 누락 등) | 없음 |
| `ENGINE_ERROR` (신규 제안) | 엔진 내부 예외. 입력 탓이 아님 | 없음 |

**② 평가 행 — `application_state`와 `status`**

| 상황 | application_state | status | reason 예 |
|---|---|---|---|
| 선택했고 계산함, 한도 이내 | APPLIED | SATISFIED | — |
| 선택했고 계산함, 한도 초과 | APPLIED | VIOLATED | `CAPACITY_EXCEEDED`, `QUANTITY_EXCEEDED` |
| 선택했지만 그 월의 한도 행이 없음 | MISSING_INPUT | UNVERIFIED | `HOURS_BUDGET_MISSING`, `QUANTITY_LIMIT_MISSING` |
| 선택했지만 제품의 라우팅·속도가 없음 | MISSING_INPUT | UNVERIFIED | `ROUTING_MISSING`, `RATE_MISSING` |
| 잔업이 필요한데 잔업 한도가 null | APPLIED | UNVERIFIED | `OVERTIME_LIMIT_MISSING` |
| 미지원 패턴(달력 계산, 병렬 공정, 대체 설비 미지정 등) | UNSUPPORTED | UNVERIFIED | `DERIVE_CALENDAR_UNSUPPORTED`, `ALTERNATIVE_NOT_ASSIGNED` |
| 선택 안 함 | NOT_SELECTED | null | — |

- 라우팅이 일부 제품에만 없으면, 그 자원의 부하는 "알 수 있는 제품의 합"으로 계산해 `used_value`에 넣고 상태는 `UNVERIFIED`로 둔다. 단, **알 수 있는 부하만으로 이미 한도를 넘으면 `VIOLATED`다**(빠진 제품이 더해져도 결과가 바뀌지 않으므로).

**③ 계획안 — `feasibility` 집계**

```
선택한 HARD 평가 중 VIOLATED가 하나라도 있으면        → INFEASIBLE
아니고, UNVERIFIED가 하나라도 있으면                   → UNVERIFIED
아니고, 기대한 평가 행이 모두 SATISFIED이면            → FEASIBLE
```

- HARD 위반과 미확인이 같이 있으면 `INFEASIBLE`이다. 미확인 사유도 지우지 않고 `infeasibility_reasons`에 함께 남긴다.
- SOFT 위반은 판정에 영향을 주지 않고 표시만 한다.

**빈 `constraints[]` 방지**

1. 계산 전에 **기대 평가 집합**을 만든다: 선택한 정의 × 대상 자원(또는 범위) × 계획 월.
2. 계산 후 평가 행 수가 기대 집합과 다르면 `ENGINE_ERROR`다. FEASIBLE로 내보내지 않는다.
3. CAPA 선택이 0건이면 `UNVERIFIED`(`NO_CAPACITY_CHECK`)다.
4. 응답 `coverage`에 기대 행 수 / 평가한 행 수 / 미확인 행 수를 넣는다.

**`missing_input_policy` — 결정: `EVALUATE_AS_UNVERIFIED`**

| 값 | 동작 | 판단 |
|---|---|---|
| `EVALUATE_AS_UNVERIFIED` | 계산은 하고 빠진 부분을 UNVERIFIED로 표시 | **채택.** 무엇이 빠졌는지와 나머지 결과를 한 번에 볼 수 있다 |
| `REJECT_REQUEST` | 선택한 제약의 자료가 하나라도 없으면 `DATA_INSUFFICIENT`로 거부 | 채택하지 않음. 엄격하지만 자료를 다 채울 때까지 아무 결과도 못 본다 |

어느 쪽이든 0·무제한·평일·효율 1 같은 **자동 보충은 하지 않는다.** UNVERIFIED 안을 승인할 수 있는지는 판정과 별개이며 N-04가 정한다. 엔진은 승인 여부를 출력하지 않는다.

---

## H-11. 예측 execution_id와 계획 대상 범위 — 계산 기준 확정안 (관련: N-05)

- **동의한다**: 계획은 UUID `execution_id`를 참조하고, 백엔드가 저장된 예측 행으로 `forecast.rows`를 구성한다. `FRUN-…` 문자열은 재현 확인용 메타일 뿐 조회 키가 아니다. 호출자가 rows를 바꿔 넣는 경로는 두지 않는다.
- 엔진은 받은 rows의 `input_digest`를 계획 `input_digest` 계산에 포함한다.
- **계획 대상**: `planning_scope.product_ids[]`에 **독립수요 제품을 명시**한다. "예측 행이 있으면 독립수요"처럼 엔진이 추측하지 않는다. 반제품은 BOM 전개로 종속수요가 생기므로 목록에 없어도 계산된다. 반제품에 독립수요도 있으면 목록에 넣는다.
- **기간**: 계획 월이 예측 `target_month` 범위를 벗어나면 `DATA_INSUFFICIENT`(`HORIZON_OUTSIDE_FORECAST`)다. 외삽하지 않고 새 예측을 요구한다. 필요한 분위수 열이 없을 때도 같다.
- **PARTIAL 예측 — 결정: (가)**. 수요는 계획의 출발점이라, 한도 자료가 빠진 경우(§H-10)와 달리 거부한다.

  | 안 | 동작 | 계산 관점 의견 |
  |---|---|---|
  | (가) | 계획 대상 제품이 예측에서 제외됐으면 요청을 `DATA_INSUFFICIENT`(`FORECAST_COVERAGE_GAP`)로 거부 | **채택.** 빠진 제품의 부하가 공유 자원 합계에서 빠지면 다른 제품의 SATISFIED도 믿을 수 없다 |
  | (나) | 계산하되, 빠진 제품이 쓰는 자원·자재의 평가를 모두 UNVERIFIED로 | 나머지 결과를 볼 수 있지만 UNVERIFIED 범위가 넓어진다 |
  | (다) | 사용자가 그 제품을 대상에서 빼고 다시 요청 | (가)와 함께 쓸 수 있는 후속 행동. 화면 동선은 N-05 |

  어느 안에서도 제외 제품의 수요를 0으로 채우지 않는다.
- **what-if**: 같은 `execution_id`에 `demand_multiplier`와 `overrides[]`만 바꿔 계획을 다시 계산한다. 바꾼 값은 계획 `input_digest`에 들어가므로 같은 조건이면 같은 결과가 나온다. 원본 승인 데이터는 바꾸지 않는다. `base_request_id`로 기준 결과와 짝을 맞춘다.

---

## H-12. 손계산 정답표와 입출력 예시 — 계산 기준 확정안

### 공통 기준 데이터 (합성, 2026-12 한 달)

| 항목 | 값 |
|---|---|
| 제품 | P1, P2 (EA, 완제품) |
| M1 | EQUIPMENT, MACHINE_HOUR, 1대. 정규 100 h, 잔업 한도 20 h, NET |
| C1 | LABOR_POOL, PERSON_HOUR, 2명. 정규 90 인시, 잔업 한도 0 |
| 라우팅 | P1·P2 모두 공정 10 하나. 기준 수량 1 EA당 M1 1 설비시간 + C1 0.5 인시 (`ALL_REQUIRED`) |
| 수량 한도 | Q-P1 70 EA, Q-P2 70 EA (`FINISHED_OUTPUT`, `includes_overtime = false`) |
| 수요·재고 | 안전재고 10, 기초 가용 5 EA. 잔업 허용 행은 기준형으로 재현: 수요 55 → `55 + 10 − 5 = 60`. 잔업 불가 행은 비용절감형으로 재현: 생산 60이면 수요 65 → `65 − 5 = 60`, 생산 50이면 수요 55 → `55 − 5 = 50` |
| 자재 | T-10에서만 사용 (§H-07 예의 값) |

달리 적지 않으면 Q-P1·Q-P2·M1·C1을 모두 선택하고, 자재 제약은 충족이라고 본다.

### 정답표

| ID | 바꾼 조건 | 중간 값 | 평가 결과 | 최종 |
|---|---|---|---|---|
| T-01 | 생산 60·60, 잔업 불가 | M1 120 / 100, C1 60 / 90, Q 60 / 70 ×2 | M1 VIOLATED (+20 h) | **INFEASIBLE** |
| T-02 | 생산 60·60, 잔업 허용 | M1 120 / 120 (경계), 잔업 20 설비시간 | 전부 SATISFIED | **FEASIBLE** |
| T-03 | 생산 50·50, 잔업 불가 | M1 100 / 100 (경계), C1 50 / 90 | 전부 SATISFIED | **FEASIBLE** |
| T-04 | T-02 + 묶음 한도 Q-SET {P1, P2} 110 EA 선택 | Q-SET 120 / 110 | Q-SET VIOLATED (+10 EA) | **INFEASIBLE** |
| T-05 | 생산 60·60, 잔업 불가, **수량만 선택** | Q 60 / 70 ×2. M1·C1은 NOT_SELECTED | 수량 SATISFIED | **FEASIBLE** (검사 범위: 수량) |
| T-06 | T-02에서 M1 잔업 한도 = null | M1 120 > 정규 100, 잔업 한도 모름 | M1 UNVERIFIED (`OVERTIME_LIMIT_MISSING`) | **UNVERIFIED** |
| T-07 | T-01에서 M1의 12월 한도 행 없음 | M1 부하 120, 한도 null | M1 MISSING_INPUT / UNVERIFIED | **UNVERIFIED** |
| T-08 | T-07 + C1 정규 50 인시 | C1 60 / 50 | C1 VIOLATED (+10), M1 UNVERIFIED | **INFEASIBLE** (두 사유 모두 보존) |
| T-09a | P2 공정이 `CHOOSE_ONE` {M1, M2}, 지정 없음. M2 정규 80 h | 배분 불가 | M1·M2 UNSUPPORTED (`ALTERNATIVE_NOT_ASSIGNED`) | **UNVERIFIED** |
| T-09b | T-09a에서 P2 → M2 지정, 잔업 불가 | M1 60 / 100, M2 60 / 80, C1 60 / 90 | 전부 SATISFIED | **FEASIBLE** |
| T-10 | T-02 + 자재 M-A (소요 186, SS 10, 가용 180 kg) | 부족 16 kg | MAT UNVERIFIED (조달 필요 16 kg) | **UNVERIFIED** |
| T-11 | 휴일이 많은 달: M1 정규 80 h, 잔업 20 h, 생산 60·60, 잔업 허용 | M1 120 / 100 | M1 VIOLATED (+20 h) | **INFEASIBLE** |
| T-12a | C1 소요를 "100 EA당 경과 2 h, 투입 3명"으로 입력, 생산 60 | 60 ÷ 100 × 2 × 3 = 3.6 인시 | — | (부하 값 확인용) |
| T-12b | 같은 사실을 "100 EA당 6 인시"로 입력 | 60 ÷ 100 × 6 = 3.6 인시 | T-12a와 같아야 함 | — |
| T-12c | "100 EA당 6 인시" + `resource_units = 3` | — | `RESOURCE_UNITS_WITH_AGGREGATED_TIME` | status **INPUT_INVALID** |
| T-13 | M1 소요가 "3 EA당 1 h", P1만 300 EA, 잔업 불가 | 300 ÷ 3 × 1 = 100 / 100 | M1 SATISFIED (반올림 오차 없음) | **FEASIBLE** |
| T-14 | 라우팅이 없는 자원 ID를 참조 | — | 참조 오류 | status **INPUT_INVALID**, 안 없음 |
| T-15 | 계획 대상 P2가 예측에서 제외(PARTIAL) | — | `FORECAST_COVERAGE_GAP` | status **DATA_INSUFFICIENT**, 안 없음 |
| T-16 | 엔진 내부 예외(테스트에서 강제 발생) | — | — | status **ENGINE_ERROR**, 안 없음 |
| T-17a | 세 안 비교, 안정형: P80 65 → 생산 70·70 | M1 140 / 120, C1 70 / 90, Q 70 / 70 ×2 (경계) | M1 VIOLATED (+20 h) | **INFEASIBLE** |
| T-17b | 세 안 비교, 기준형: P50 55 → 생산 60·60 | M1 120 / 120, 잔업 20 설비시간. 월말 재고 10 | 전부 SATISFIED | **FEASIBLE** |
| T-17c | 세 안 비교, 비용절감형: P50 55 → 생산 50·50 | M1 100 / 100, 잔업 0. 월말 재고 0 | 전부 SATISFIED. 안전재고 미달 10 ×2 (SOFT 경고) | **FEASIBLE** |

"해가 없음 / 데이터가 없음 / 계산 실패"의 구분은 T-01(INFEASIBLE, status OK) / T-07·T-15(UNVERIFIED 또는 DATA_INSUFFICIENT) / T-16(ENGINE_ERROR)으로 본다. 상하위 한도는 §H-05의 예, 달력은 §H-02의 예가 정답표를 겸한다.

- 단위·반올림 기준: §H-04 표. 경계 값(같음)은 충족이다.
- **이 표는 원본 데이터나 기존 Fixture와 무관하게 손으로 계산한 값이다.** 구현 후에는 엔진 출력과 대조하고, 표를 엔진 출력으로 덮어쓰지 않는다.
- 성능 목표 수치는 정하지 않는다(팀 합의 필요).

### 입력 예시 (T-01, 설명용 발췌 — 현행 서버 요청이 아님)

```json
{
  "contract_version": "plan-input-0.2-multi-capacity-draft",
  "horizon": {"period_from": "2026-12", "period_to": "2026-12"},
  "forecast": {"execution_id": "00000000-0000-4000-8000-000000000001"},
  "planning_scope": {"product_ids": ["P1", "P2"]},
  "multi_capacity": {
    "resources": [
      {"resource_id": "M1", "resource_kind": "EQUIPMENT", "time_basis": "MACHINE_HOUR", "parallel_units": 1},
      {"resource_id": "C1", "resource_kind": "LABOR_POOL", "time_basis": "PERSON_HOUR", "parallel_units": 2}
    ],
    "hours_budgets": [
      {"capacity_id": "H-M1", "resource_id": "M1", "period": "2026-12", "availability_mode": "EXPLICIT_PERIOD",
       "time_basis": "MACHINE_HOUR", "unit": "h", "regular_hours": "100", "overtime_hours_limit": "20", "availability_basis": "NET"},
      {"capacity_id": "H-C1", "resource_id": "C1", "period": "2026-12", "availability_mode": "EXPLICIT_PERIOD",
       "time_basis": "PERSON_HOUR", "unit": "h", "regular_hours": "90", "overtime_hours_limit": "0", "availability_basis": "NET"}
    ],
    "quantity_limits": [
      {"capacity_id": "Q-P1", "scope_id": "SC-P1", "period": "2026-12", "limit_qty": "70", "unit": "EA",
       "accounting_point": "FINISHED_OUTPUT", "includes_overtime": false},
      {"capacity_id": "Q-P2", "scope_id": "SC-P2", "period": "2026-12", "limit_qty": "70", "unit": "EA",
       "accounting_point": "FINISHED_OUTPUT", "includes_overtime": false}
    ],
    "routing_steps": [
      {"routing_id": "R-P1", "step_id": "10", "operation_id": "OP-ASSY", "predecessor_step_ids": [], "reference_qty": "1", "qty_unit": "EA"},
      {"routing_id": "R-P2", "step_id": "10", "operation_id": "OP-ASSY", "predecessor_step_ids": [], "reference_qty": "1", "qty_unit": "EA"}
    ],
    "routing_resource_requirements": [
      {"routing_id": "R-P1", "step_id": "10", "requirement_group_id": "G1", "relation": "ALL_REQUIRED", "requirement_id": "RQ-P1-M1",
       "resource_id": "M1", "usage_model": "TIME_PER_REFERENCE_QTY", "run_time": "1", "time_unit": "h", "usage_time_basis": "MACHINE_HOUR"},
      {"routing_id": "R-P1", "step_id": "10", "requirement_group_id": "G1", "relation": "ALL_REQUIRED", "requirement_id": "RQ-P1-C1",
       "resource_id": "C1", "usage_model": "TIME_PER_REFERENCE_QTY", "run_time": "0.5", "time_unit": "h", "usage_time_basis": "PERSON_HOUR"}
    ],
    "constraint_selections": [
      {"selection_id": "S-Q-P1", "definition_id": "Q-P1", "selected": true, "strength": "HARD", "plans": ["COST_SAVING"]},
      {"selection_id": "S-Q-P2", "definition_id": "Q-P2", "selected": true, "strength": "HARD", "plans": ["COST_SAVING"]},
      {"selection_id": "S-M1", "definition_id": "H-M1", "selected": true, "strength": "HARD", "plans": ["COST_SAVING"]},
      {"selection_id": "S-C1", "definition_id": "H-C1", "selected": true, "strength": "HARD", "plans": ["COST_SAVING"]}
    ],
    "capacity_application_policy": {
      "policy_id": "CAP-ALL-SELECTED", "version": "0.1-draft",
      "combination_mode": "ALL_SELECTED", "missing_input_policy": "EVALUATE_AS_UNVERIFIED"
    }
  }
}
```

P2의 요구 2행, `routing_headers`, `capacity_scopes`, `constraint_definitions`, 기존 필드(products·inventory 등)는 줄였다. 잔업 불가 안의 예로 `COST_SAVING`을 썼다(수요 65, 기초 가용 5 → 생산 60).

### 출력 예시 (T-01, 발췌)

```json
{
  "contract_version": "plan-output-0.2-multi-capacity-draft",
  "status": "OK",
  "forecast_execution_id": "00000000-0000-4000-8000-000000000001",
  "verification_scope": {
    "time_grain": "MONTH",
    "checked": ["QUANTITY_LIMIT", "LABOR_TIME", "EQUIPMENT_TIME"],
    "not_verified": ["INTRA_MONTH_CONCURRENCY", "OPERATION_SEQUENCE", "SETUP"]
  },
  "coverage": {"expected_evaluations": 4, "evaluated": 4, "unverified": 0},
  "constraint_evaluations": [
    {
      "evaluation_id": "EV-H-M1-2026-12", "plan_id": "PLAN-COST", "selection_id": "S-M1", "definition_id": "H-M1",
      "application_state": "APPLIED", "dimension": "EQUIPMENT_HOURS", "strength": "HARD",
      "resource_id": "M1", "period": "2026-12", "status": "VIOLATED",
      "limit_value": "100", "used_value": "120", "unit": "h", "time_basis": "MACHINE_HOUR",
      "reason_codes": ["CAPACITY_EXCEEDED"], "note": "한도 초과 20 h (잔업 미허용 안)",
      "rule_version": "plan-rules-0.3-draft",
      "contributions": [
        {"product_id": "P1", "step_id": "10", "produced_qty": "60", "hours": "60", "regular_hours": null, "overtime_hours": null},
        {"product_id": "P2", "step_id": "10", "produced_qty": "60", "hours": "60", "regular_hours": null, "overtime_hours": null}
      ]
    },
    {"evaluation_id": "EV-H-C1-2026-12", "application_state": "APPLIED", "status": "SATISFIED", "limit_value": "90", "used_value": "60", "unit": "h", "time_basis": "PERSON_HOUR"},
    {"evaluation_id": "EV-Q-P1-2026-12", "application_state": "APPLIED", "status": "SATISFIED", "limit_value": "70", "used_value": "60", "unit": "EA"},
    {"evaluation_id": "EV-Q-P2-2026-12", "application_state": "APPLIED", "status": "SATISFIED", "limit_value": "70", "used_value": "60", "unit": "EA"}
  ],
  "comparison": {
    "comparable_plan_ids": [],
    "ranking": null,
    "infeasibility_reasons": [
      {"plan_id": "PLAN-COST", "feasibility": "INFEASIBLE", "causes": ["EV-H-M1-2026-12"]}
    ]
  }
}
```

- 공유 자원의 초과분은 어느 제품의 것이라고 나눌 수 없으므로 제품별 `regular_hours` / `overtime_hours`는 `null`이다. 정규·잔업 구분은 자원 합계에서만 낸다.
- 기존 `export`(`plan-export-0.1-draft`)에는 `APPLIED`인 평가만 `constraints[]`로 옮긴다. `constraint_id`는 `evaluation_id`를 쓴다. `NOT_SELECTED`·기여 내역은 export에 넣지 않고 바깥 응답에만 둔다.

---

## 2. 필요 데이터 필드·단위 (MVP 최소 집합)

계약 초안의 필드 중 MVP 계산에 **실제로 쓰는 것**만 추렸다.

| 묶음 | 필드 | 단위·타입 | 필수 | 없을 때 |
|---|---|---|---|---|
| `resources` | resource_id, resource_kind, time_basis, parallel_units | Code, enum, enum, 정수 ≥ 1 | 필수 | `INPUT_INVALID` |
| `resource_members` | group_resource_id, member_resource_id | Code | 그룹 한도를 쓸 때 | 그룹 평가 `MISSING_INPUT` |
| `hours_budgets` | resource_id, period, availability_mode(=EXPLICIT_PERIOD), time_basis, unit, regular_hours | h 또는 min, Measure ≥ 0 | 시간 제약 선택 시 | 그 자원·월 평가 `MISSING_INPUT` |
| 〃 | overtime_hours_limit | h 또는 min, null 허용 | 선택 | 잔업이 필요해질 때만 `UNVERIFIED` |
| 〃 | availability_basis | GROSS / NET | 필수 | `INPUT_INVALID` |
| `quantity_limits` | capacity_id, scope_id, period, limit_qty, unit, accounting_point, includes_overtime | Qty, 품목 기준 단위 | 수량 제약 선택 시 | 그 한도·월 평가 `MISSING_INPUT` |
| `capacity_scopes` / `scope_products` | scope_id, scope_kind, product_id 목록, aggregation_basis | Code, enum | 수량 제약 선택 시 | `INPUT_INVALID` |
| `routing_headers` | routing_id, routing_version, product_id, valid_from, valid_to | Date | 시간 제약 선택 시 | 그 제품이 쓰는 자원 평가 `MISSING_INPUT` |
| `routing_steps` | routing_id, step_id, operation_id, predecessor_step_ids, reference_qty, qty_unit | Qty > 0 | 〃 | 〃 |
| `routing_resource_requirements` | resource_id, relation, usage_model, run_time, time_unit, usage_time_basis | Measure > 0, h 또는 min | 〃 | 〃 |
| 〃 | resource_units | 정수 ≥ 1 | `ELAPSED`일 때만 필수 | `INPUT_INVALID` |
| 〃 | setup_time, setup_time_unit | Measure ≥ 0, null 허용 | 선택 | 셋업 제외 + `not_verified` |
| `equipment_rates` | rate_id, output_qty, output_unit, per_time, time_unit, rate_scope, speed_kind, efficiency_factor | Qty > 0, Measure > 0 | `EQUIPMENT_RATE_REF`일 때 | 〃 `MISSING_INPUT` |
| `constraint_selections` | selection_id, definition_id, selected, strength, plans | — | 필수 | 선택 0건이면 `UNVERIFIED` |
| `capacity_application_policy` | policy_id, version, combination_mode, missing_input_policy | — | 필수 | `INPUT_INVALID` |

**계약 초안에 추가를 요청하는 필드 (2개)**

| 필드 | 위치 | 타입 | 용도 |
|---|---|---|---|
| `setups_per_period` | 제품·공정·자원·월별 (위치는 대영과 협의) | 정수 ≥ 0 | 월 셋업 횟수 명시 입력 (§H-04) |
| `assigned_resource_id` | 계획 요청의 제품·공정별 지정 | Code | 대체 설비 중 사용할 설비 지정 (§H-05) |

MVP에서 받기는 하되 계산에 쓰지 않는 묶음: `calendars`, `calendar_shifts`, `resource_shift_assignments`, `resource_unavailability`. 이 자료가 와도 엔진은 가용시간을 만들지 않는다.

---

## 3. N 항목과 겹치는 부분 — 정한 기준과 남는 것

| 항목 | 관련 | 정한 기준 | 영향 받는 것 |
|---|---|---|---|
| H-01 결합 방식·필수 제약 | N-01 | 선택한 것 모두 AND, 최소 1종 선택 | 판정 집계, 선택 화면 |
| H-07 자재 부족 판정 | N-02, N-03 | 조달 자료 없으면 UNVERIFIED | T-10 정답, 자재 제약 구현 |
| H-07 안전재고 강도 | N-02 | SOFT, 월말 기준 | SS 평가 행 |
| H-08 비용절감형 정의 | N-02 | 잔업 불가 + 재고 활용(안전재고를 필요량에서 제외). 이름 유지 | 프로필 3종 구현, 예시 재생성 |
| H-09 KPI 목록·STOCKOUT 처리 | N-06 | 초과량 KPI 신설, STOCKOUT 제외 | KPI 계산, 비교 화면 |
| H-10 결측 정책 | N-03 | `EVALUATE_AS_UNVERIFIED` | T-07 정답, 업로드 검증 흐름 |
| H-10 UNVERIFIED 승인 | N-04 | 엔진은 관여하지 않음 | 승인 API (엔진 범위 밖) |
| H-11 PARTIAL 예측 | N-05 | 대상 제품이 빠지면 거부 | T-15 정답, 실행 선택 화면 |

**정마나미 몫으로 남긴 것 (계산과 무관)**

- "월 집계 기준 검사"와 "검사하지 않은 항목"을 화면에서 보여 주는 문구 (N-01, N-07)
- UNVERIFIED 계획을 승인할 수 있는지, 누가 승인하는지 (N-04)
- 비교표의 KPI 표시 순서, INFEASIBLE·UNVERIFIED 안의 배치, 미산출 값의 표시 문구 (N-06)
- 재고 자료를 며칠부터 "오래됨"으로 볼지 (N-08)
- 예측에서 빠진 제품이 있을 때의 안내와 다음 행동 동선 (N-05)

**후속 항목 (MVP 범위 밖, 기록만)**

- 입고 예정·납품 기간 업로드 → 자재 부족을 가능/불가까지 판정
- 한도에 맞춰 같은 비율로 생산을 줄인 "실행 가능한 안"을 엔진이 직접 생성
- 선생산 평준화, 비용 자료를 쓰는 최적화
- 달력에서 가용시간 계산, 대체 설비 자동 선택, 일·교대별 일정 검증

## 4. 다음 작업 순서 (선행 관계 기준)

| 순서 | 작업 | 선행 | 완료 기준 |
|---|---|---|---|
| 1 | 이 답변서를 대영·마나미가 확인 | — | 이의가 없으면 확정, 있으면 해당 항목만 조정해 기록 |
| 2 | 이의로 바뀐 항목이 있으면 정답표의 해당 행 수정 | 1 | 정답표와 결정 내용이 일치 |
| 3 | 다중 CAPA 입출력 계약 버전 확정, 엔진 계약 v0.3으로 갱신 | 1 | 폐기 항목 3건 반영, 신규 필드 2개 반영 여부 결정 |
| 4 | DB·업로드·검증 API (대영) | 3 | 필수 필드·오류 코드가 이 문서와 일치 |
| 5 | 계획 엔진 `engine/plan` 구현 | 2, 3 | 정답표 전 행 통과, 기여 합 = 사용량 대조 통과 |
| 6 | 계획안 3종·KPI·export 투영, 예시 JSON 재생성 | 5, H-08·H-09 결정 | v0.2 §7 예시를 새 규칙으로 교체 |
| 7 | 백엔드 연동 | 4, 6 | 승인 데이터 → 예측 → 계획 → Excel 한 흐름 |

4와 5는 3이 끝나면 서로 기다리지 않고 진행할 수 있다(엔진은 예시 JSON만으로 개발 가능).
