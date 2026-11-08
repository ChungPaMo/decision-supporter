# engine — 예측·계획 엔진

DB에 접근하지 않는 순수 Python 모듈이다 (입력 JSON → 출력 JSON). 계약·계산 규칙: [docs/engine_forecast_plan_contract.md](../docs/engine_forecast_plan_contract.md)

| 모듈 | 상태 |
|---|---|
| `engine.forecast` 월별 수요예측 P50/P80/P90 | 구현 (계약 §3, §6.1) |
| `engine.plan` 3개 계획안 | 미구현 (계약 §4, §6.2~) |

## 설치 (백엔드와 별도 가상환경)

```powershell
python -m venv .venv-engine                      # Python 3.11
.\.venv-engine\Scripts\python.exe -m pip install -r engine\requirements.txt
```

## 사용 (저장소 루트에서)

```powershell
# 예측: forecast-input JSON → forecast-output JSON
python -m engine.forecast --input contracts\examples\engine\forecast_request.json --output out.json

# 테스트
python -m pytest engine\tests -p no:cacheprovider

# 계약 예시 다시 만들기 (합성 데이터)
python -m engine.tools.make_forecast_examples
```

## 공개 데이터로 실행·백테스트

Kaggle "Forecasts for Product Demand"(felixzhao, **GPL-2.0**) 의 `Historical Product Demand.csv` 를 받아 **저장소 밖**에 둔다.
가공본도 GPL-2.0 이므로 저장소에 커밋하지 않는다.

```powershell
# 원본 → 예측 요청 JSON (상위 100제품, 2012-01~2016-12, trained_until 2016-12-31)
python -m engine.datasets.kaggle_product_demand --csv "..\data_raw\Historical Product Demand.csv" --output ..\data_raw\processed\request.json

# 2017-01~03 예측 (약 7초)
python -m engine.forecast --input ..\data_raw\processed\request.json --output ..\data_raw\processed\forecast.json

# rolling-origin 백테스트 (검증 2015 / 테스트 2016, 약 1분)
python -m engine.backtest --input ..\data_raw\processed\request.json --output ..\data_raw\processed\backtest.json
```

원본 파일 SHA-256: `114dfeb43a971d3e0efa7c941fc3614116fce101456708e0ce909989cc509de3` (1,048,575행).
