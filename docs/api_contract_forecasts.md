# 개발용 예측 API

`POST /dev/forecasts`는 `forecast-input-0.1-draft` 합성 JSON을 `engine.forecast.run`에 전달한다.
DB 조회·저장·배치 승인 확인은 없다. 실제 승인 수요 기반 예측 경로가 아니다.

- 엔진 요청 모델을 상속하며 `source_type=SYNTHETIC`만 허용한다. batch_id는 입력 표식이다.
- 수량은 ASCII 10진 문자열(정수 최대 14자리, 소수 최대 4자리). JSON 숫자·지수 표기 불가.
- 날짜/기간 연도는 0001~9998. 기준일은 월말, horizon은 1~12. 4~12는 미검증 범위 경고와 함께 허용한다.
- `p50_method=MA12|LGBM_L1`, `missing_month_policy=ZERO_FILL`, 분위수는 `[P50,P80,P90]` 고정.
- 원래 엔진 입력 계약과 달리 공개/기업 source_type은 이 개발 경로에서 거부한다. 모델 계산식은 변경하지 않았다.

요청/응답 예시는 `contracts/examples/engine/forecast_request.json`, `forecast_response.json`이다.
실제 API 응답은 같은 구조이며 `generated_at`은 실행 시각이다. 결과의 run, forecasts, skipped,
adjustments(before/after 포함), warnings(제품/월/학습행 메타 포함), errors를 보존한다.

| HTTP | 응답 |
|---|---|
| 200 | OK, PARTIAL 또는 DATA_INSUFFICIENT. 부족한 이력/학습 행도 정상 도메인 결과다 |
| 422 | 스키마 오류는 `{detail:{code:REQUEST_INVALID,message,details}}` |
| 422 | 엔진의 월말·중복·미래 실적·분위수 판정 오류는 `status=INPUT_INVALID`, `run=null`, `errors[]`인 예측 응답 |
| 500 | `{detail:{code:FORECAST_ENGINE_ERROR,message}}` |
| 503 | `{detail:{code:FORECAST_ENGINE_UNAVAILABLE,message}}` |

프론트엔드는 HTTP 상태와 응답 모양을 모두 구분하고 run=null과 예측 0행을 처리해야 한다.
DATA_INSUFFICIENT를 임의의 성공 예측이나 0 수요로 대체하지 않는다.

기계 판독 계약: `contracts/openapi.json`, `contracts/dev_forecast_draft.schema.json`.

저장소 루트에서 통합 의존성을 설치하고 backend에서 실행한다.

```powershell
.\.venv\Scripts\python.exe -m pip install -r backend/requirements-forecast.txt
cd backend
..\.venv\Scripts\python.exe generate_api_examples.py
..\.venv\Scripts\python.exe export_contract_schema.py
..\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

생성 스크립트는 API·모델에서 계약을 만든다. 테스트가 파일 동기화를 검사한다.
