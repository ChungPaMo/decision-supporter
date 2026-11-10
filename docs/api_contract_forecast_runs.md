# 승인 수요 예측 실행 API — forecast-run-0.1

`PERSISTENCE=postgres`, Alembic **0005**가 필요하다. `/dev/forecasts`는 기존 합성 JSON 전용 API로 유지한다.
정식 API는 클라이언트의 rows/batch_id를 받지 않는다. 서버가 승인 수요 뷰를 직접 조회한다.

## API

| 메서드 | 경로 | 응답 |
|---|---|---|
| POST | `/forecasts` | `{execution: ForecastExecution, reused: boolean}` |
| GET | `/forecasts?limit=20&offset=0` | `{items: ForecastExecutionSummary[], total, limit, offset}`. limit 1~100 |
| GET | `/forecasts/{execution_id}` | ForecastExecution. UUID 기준이며 엔진의 FRUN 문자열과 구분 |

요청 예시(서버 수요 조회 조건이며 입력 행은 전송하지 않는다):

```json
{"trained_until":"2016-12-31","horizon_months":3,"p50_method":"MA12","min_history_months":12,"random_seed":42}
```

선택적 `contract_version`은 `forecast-run-0.1`. 기준일 연도 ≤ 9998, horizon 1~12, P50는 MA12/LGBM_L1,
최소 이력 12~120, 시드 0~2147483647. 기본값은 3/MA12/12/42다. 다른 키는 422.
기준일은 월말이어야 한다. 4~12개월 예측은 엔진의 검증 범위 경고와 함께 지원한다.

## 실행·실패 HTTP 의미

- **201**: 새 이력 저장 완료. status를 반드시 확인한다. DATA_INSUFFICIENT는 예측 성공이 아니다.
- **200**: 동일 완료 실행을 재사용. reused=true, 원래 execution_id/시각/결과를 반환한다.
- **422 + execution**: 엔진 INPUT_INVALID 이력 저장. result.run=null, result.errors[]를 확인한다.
- **422 + detail**: 요청 스키마 오류. 실행 이력을 만들지 않는다.
- **500/503 + execution**: FAILED 이력 저장. result=null, failure={code,message}. 모델 실행 오류는 500, 의존성 누락은 503.
- **409 + detail.code=FORECAST_IN_PROGRESS**: 같은 입력을 다른 요청이 계산 중. 자동 중복 실행하지 말고 이력을 다시 조회한다.
- **501 PERSISTENCE_DISABLED**, **503 DB_UNAVAILABLE**: 저장 비활성/접속 실패. 결과를 생성한 것처럼 처리하지 않는다.
- **500 PERSISTENCE_ERROR**: 저장 트랜잭션 전체 롤백. 해당 실행/배치 참조/예측 행은 모두 남지 않는다.
- **404 FORECAST_NOT_FOUND**: 해당 UUID의 실행 없음.

execution은 status, execution_id, parameters, input_digest, dedup_key, runtime_identity, quantile_model_version,
input_row_count/forecast_count, created_at/finished_at, input_batches, result, failure를 제공한다.
result는 엔진 응답 전체(run, forecasts, skipped, adjustments, warnings, errors)다.
OK는 전 제품 예측, PARTIAL은 일부 제외, DATA_INSUFFICIENT는 예측 0행이다. NaN/비정상 분위수 모델 출력도 FAILED로 처리한다.

## 입력 추적과 중복 정책

1. `v_forecast_input_monthly_demand`와 승인 배치를 한 SELECT로 읽는다. APPROVED·오류 0·monthly_demand 배치 및 기준일까지의 월만 포함한다.
2. 제품/월로 정렬하고 Decimal을 10진 문자열로 보존한다. 기본값까지 채운 **정확히 같은 JSON**을 엔진과 저장 스냅샷에 사용한다.
3. input_digest는 request_id를 제외한 JSON의 sha256이다. 엔진이 반환한 input_digest와 일치해야 한다.
4. 입력 배치 UUID·승인 버전·원본 SHA256·검증 digest·승인 시각을 고정한다. DB 트리거가 현재 승인 기록과 일치하는지 확인한다.
5. dedup_key는 입력 digest + 배치 출처 스냅샷 + 엔진 버전/의존성 버전/엔진 코드 파일 SHA256으로 만든다.
6. OK/PARTIAL/DATA_INSUFFICIENT/INPUT_INVALID는 동일 키를 재사용한다. FAILED는 매번 새 실행으로 재시도한다.
7. 키별 PostgreSQL transaction advisory lock으로 동시 계산을 막고 partial unique index로 완료 이력 중복을 추가 차단한다.

실행은 동기 방식이며 계산 중 DB 트랜잭션을 유지한다. 프로세스 중단·DB 저장 실패는 롤백되고 미완성 성공 기록을 남기지 않는다.
비동기 작업 큐나 장시간 실행의 취소·진행률은 이번 범위가 아니다. 다른 입력은 별도로 실행할 수 있다.

## 저장 구조 / 마이그레이션

- **0001~0004 변경 없음. 0005만 추가.** 원본 수요·승인 기록을 변경하지 않는다.
- forecast_run: 실행 설정, 비공개 입력 JSON 스냅샷, 입력/런타임 지문, 전체 엔진 결과·실패, 행 수·시각.
- forecast_run_input: 실행 FK + 승인 배치 FK, 승인 당시 버전/지문. 미승인·다른 entity·변경된 출처 참조를 트리거가 거부한다.
- demand_forecast.forecast_run_id: 새 실행 FK. 새 예측 행은 실행별로 제품/월/horizon이 유일하다.
- 기존 예측 행은 삭제하지 않고 FK=NULL인 legacy로 보존한다. 기존 유일 키는 legacy 행에만 partial index로 유지한다.
- legacy 행을 새 실행 이력으로 위장하지 않는다. 실행 이력이 있으면 0005 downgrade는 데이터 삭제 대신 실패한다.

입력 JSON 전체는 DB에 보존하고 일반 이력 API는 출처·지문·실행 조건만 제공한다. 복원 검증은 테스트 DB에서 저장 JSON으로 엔진을 재실행해 비교한다.
OpenAPI와 `contracts/forecast_run_v1.schema.json`은 생성 스크립트와 동기화 테스트로 관리한다.
