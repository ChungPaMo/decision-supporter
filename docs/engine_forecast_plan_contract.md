# 예측·계획 엔진 계약 초안 v0.2 (2026-11-08)

> 상태: **초안 — 예측·계획 담당 제안, 백엔드·업무 규칙 담당 확인 전.** 데이터 계약 v1(`data_contract_v1.md`)과 출력 계약(`plan-export-0.1-draft`)을 기준으로 작성했다.
> 예시 JSON: [`contracts/examples/engine/`](../contracts/examples/engine/)
> - `forecast_*.json`: **실제 예측 모듈(`engine/forecast`) 출력**(합성 수요 데이터). `python -m engine.tools.make_forecast_examples` 로 다시 만들고, 테스트가 파일과 모듈 출력이 같은지 확인한다. 각 행이 백엔드 `ForecastRow` 를 통과하는 것도 테스트로 확인했다.
> - `plan_*.json`: 아래 계획 규칙을 구현한 참조 계산기 출력. 계획 엔진 구현 전이라 그 안의 예측 행(P50 400, P80 445·460·470)과 날짜(2026-11~2027-01)는 **형식 설명용**이다. 계획 엔진 구현(§10 4~5) 때 예측 모듈 출력과 날짜(2017-01~03)로 다시 만든다. `PlanExportRequest` 검증과 `POST /exports/plans/xlsx` 호출(200, 시트 5개)까지 확인했다.
>
> v0.2 변경: 기존 예측 실험(26-10-26 실험기록)을 원본 데이터로 재현해 대조 완료(§6.1). `p50_method` 열거형을 `MA12 | LGBM_L1` 로 바꿈. 예측 모듈 구현. 비용절감형 정의가 실험기록의 다음작업계획과 달라 확인 요청에 추가(§6.3, §8).

## 1. 흐름과 경계

2026-11-10: 합성 JSON용 `POST /dev/forecasts`는 유지하며, 별도로 승인 수요 → 예측 실행·저장·이력 조회의 `POST/GET /forecasts`를 구현했다([정식 API 계약](api_contract_forecast_runs.md)). 계획 엔진 연결은 아직 없다.

```
v_forecast_input_monthly_demand ──► [예측] forecast_request → forecast_response (ForecastRow[])
                                                                   │  (백엔드가 demand_forecast 에 저장)
승인된 product·material·bom·inventory·capacity  + routing·calendar·costs(신규)
                                                                   ▼
                                     [계획] plan_request → plan_response
                                                             ├─ comparison (실행 가능 안·불가 원인)
                                                             └─ export (= plan-export-0.1-draft 그대로 → Excel)
```

- 엔진은 **DB에 접근하지 않는 순수 Python 모듈**이다. 입력 JSON → 출력 JSON. DB 조회·저장·API 노출은 백엔드가 맡는다. 그래서 JSON 샘플만으로 각자 독립 개발할 수 있다.
- 조건 변경(what-if)은 **재예측 없이 계획만 재계산**한다 (`scenario.reforecast = false`).
- 엔진은 순위·추천을 내지 않는다. FEASIBLE 안 목록과 불가 원인만 주고 선택은 사용자가 한다 (E11 제약 정책 유지).

## 2. 백엔드 확인 요청 6개에 대한 답

| 질문 | 답 (제안) | 계약 영향 |
|---|---|---|
| 월별 수요예측 입력·출력이 기존 예측 코드와 맞는가 | **맞다(확인 완료).** 기존 실험도 창고 합산 제품×월 집계라 입력은 `(product_id, period, demand_qty, unit)` 1행으로 충분하다. 기존 실험의 피처 중 주문건수비·반품비·카테고리는 이 입력에 없지만, 빼고 재현한 결과 차이가 작아(§6.1) 계약을 늘리지 않는다. 출력은 현재 `ForecastRow` 그대로이고, 엔진 응답에 실행 메타(`run`)·제외 제품(`skipped`)·보정 기록(`adjustments`)·경고·오류를 더한다 | `ForecastRow` 변경 없음. 응답 래퍼만 신규 |
| `horizon` 의미 | **`trained_until` 이 속한 달의 다음 달 = 1.** `horizon = 월차(target_month − trained_until 의 월)`. `trained_until` 은 학습에 쓴 마지막 실적월의 **말일**이어야 한다(월 중간 날짜는 입력 오류). 기존 실험과 같은 정의다(예: 2015-03 말 기준 → 2015-05 = h2). 주 평가 지표는 **h=2**(리드타임 1개월 이상), h=1·3 보조 | 백엔드 ⚠ 가정과 같음 → ⚠ 해제 가능 |
| `p50_method` 값 | 열거형 `MA12`(최근 12개월 단순평균, 모든 horizon 같은 값 — **기본**) / `LGBM_L1`(LightGBM l1 점예측). P80·P90은 항상 LightGBM quantile | 열린 문자열 → 열거형 전환 가능 |
| BOM·재고·CAPA 중 필요한 필드 | §5 표. 현재 계약으로 대부분 충분하고, **라우팅(제품별 단위 소요시간)·근무일·비용**이 추가로 필요 | 계약 v1.1 요청 (§8) |
| 생산능력: 월 수량 vs 제품별 소요시간 | **시간 기준 + 제품별 단위 소요시간(라우팅).** 이미 CAPA가 자원별 시간/일이고, 한 라인을 여러 제품이 나눠 쓰면 수량 한도로는 표현이 안 된다. 기업이 "월 N개"만 아는 경우 `run_time_per_unit_h = 월 정규시간 ÷ N` 으로 같은 형식에 넣는다(별도 모드 불필요) | `routing` 엔터티 신규 |
| 3개 계획안 계산 기준 / KPI 출력 형식 | §6.3, §6.6. 출력은 `plan-export-0.1-draft` 를 그대로 `export` 에 담는다 → 백엔드가 그대로 Excel API에 넘길 수 있음 | 출력 계약 변경 1건 제안 (§8) |

## 3. 예측 모듈 입출력

파일: [`forecast_request.json`](../contracts/examples/engine/forecast_request.json), [`forecast_response.json`](../contracts/examples/engine/forecast_response.json)

**요청**

| 필드 | 타입 | 규칙 |
|---|---|---|
| `contract_version` | `"forecast-input-0.1-draft"` | |
| `request_id` | string | |
| `trained_until` | date | 월말. 이 날짜 이후 실적이 있으면 `ROW_AFTER_TRAINED_UNTIL` 오류(미래 누수 금지) |
| `horizon_months` | int 1~12 | 기본 3. 4~12는 `HORIZON_BEYOND_VALIDATED` 경고(백테스트는 h=1~3만 함). `ForecastRow.horizon` 은 1~36 이지만 엔진은 더 좁게 받는다 |
| `p50_method` | `MA12` \| `LGBM_L1` | 기본 `MA12` |
| `quantiles` | `["P50","P80","P90"]` | 고정 |
| `min_history_months` | int 12~120 | 기본 12. 첫 실적월~`trained_until` 기간이 이보다 짧으면 그 제품은 `skipped` |
| `missing_month_policy` | `ZERO_FILL` | 기간 안에 행이 없는 달을 0으로 채우고 경고. 기존 실험의 결정("빈 월은 0")과 같다. 기업 데이터는 §9 |
| `random_seed` | int | 재현성 |
| `rows[]` | `{product_id, period, demand_qty, unit, source_type, batch_id}` | 뷰 `v_forecast_input_monthly_demand` 행 그대로. 수량은 10진 문자열(소수 4자리 이하, JSON 숫자·지수 표기 거부). 같은 (제품, 월) 중복은 `DUPLICATE_PERIOD` 오류 |

**응답**

| 필드 | 설명 |
|---|---|
| `status` | `OK`(전 제품 예측) / `PARTIAL`(일부 제외) / `DATA_INSUFFICIENT`(예측된 제품 없음, 또는 학습 행 없음 `NO_TRAINING_ROWS`) / `INPUT_INVALID`(`errors[]`, `run: null`) |
| `run` | `run_id, engine, p50_method, quantile_model_version, trained_until, horizon_months, random_seed, input_digest(sha256), generated_at, data_classification`. `run_id = FRUN-<digest 앞 12자리>`, digest 는 `request_id` 를 뺀 요청 내용 → 같은 데이터·설정이면 같은 `run_id` |
| `forecasts[]` | **`ForecastRow` 그대로** (`product_id, target_month, horizon, p50, p80, p90, p50_method, quantile_model_version, trained_until`). 수량은 소수 4자리 이하 10진 문자열 |
| `skipped[]` | `{product_id, reason_code, detail, observed_months}` — `INSUFFICIENT_HISTORY`, `ALL_ZERO`, `UNIT_MIXED` |
| `adjustments[]` | 분위수 교차 보정 기록 `{product_id, target_month, code: "QUANTILE_CROSSING_FIXED", before, after}` |
| `warnings[]` | `ZERO_FILLED_MONTH`(제품·월 목록), `ZERO_RECENT_DEMAND`(최근 12개월 0 → 전부 0 예측), `SMALL_TRAINING_SET`(학습 행 1,000 미만), `HORIZON_BEYOND_VALIDATED` |
| `errors[]` | `{code, loc?, detail}` — `SCHEMA_INVALID`, `TRAINED_UNTIL_NOT_MONTH_END`, `ROW_AFTER_TRAINED_UNTIL`, `DUPLICATE_PERIOD`, `QUANTILES_UNSUPPORTED`, `NO_TRAINING_ROWS` |

## 4. 계획 모듈 입출력

파일: [`plan_request.json`](../contracts/examples/engine/plan_request.json), [`plan_response.json`](../contracts/examples/engine/plan_response.json), 수요 +20% 재계산 [`plan_request_whatif_demand120.json`](../contracts/examples/engine/plan_request_whatif_demand120.json) / [`plan_response_whatif_demand120.json`](../contracts/examples/engine/plan_response_whatif_demand120.json)

**요청** (`plan-input-0.1-draft`)

| 필드 | 출처 | 비고 |
|---|---|---|
| `as_of_date`, `horizon{period_from, period_to}` | 백엔드 | 재고 기준일, 계획 월 범위 |
| `scenario{scenario_id, label, demand_multiplier, overrides[], base_request_id?, reforecast:false}` | 화면 | what-if 조건 |
| `forecast{run_id, rows: ForecastRow[]}` | 예측 결과(DB) | |
| `products[]`, `materials[]`, `units[]` | 계약 v1 | `units` 는 `to_base_factor` 포함 (kg↔g 환산) |
| `boms[]` (헤더 + `lines[]`) | 계약 v1 | APPROVED·활성·유효기간 행만 |
| `inventory[]` | 계약 v1 | |
| `capacity[]` | 계약 v1 | |
| `routings[]` `{product_id, resource_id, run_time_per_unit_h}` | **신규** | |
| `calendar.working_days{YYYY-MM: int}` | **신규** | 없으면 평일 수 + 경고 |
| `costs{material_unit_cost[], resource_hourly_cost[], holding_cost_per_unit_month[]}` | **신규(선택)** | 없으면 해당 비용 KPI = `null`(미산출), 0으로 채우지 않음 |
| `policy{plans[], include_product_safety_stock, include_material_safety_stock, safety_stock_strength, demand_strength, integer_units}` | 업무 규칙 | §6.3 |
| `evidence[]` | 백엔드 | 출력 `evidence` 로 그대로 전달 |

**응답** (`plan-output-0.1-draft`)

| 필드 | 설명 |
|---|---|
| `status` | `OK` / `INPUT_INVALID` / `DATA_INSUFFICIENT` (계산 자체가 안 될 때). **계획이 실행 불가인 것은 `status` 가 아니라 각 안의 `feasibility`** — 성공/데이터 부족/실행 불가를 분리 |
| `input_digest`, `engine`, `scenario` | 재현성: 같은 입력 → 같은 출력 |
| `errors[]`, `warnings[]` | `{code, detail, product_id?, ...}` 예: `BOM_VERSION_AMBIGUOUS`, `ROUTING_MISSING`, `UNIT_DIMENSION_MISMATCH`, `INVENTORY_STALE` |
| `comparison` | `comparable_plan_ids`(FEASIBLE 안), `ranking: null`, `infeasibility_reasons[]{plan_id, feasibility, causes[]}` |
| `export` | **`plan-export-0.1-draft` 문서 그대로.** 안별 `kpis[]`, `production[]`, `materials[]`, `constraints[]`, `assumptions[]` |

## 5. 필수 데이터 목록

| 데이터 | 필드 | 필수 | 현재 계약 | 없을 때 |
|---|---|---|---|---|
| 월별 수요 | product_id, period, demand_qty, unit | 필수 | ✅ | 예측 불가 |
| ↳ 이력 길이 | 제품당 ≥12개월 (권장 24개월 이상: LightGBM 학습) | 필수 | — | `INSUFFICIENT_HISTORY` 로 제외 |
| 제품 | product_id, product_name, base_unit, product_type, safety_stock_qty | 필수 | ✅ | — |
| 자재 | material_id, material_name, base_unit, safety_stock_qty | 필수 | ✅ | — |
| BOM | bom_id, product_id, status, is_active, valid_from/to, base_qty / line_no, component_type, component_id, qty_per, unit, scrap_rate | 필수 | ✅ | 해당 제품 자재 계산 `UNVERIFIED` |
| 재고 | item_type, item_id, as_of_date, on_hand_qty, allocated_qty, unit | 필수 | ✅ | 재고 0 가정 금지 → `UNVERIFIED` |
| 단위 | unit_code, dimension, to_base_factor | 필수 | ✅ (DB `unit`) | 환산 불가 시 `UNIT_DIMENSION_MISMATCH` |
| 생산능력 | resource_id, regular_hours_per_day, overtime_hours_per_day_max | 필수 | ✅ | — |
| **라우팅** | product_id, resource_id, run_time_per_unit_h | 필수 | ❌ 신규 요청 | 그 제품의 CAPA 제약 `UNVERIFIED` |
| **근무일** | 월별 근무일수 | 권장 | ❌ 신규 요청 | 평일 수로 계산 + 경고 |
| **비용** | 자재 단가, 자원 시간당 정규·잔업 비용, 품목 재고유지비/월 | 선택 | ❌ 신규 요청 | 비용 KPI `null` |
| 입고예정 | material_id, qty, expected_receipt_date | 선택(v1.1) | ❌ | 자재 부족 = 조달 필요량, 조달 가능 여부 `UNVERIFIED` |
| 공급사 리드타임 | material_id, lead_time_days | 선택(v1.1) | ❌ | 위와 같음 |
| 수주 | sales_order | 선택(후속) | 테이블만 있음 | v0.1은 예측 수요만 사용 |

## 6. 계산 규칙

### 6.1 예측

구현: `engine/forecast/` (`engine.forecast.run(dict) -> dict`). 근거: 26-10-26 수요예측 실험기록 §4~§9.

1. 제품별로 `trained_until` 까지의 월 시계열을 만든다. 첫 실적월~`trained_until` 사이에 행이 없는 달은 `ZERO_FILL` 정책이면 0 (경고 기록).
2. 관측 기간 < `min_history_months` → `skipped(INSUFFICIENT_HISTORY)`. 전부 0 → `skipped(ALL_ZERO)`. 단위가 둘 이상 → `skipped(UNIT_MIXED)`.
3. **P50**: `MA12` = 최근 12개월 평균 (horizon 1~H 모두 같은 값, 10진 정확 계산) / `LGBM_L1` = LightGBM l1 점예측.
4. **P80·P90**: LightGBM quantile 회귀 α=0.8, 0.9.
   - **학습 행** = (제품, 기준 월 t, h). h 를 피처로 넣은 **전 제품 통합 단일 모델, 직접 다단계**(재귀 아님). 학습은 h=1~max(3, H).
   - **피처**(기준 월까지의 정보만): 최근 수요 lag 0~11, 작년 같은 달(h>12 이면 2년 전), ma3·ma6, 표준편차 6·12, 최근 12개월 0인 달 수, 목표 월(1~12), h.
   - **정규화**: 수요 관련 값과 목표를 제품별 기준 월 ma12 로 나눈다. ma12 = 0 인 행은 학습에서 빼고, 그 제품은 0 예측 + `ZERO_RECENT_DEMAND` 경고.
   - **하이퍼파라미터**(실험기록 선택값): num_leaves 7, min_data_in_leaf 50, 200 rounds, learning_rate 0.05, product_id 미포함. 실행마다 요청 이력 전체로 학습한다(100제품·60개월 기준 학습+예측 약 2초). 모델 파일은 저장하지 않는다.
   - **실험기록과 다른 점**: 주문건수비·반품비·카테고리 피처를 뺐다(계약 입력에 없음). 추세비는 기록대로 ma3 와 같은 값이라 뺐다. 시드 3회 평균은 무작위 샘플링이 없는 설정이라 결과가 같아(최대 차이 8.9e-16) 시드 1개로 줄였다.
5. **분위수 교차 보정**: P50과 P80·P90이 서로 다른 모델이라 교차할 수 있다. `p80 = max(p80, p50)`, `p90 = max(p90, p80)` 후 `adjustments` 에 기록 (DB CHECK `p50 ≤ p80 ≤ p90` 보장). 실험기록 §9 결정과 같다.
6. 음수 예측은 0으로 자르고, 소수 4자리 반올림(ROUND_HALF_UP) 10진 문자열로 낸다.
7. **성능**은 실행마다가 아니라 별도 백테스트로 낸다: `python -m engine.backtest` 가 예측 모듈 자체를 기준 월마다(26개) 다시 돌린다. 공개 데이터(Kaggle felixzhao "Forecasts for Product Demand", 상위 100제품, 검증 2015 / 테스트 2016):

| 구분 (h=2) | P50 WAPE (Bias) | pinball P80 / P90 | 커버리지 P80 / P90 | 교차 보정 행 |
|---|---|---|---|---|
| 검증, `MA12` | **0.254** (+1.0%) | 0.113 / 0.080 | 0.842 / 0.910 | 17 / 1,200 |
| 검증, `LGBM_L1` | 0.259 (+0.7%) | 〃 | 0.841 / 0.910 | 12 / 1,200 |
| 테스트, `MA12` | 0.241 (+0.1%) | 0.097 / 0.068 | 0.808 / 0.903 | 11 / 1,200 |
| 테스트, `LGBM_L1` | **0.235** (−1.8%) | 〃 | 0.808 / 0.903 | 5 / 1,200 |

   - **대조 결과**: 실험기록 전 피처 재현은 ma12 검증 0.254·테스트 0.241, LightGBM 검증 0.257·테스트 0.233, quantile 검증 pinball 0.111/0.080·커버리지 0.836/0.910 으로 기록과 일치했다(교차 행만 기록 8~10, 재현 17 — 세는 기준 차이로 추정). 위 표는 3개 피처를 뺀 엔진 결과다.
   - P50 기본값 `MA12` 는 실험기록 §9 사전 규칙(검증 5% 이상 개선 & |Bias| ≤ 5%)을 통과한 ML 이 없어서다. 테스트에서 LightGBM 이 앞서도 사전 원칙대로 재선정하지 않는다(멘토 확인 필요).
   - 데모 기준: "현재" = 2016-12 말, `trained_until = 2016-12-31` → 2017-01~03 예측. 공개 데이터 변환은 `python -m engine.datasets.kaggle_product_demand`(실험기록 §5·§6 전처리 규칙). 결과는 GPL-2.0 가공본이라 **저장소 밖**에 둔다.

### 6.2 계획 공통 (월 단위, PoC `decision_logic.md` §1~3 단순화)

기호: `d_m` 월 수요(선택 분위수 × `demand_multiplier`, EA는 정수 올림), `SS` 안전재고, `I_0 = on_hand − allocated` (as_of_date 기준), `cap_m(r) = (정규 h/일 [+ 잔업 h/일]) × 근무일_m`.

1. **완제품 순소요**: `need_m = max(0, ⌈d_m + SS − I_{m−1}⌉)`, `I_m = I_{m−1} + prod_m − d_m`. `I_m < 0` 이면 결품이며 다음 달로 이월된다(다음 달 `need` 에 포함).
2. **능력 상한**: `prod_m = min(target_m, ⌊cap_m(r) ÷ run_time⌋)` (EA 정수 내림). 한 자원을 여러 제품이 쓰면 ⚠ v0.1은 `product_id` 순 배분(단순화), v0.2에서 납기·우선순위 규칙 적용.
3. **BOM 전개**: 저수준코드(LLC) 순, 품목마다 수요를 합친 뒤 재고를 **한 번만** 차감하고 순소요만 전개 (`decision_logic.md` §2.1). 구성품 소요 = 상위 생산량 × `qty_per / base_qty` × `(1 + scrap_rate)`, 기준 단위로 환산. BOM 선택: 그 달 1일에 유효한 APPROVED·활성 BOM이 정확히 1개일 때만, 아니면 `BOM_VERSION_AMBIGUOUS`/`BOM_MISSING`.
4. **자재 부족**: 월별 누적 소요 + 자재 SS 가 기준일 가용(on_hand − allocated)을 넘는 양. 그 달 증가분을 `shortage_qty` 로 낸다 = **조달 필요량**. 입고예정·리드타임 데이터가 없으므로 제약 상태는 `VIOLATED` 가 아니라 **`UNVERIFIED`**(조달 가능 여부 미확인). ⚠ PoC E11 T-01(기준형 자재 부족 = 위반)과 다르게 바꾼 부분이라 확인 필요.
5. **반제품**은 독립 수요가 없으면 예측하지 않고, 상위 생산량에서 나온 종속수요로 같은 1~2단계를 적용한다.

### 6.3 3개 계획안

| 안 | 수요 기준 | 잔업 | 생산 방식 | 의도 |
|---|---|---|---|---|
| **BASELINE 기준형** | P50 | 허용 | chase: 월 필요량만큼 | 예측 그대로 계획 |
| **STABLE 안정형** | **P80** (설정으로 P90) | 허용 | chase | 결품 위험 축소, 재고·잔업 증가 |
| **COST_SAVING 비용절감형** | P50 | **불가** | leveling: 정규시간 초과분을 **앞 달 정규 여유에 선생산**, 남는 부족은 다음 달 여유로 보충 | 잔업비 제거, 재고유지비·안전재고 미달 감수 |

- 세 안 모두 같은 재고·BOM·CAPA·안전재고 규칙을 쓰고 위 표의 축만 다르다.
- ⚠ **비용절감형 정의 미확정**: 26-10-26 다음작업계획은 "P50 수요 + 재고 활용 최대화, 추가 조달 최소화"로 적었고, 위 표는 "잔업 불가 + 선생산 평준화"다. 두 정의는 비용 축이 다르다(조달비 vs 잔업비). 업무 규칙 담당과 정한다(§8).
- 기본값(업무 규칙 확정 전): **월 수요 충족 = HARD**, **안전재고 = SOFT**.

### 6.4 제약 판정 (E11 정책 유지)

| constraint_id | dimension | 강도 | 판정 |
|---|---|---|---|
| `CAP-{자원}-{월}` | WORK_HOURS | HARD | `limit` = 그 안의 가용시간(정규 또는 정규+잔업), `used` = 계획 생산시간. 한도에 걸리면 note 에 "목표 생산에 X h 필요" |
| `DEM-{제품}-{월}` | DUE_DATE | HARD | 월말까지 수요(+이월분) 충족. 미충족 → `VIOLATED`, note 에 결품 수량과 원인 자원 |
| `SS-{제품}-{월}` | OTHER | SOFT | 월말 재고 ≥ 안전재고 |
| `MAT-{자재}-{월}` | MATERIAL | HARD | 누적 소요 + SS ≤ 가용. 초과 → `UNVERIFIED` (§6.2-4) |

`feasibility`: HARD 중 하나라도 `VIOLATED` → `INFEASIBLE`, 아니고 `UNVERIFIED` 가 있으면 → `UNVERIFIED`, 나머지 → `FEASIBLE`. SOFT 위반은 feasibility 에 영향 없이 표시만.

### 6.5 What-if

`scenario.demand_multiplier`(예 1.2), `overrides[]`(예: 자원 잔업 한도, 안전재고 배수, 재고 수량 변경)를 적용해 **같은 forecast run 으로** 계획만 재계산한다. 응답의 `scenario.base_request_id` 로 기준 결과와 짝을 맞춘다.

### 6.6 KPI (안별 `kpis[]`, 값은 10진 문자열, 산출 불가는 `null`)

| code | 단위 | 산식 |
|---|---|---|
| `TOTAL_PRODUCTION` | EA | 완제품 생산량 합 |
| `PLANNED_DEMAND` | EA | 그 안이 쓴 수요(분위수 × 배수) 합 |
| `STOCKOUT_QTY` | EA·월 | 월말 결품 이월 수량 합 (계획 수요 기준) |
| `DEMAND_COVERAGE_P80` | % | `min(1, (I_0 + Σprod) ÷ Σ P80수요) × 100` — **모든 안을 같은 위험 기준으로 비교** |
| `SAFETY_STOCK_SHORTFALL` | EA | 월말 안전재고 미달 합 |
| `END_INVENTORY_AT_P50` | EA | P50 수요가 실현될 때 기간 말 완제품 재고 |
| `OVERTIME_HOURS` | h | Σ max(0, 사용시간 − 정규시간) |
| `MATERIAL_SHORTAGE_ITEMS` | 건 | 조달 필요 자재 품목 수 |
| `MATERIAL_COST` / `REGULAR_LABOR_COST` / `OVERTIME_COST` / `HOLDING_COST` / `TOTAL_COST` | KRW | 자재 소요 × 단가 / 정규 사용시간 × 정규단가 / 잔업시간 × 잔업단가 / P50 기준 월말 재고 × 월 유지비 / 앞 4개 합(결품 페널티 제외) |

> 다품목 합계는 같은 단위끼리만 더한다. 결품 페널티·납기 지연 비용은 기준이 없어 금액으로 바꾸지 않는다.

## 7. 예시 결과 (합성 데이터, `contracts/examples/engine/`)

계획 엔진 구현 전 참조 계산기 결과다(예측 행·날짜는 형식 설명용, 머리말 참고).

조건: 제품 0001001(완제품, SS 10, 가용 7) ← 반제품 0001002 ← 자재 M001(2.5 kg, 스크랩 5%)·M002. L1 0.42 h/EA, 8 h/일 + 잔업 2 h/일, 근무일 21·22·20. P50 400/월(MA12), P80 445·460·470.

| 시나리오 | 안 | 판정 | 완제품 생산 (11·12·1월) | 잔업 h | P80 충족률 | 총비용(KRW) | 불가·미확인 원인 |
|---|---|---|---|---|---|---|---|
| 기본 | 기준형 | FEASIBLE | 403·400·400 | 9.26 | 88.00% | 27,432,925 | — |
| 기본 | 안정형 | **UNVERIFIED** | 448·460·470 | 74.76 | 100% | 33,033,550 | M001 1월 누적 부족 217.25 kg (조달 미확인) |
| 기본 | 비용절감형 | FEASIBLE | 400·419·380 | 0 | 87.71% | 27,221,325 | (SOFT) 안전재고 미달 11월 3·1월 4 EA |
| 수요 +20% | 기준형 | UNVERIFIED | 483·480·476 | 100.38 | 87.64% | 34,143,025 | M001 부족 377.375 kg |
| 수요 +20% | 안정형 | **INFEASIBLE** | 500·523·476 | 125.58 | 91.27% | 36,155,525 | 11·12·1월 결품 27·56·144 EA — L1 작업시간 한도 |
| 수요 +20% | 비용절감형 | **INFEASIBLE** | 400·419·380 | 0 | 73.09% | 27,143,325 | 11·12·1월 결품 73·134·234 EA — L1 작업시간 한도 |

## 8. 계약 변경 요청

**백엔드(데이터 계약 v1.1)**
1. `routing` 엔터티 신규: `(product_id, resource_id)` 키, `run_time_per_unit_h > 0` — 업로드·승인 게이트 동일 적용.
2. 월별 근무일(`calendar`) — 업로드 또는 계획 요청 파라미터 중 택1.
3. 비용(자재 단가, 자원 시간당 정규·잔업 비용, 재고유지비) — v0.1은 계획 요청 파라미터(합성)로 받고 DB 테이블은 후속.
4. `horizon`·`p50_method` ⚠ 해제 (§2 정의), `p50_method` 열거형 `MA12 | LGBM_L1`.
5. 예측 실행 메타를 남길 곳: `demand_forecast` 에 `run_id`(또는 `forecast_run` 테이블: input_digest·seed·skipped 수).
6. 출력 계약 `plan-export` v0.2: 안마다 수요 분위수가 달라 최상위 `forecast_basis.quantile` 하나로 표현이 안 된다 → `PlanResult.forecast_quantile` 추가 제안. v0.1에서는 최상위 `quantile: null` + 안별 `assumptions` 에 기록.
7. 공개 수요 데이터 전처리 위치: 원본은 일 단위 주문 행(창고·반품·날짜 결측 포함)이고 백엔드 업로드는 제품×월 집계를 받는다. 제안: 공개 데이터는 `engine/datasets/kaggle_product_demand.py` 로 월 집계 CSV 를 만든 뒤 일반 업로드 경로로 올린다(전처리 규칙은 그 파일 머리말, 실험기록 D-03 전달 건). 기업 데이터의 반품·결측 규칙은 업로드 검증에서 따로 정한다.

**업무 규칙·UX 담당 확정 요청**
1. 안전재고: SOFT(기본) / HARD.
2. 월 수요 충족: HARD(기본) / SOFT(결품 허용 + 페널티).
3. 안정형 분위수: P80(기본) / P90.
4. 자재 부족을 "조달 필요량 + UNVERIFIED"로 볼지, "위반"으로 볼지.
5. 잔업 허용 한도, 비용 단가(합성 값 범위), KPI 목록·표시 순서 확정.
6. 결품 페널티를 금액으로 넣을지(넣으면 기준 단가 필요).
7. **비용절감형 정의**: "잔업 불가 + 선생산 평준화"(현재 초안) / "재고 활용 최대화 + 추가 조달 최소화"(다음작업계획) / 둘의 조합.

## 9. 미확정·확인 필요

- ✅ 기존 예측 실험 대조 완료(§6.1): 주문일 기준·창고 합산(Whse_S 제외) 제품×월 집계, 전 제품 통합 직접 다단계 모델. quantile 모델 버전 이름은 `lgbm-q/engine-<엔진 버전>/<피처셋>`(예: `lgbm-q/engine-0.1.0/demand-v1`).
- 결측월 0 채움: 공개 수요 데이터(주문 기반)는 "행 없음 = 주문 0"(실험기록 결정). 기업 데이터는 "미집계"일 수 있다 → 필요하면 `missing_month_policy` 에 `REJECT` 를 추가한다(v0.1 미구현).
- 점예측 ma12 + ML 분위수 구성이 "ML 모델 1종" 요건에 맞는지 (멘토 확인, 실험기록 §11).
- GPL-2.0 공개 데이터의 가공본을 실행 패키지·데모에 포함해도 되는지 (멘토·더브릿지 확인, 실험기록 §11). 그 전까지 가공본은 저장소 밖에 둔다.
- 반품·음수 수요: 계약상 `demand_qty ≥ 0` 이라 엔진에 오지 않는다. 반품 처리 규칙은 업로드 단계 책임(`Whse_S` 절단 규칙은 공개 데이터셋 전용).
- 다품목 자원 배분 순서, 다단계 라우팅(공정 여러 개)은 v0.2.

## 10. 구현 순서 (작업 단위, 날짜 없음)

앞 단계의 완료 기준을 통과해야 다음 단계로 간다. 계획 엔진(4~6)은 예시 JSON 입력만으로 개발할 수 있어 예측(2~3)과 병렬로 진행할 수 있다.

| 순서 | 작업 | 선행 | 완료 기준 |
|---|---|---|---|
| 1 | 이 계약 합의 (대영·마나미 확인, §8 요청 반영) | — | ⚠ 항목 결론 기록 |
| 2 | ✅ 예측 모듈 골격: 입력 검증, 결측월 처리, `MA12`, skipped·warnings | 1 | `forecast_request.json` → 응답 재현, 단위 테스트 (`engine/tests`) |
| 3 | ✅ 기존 실험 재현·대조 후 LightGBM quantile 통합, 교차 보정, 백테스트(WAPE·포함률·pinball) 재측정 | 2 | 같은 입력·seed → 같은 출력, 백테스트 표(§6.1) |
| 4 | 계획 엔진 핵심: 순소요·BOM 전개(LLC)·CAPA·제약 판정 | 1 | 손계산 정답 테스트(E11 T-01) 통과 |
| 5 | 3개 계획안·KPI·`export` 생성 | 4 | §7 6개 케이스 일치, `POST /exports/plans/xlsx` 200 |
| 6 | what-if 재계산, 오류 케이스(BOM 모호·라우팅 없음·단위 불일치) | 5 | 케이스별 테스트 |
| 7 | 백엔드 연동 (대영 API에서 엔진 호출) | 3, 6, 라우팅 계약 | 승인 데이터 → 예측 → 3안 → Excel 한 흐름 |

모듈 형태: 저장소 `engine/` 패키지 (`engine.forecast.run(dict) -> dict`, `engine.plan.run(dict) -> dict`), CLI `python -m engine.forecast --input req.json --output res.json`. LightGBM·numpy 의존성은 백엔드와 분리된 `engine/requirements.txt` 로 둔다(백엔드는 pandas 미사용 방침 유지).
