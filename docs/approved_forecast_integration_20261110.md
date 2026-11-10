# 3단계 — 승인 수요 기반 예측 통합 (2026-11-10)

기존 미커밋 변경을 보존하고 두 MVP 저장소를 별도로 유지했다. PoC·sisicallcall 자원·Windows 서비스 변경 및 commit/push는 없다.

## 구현 결과

- 승인된 월별 수요를 학습 기준일까지 조회해 기존 engine.forecast를 호출한다. 모델·계산식은 변경하지 않았다.
- 실행·입력 스냅샷·출처 버전·엔진/의존성/코드 지문·결과·제외·보정·경고·오류를 원자적으로 저장한다.
- 같은 입력/설정/런타임은 완료 이력을 재사용하고, 동시 계산은 409, 실패 이력은 재시도를 허용한다.
- React 예측 화면은 실제 실행·저장·목록·상세 API를 사용한다. Fixture fallback은 없다.
- 기준일·기간·P50 방식·최소 이력·시드를 선택할 수 있다. 저장 결과는 새로고침·API 프로세스 재시작 후에도 복원된다.

API 요청·응답과 HTTP별 처리의 기준은 [정식 예측 API 계약](api_contract_forecast_runs.md)이다.

## 저장과 마이그레이션

신규 **0005**만 추가했다. 작업 시작 시 기록한 0001~0004의 SHA256을 마지막에 비교해 바이트 단위 보존을 확인했다.

| 구조 | 저장 내용·제약 |
|---|---|
| forecast_run | 실행 UUID, 조건, 원본 입력 JSON 스냅샷, 입력 digest, 런타임 지문, 전체 결과/실패, 행 수·시각 |
| forecast_run_input | 실행 FK와 승인 배치 FK, 승인 버전·원본 SHA256·검증 digest·승인 시각. 미승인/변경된 출처는 트리거가 차단 |
| demand_forecast.forecast_run_id | 새 실행의 FK. 실행별 제품/목표월/horizon 유일. 기존 행은 NULL legacy로 보존 |

개발 DB `chungpamo`는 0004에서 0005로 정상 업그레이드했다. 기존 테이블 건수는 그대로이며 업무 데이터는 0행, unit은 6행이다. 새 실행 테이블도 0행이다. 개발 DB에서 모델 실행·시험 데이터 쓰기·초기화를 하지 않았다.

테스트 DB `chungpamo_test`는 0005를 사용한다. 기존 예측 행 보존, 빈 스키마 왕복, 이력이 있는 경우 downgrade 거부를 실제 확인했다. 테스트 초기화는 이 DB만 대상으로 한다.

## 테스트

```powershell
# decision-supporter/backend
..\.venv\Scripts\python.exe -m pytest tests ../engine/tests -q --tb=short
# 478 passed, 0 failed, 0 skipped (30.04s)

# decision-supporter-frontend
npm run type-check
npm run build
npm run test:e2e
# 8 passed, 0 failed, 0 skipped (7개 + API 재시작 복원 1개)
```

신규 정식 예측 테스트 17개와 JSON Schema 동기화 시험 1개가 추가됐다. 기존 테스트의 assertion을 완화하거나 skip으로 바꾸지 않았다. 기존 health 정상 경로 시험도 실제 테스트 DB 엔진으로 고정했다.

검증 범위:

- 승인/미승인/기준일 이후 수요를 구분하고 입력 배치 버전·원본 지문·검증 지문을 유지한다.
- 저장한 JSON을 엔진에 다시 넣어 생성 시각을 제외한 결과 전체를 재현한다. 엔진 input_digest와 저장 digest가 일치한다.
- MA12/LGBM_L1의 같은 대상월 결과가 공존한다. 입력 또는 엔진 런타임 변경 시 새 실행을 만든다.
- 같은 입력 재실행은 원래 실행을 재사용한다. 두 연결이 동시에 요청하면 한 번만 계산하고 후발 요청은 409다.
- INPUT_INVALID/run=null, 이력 부족, 학습 행 없음, PARTIAL, 모델 예외·의존성 누락·NaN 출력을 처리한다.
- 실제 FK 위반 저장 실패 시 실행 메타·입력 참조·예측 행이 모두 롤백된다. 원본 승인 기록은 유지된다.
- API 프로세스 재시작 뒤 실제 저장 결과를 읽는다. 브라우저는 실제 CSV/XLSX 등록·승인부터 예측까지 수행한다.
- 브라우저에서 201 PARTIAL, 200 재사용, 기간 경고, DATA_INSUFFICIENT, 422 INPUT_INVALID/run=null, 새로고침 복원과 네트워크 실패 시 결과 미표시를 확인한다.

브라우저 시험 데이터는 합성 데이터다. 기업 수요에 대한 예측 정확도·운영 품질 검증으로 표현하지 않는다. Starlette/httpx 사용 중단 예정 경고는 기존과 동일하게 남아 있다.

## 파일별 변경 — 이번 단계

백엔드 저장소:

| 파일 | 이유 |
|---|---|
| backend/migrations/versions/0005_forecast_execution_history.py | 실행·출처·결과 FK와 유일 키 및 승인 출처 트리거 |
| backend/app/models.py | 신규 DB 모델 및 실행별 결과 관계 |
| backend/app/forecast/runs.py, runs_schemas.py | 승인 입력 스냅샷·실행·중복/동시성·원자 저장·이력 계약 |
| backend/app/forecast/service.py | 개발/정식 공용 어댑터와 기존 ForecastRow 수량·분위수 검증. 응답 메타는 보존 |
| backend/app/api/forecasts.py, backend/app/main.py, backend/app/errors.py | 정식 3개 endpoint·저장 비활성·404/409/실패 응답 |
| backend/export_contract_schema.py, contracts/forecast_run_v1.schema.json, contracts/openapi.json | 생성 모델 기반 계약 동기화 |
| backend/tests/test_forecast_runs.py | 실제 PostgreSQL 정식 실행·재현·실패·동시성·legacy 보존 시험 |
| backend/tests/test_contract_schema_sync.py | 정식 실행 Schema 동기화 |
| backend/tests/test_migration_consistency.py, test_postgres_integration.py, test_sql_repository.py | 신규 head 체인·왕복 복구 및 새 FK의 테스트 정리 |
| backend/tests/test_health.py | 실제 health 검증도 테스트 DB로 한정 |
| README.md, .env.example, engine/README.md, docs/frontend_guide.md, docs/engine_forecast_plan_contract.md, docs/api_contract_forecast_runs.md | 최신 상태·실행 조건·API 경계 문서화 |

프론트엔드 저장소:

| 파일 | 이유 |
|---|---|
| src/screens/ForecastScreen.tsx | 실행 폼·이력·상태별 결과·출처·경고·오류·복원 |
| src/services/api.ts, src/types/engine.ts | 실제 실행·이력 API 및 nullable run/실행 실패 타입 |
| src/App.tsx, src/store/productionStore.ts | #forecast 진입·새로고침·hashchange 복원. 결과는 로컬에 저장하지 않음 |
| src/layouts/MainLayout.tsx, src/screens/DataUploadScreen.tsx | 실제 예측 진입 및 별도 계획 Mock 표시 |
| src/services/fixtures.ts | 사용하지 않는 예측 Fixture import/export 제거 |
| tests/run_postgres_e2e.py, tests/e2e/registration.spec.ts | 합성 과거 수요 생성·승인·실제 예측·상태/재시작 시험 확장 |
| README.md | 실제 API 상태와 남은 Mock 안내 |

## 남은 Mock과 다음 단계

계획 비교·진단·계획 승인 메모리 기록 및 예시 계획 JSON 기반 DEMO Excel은 유지했다. 실제 예측 결과와 연결된 것처럼 표시하지 않는다.
다음은 계획 엔진의 계약·업무 규칙 확정과 실제 예측 실행 ID를 참조하는 계획 입력/계산/저장이다. 인증·권한, 기업 데이터 품질 및 예측 성능 평가는 별도 과제다.
동기 예측 API이므로 비동기 작업·취소·진행률·대규모 실행 운영 검증도 이번 완료 범위에 포함하지 않는다.
