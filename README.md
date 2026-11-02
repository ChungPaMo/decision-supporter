# decision-supporter — 충파모 과제 7 MVP

제조 중소기업의 수요 데이터(CSV·Excel)를 올려 검증·승인하고, 수요예측과 제약조건 기반 생산계획안을 비교해 의사결정을 돕는 MVP입니다.

> 이 저장소는 MVP 개발용입니다. 사전 기술검증(E00~E12)은 별도 저장소 `chungpamo-ai-poc` 에 있으며, 이 저장소와 코드·이력을 공유하지 않습니다. 기존 PoC에서는 개념·규칙만 가져와 새로 작성했습니다 ([docs/decisions.md](docs/decisions.md) D-005, D-006).

## 전체 MVP 목표

CSV·Excel 업로드 → 파싱 → 필드 매핑 → 정합성 검증 → 사용자 승인 → PostgreSQL 저장 → 수요예측(P50/P80/P90) → 제약조건 기반 계획안 2~3개 → 웹 화면 비교 → 승인·Excel 출력

> 프론트엔드 담당은 [docs/frontend_guide.md](docs/frontend_guide.md) 부터 읽어 주세요.

## 현재 상태 (정직한 구분)

| 구분 | 항목 | 상태 |
|---|---|---|
| ✅ 구현 + 자동 테스트 확인(DB 불필요) | FastAPI `/health` (DB 없을 때 503 경로) | 확인 |
| ✅ | CSV/XLSX 파싱(인코딩·구분자·병합 헤더·원본 해시), 별칭 기반 필드 매핑, 월별 수요·제품·자재·BOM·재고 검증(코드↔품명 충돌, 선행 0, 날짜·수량·단위, 중복, 참조, BOM 버전·기간·순환, 재고 할당 초과, 매핑 보류), `POST /ingest/preview` | 확인 |
| ✅ | 데이터 계약 v1 (Pydantic) ↔ JSON Schema 일치, 계약 ↔ DB 열 이름 일치 | 확인 |
| ✅ (인메모리 저장소로만 검증) | 배치 승인 서비스(월별 수요·제품·자재·BOM·재고): 원본 보관·검증·열 지정 재검증·명시적 승인/반려·중복 승인 방지·저장 실패 시 롤백·승인 데이터만 조회, 승인된 마스터 기준 대조(미등록 품목은 셀 위치와 함께 오류, 마스터 승인 후 재검증). API `/batches*`, `/products`, `/materials`, `/boms`, `/inventory`, `/demand/monthly` ([docs/api_contract_batches.md](docs/api_contract_batches.md)) | 서비스 로직만 확인. 기본 설정(`PERSISTENCE=disabled`)에서는 이 API 가 **501** 을 돌려주며 영구 저장은 동작하지 않음 |
| ✅ (DEMO/UNVERIFIED 전용) | 계획 결과 JSON → XLSX 5시트 출력 `POST /exports/plans/xlsx` ([docs/api_contract_exports.md](docs/api_contract_exports.md)). 합성 fixture 로만 검증했고 입력 계약은 초안 | 제출된 JSON 을 그대로 표시할 뿐이며 공식 문서가 아님. 실제 Excel 프로그램으로 열어 본 확인은 없음(openpyxl 로 다시 읽어 검증) |
| ⚠ 작성만 함, **미검증** | PostgreSQL 스키마·Alembic 마이그레이션 `0001`, `0002`(승인 게이트 보강: 상태 전이, 승인 트랜잭션 한정 쓰기, 커밋 시 행 수 대조, append-only 원본 보관), `0003`(제품 배치, entity↔테이블 일치 강제), `0004`(자재·BOM·재고 배치, 재고 품목 존재 트리거), `SqlBatchRepository`, `compose.yaml`, `db/init` | 개발 PC에 Docker가 없어 실행하지 못함. `tests/test_db_rules.py`·`tests/test_sql_repository.py`·`/health` 정상 경로는 DB가 없으면 **skip** |
| ❌ 미구현 | 생산능력·주문 업로드, 승인자 인증/권한, 같은 키 덮어쓰기·마스터 정보 변경, 승인된 BOM 의 종료일 변경, 셀 단위 편집, 수요예측, 계획안 생성·저장, 승인된 계획의 정식 Excel 출력, 검색기, 프론트엔드 | |

최근 실행 결과(개발 PC, DB 없음): `379 passed, 31 skipped`. skip 은 모두 DB 가 필요한 테스트이며 **미검증**이다.

## 담당 영역

| 영역 | 내용 |
|---|---|
| 백엔드·데이터 | 전체 기술 흐름, 데이터 파싱·검증·DB, 백엔드 API, 프론트엔드 API 연결, Excel 출력 |
| 검색기·도메인 | 검색기 성능 개선·최적화·검증, 도메인 지식 연결 |
| 예측·최적화 | 월별 수요예측(P50/P80/P90), 제약조건 기반 계획안 생성 |
| 업무 규칙·UX·프론트엔드 | 업무 규칙·KPI·판단 규칙, UX 설계, React 프론트엔드 |

## 구조

```
compose.yaml           전용 PostgreSQL 컨테이너 (Compose 프로젝트명 chungpamo-mvp)
db/init/               컨테이너 최초 기동 시 앱 전용 역할·DB 생성
backend/app/           FastAPI (api/), 설정, SQLAlchemy 모델, 데이터 계약(contracts.py)
backend/app/ingest/    파싱·매핑·검증 (DB 비의존)
backend/app/exports/   계획 결과 XLSX 출력 (DEMO/UNVERIFIED)
backend/app/batches/   배치 승인 서비스, 저장소 인터페이스, SQL 구현(미검증)
backend/migrations/    Alembic (0001 초기 스키마, 0002 승인 게이트, 0003 제품 배치, 0004 자재·BOM·재고 배치)
backend/tests/         pytest
contracts/             데이터 계약·출력 계약 초안 JSON Schema (backend/export_contract_schema.py 로 생성), openapi.json·examples/responses (backend/generate_api_examples.py 로 생성), examples/ 합성 샘플
docs/                  데이터 계약 v1, 기술 결정 기록
```

## Python 개발환경 (Windows PowerShell)

```powershell
python -m venv .venv                     # Python 3.11
.\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt
Copy-Item .env.example .env              # 값은 개발용 자리표시자. .env 는 Git 에 올리지 않는다
```

## 실행·테스트 (DB 없이 가능)

```powershell
cd backend
..\.venv\Scripts\python.exe -m pytest -q
..\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

- 배치 저장·승인 API 는 `PERSISTENCE=postgres`(.env) 일 때만 동작하며, 이 경로는 Docker 개발 PC 에서 검증하기 전까지 미검증이다. 계약: [docs/api_contract_batches.md](docs/api_contract_batches.md)
- 업로드 미리보기 (저장·승인 없음, 기존 제품 대조 없음): `curl.exe -F "file=@수요.csv" http://127.0.0.1:8000/ingest/preview` (다른 데이터는 `-F "entity=product"` 처럼 `product`, `material`, `bom`, `inventory` 지정)
- 응답에는 항상 `approval_required: true`, `auto_approve: false` 가 들어가며, 오류·매핑 보류 위치는 시트·행·열·셀 주소로 반환됩니다. 인터랙티브 문서: `http://127.0.0.1:8000/docs`
- DB가 없으면 `/health` 는 503 + `degraded` 입니다.

## Docker Compose / PostgreSQL (Docker 가 설치된 별도 PC 에서)

격리 원칙: DB 는 Compose 프로젝트 `chungpamo-mvp` 의 컨테이너에서만 실행하고, 호스트에는 `127.0.0.1:55432` 로만 노출합니다(충돌 시 `.env` 의 `DB_HOST_PORT`). 이 프로젝트에서 `docker compose down -v`, `docker system prune`, `docker volume rm` 은 사용하지 않습니다.

```powershell
Copy-Item .env.example .env              # 비밀번호를 개발용 임의 값으로 바꾼다 (DATABASE_URL 도 같이)
docker compose up -d db                  # healthy 가 될 때까지 대기: docker compose ps
cd backend
..\.venv\Scripts\python.exe -m alembic upgrade head      # 개발 DB 에 마이그레이션 (0001~0004)
..\.venv\Scripts\python.exe -m pytest -q                 # 이때 skip 이 0 이어야 DB 검증 완료
docker compose stop                      # 정지 (데이터 유지)
```

### DB 테스트가 아직 미검증인 이유

초기 개발 PC 에 Docker 가 없어 컨테이너 기동, healthcheck, 마이그레이션 적용, 승인 트리거·EXCLUDE 제약·예측 입력 뷰 동작을 실행해 보지 못했습니다. 위 순서로 실행해 skip 이 사라지고 모두 통과하는 것을 확인하기 전까지는 **동작하는 것으로 간주하지 마세요.** 실패하면 이슈로 남겨 주세요.

## 데이터 계약

- 사람이 읽는 문서: [docs/data_contract_v1.md](docs/data_contract_v1.md) (미확정 항목 ⚠ 포함)
- 코드: [backend/app/contracts.py](backend/app/contracts.py), 기계 판독용: [contracts/data_contract_v1.schema.json](contracts/data_contract_v1.schema.json)
- 계약이나 API 를 바꾸면 `cd backend; ..\.venv\Scripts\python.exe export_contract_schema.py; ..\.venv\Scripts\python.exe generate_api_examples.py` 로 생성물을 갱신합니다 (테스트가 동기화를 검사).

## 이후 통합 방식 (계획)

- **프론트엔드(React)**: [docs/api_contract_batches.md](docs/api_contract_batches.md) 와 OpenAPI(`/openapi.json`)를 사용합니다. 제품·자재를 먼저 승인한 뒤 BOM·재고·월별 수요를 승인하는 순서입니다. 매핑 보류(HOLD) 시 사용자가 열을 선택해 `column_overrides` 로 다시 호출하는 흐름입니다.
- **Excel 출력**: 계획 엔진 결과를 [출력 계약 초안](docs/api_contract_exports.md)의 JSON 으로 내면 XLSX 가 됩니다. 저장된 계획안의 승인을 확인하는 정식 출력은 아직 없습니다.
- **예측 모듈**: 입력은 DB 뷰 `v_forecast_input_monthly_demand`(승인되고 검증 오류가 0인 배치만 노출), 출력은 계약의 `ForecastRow`(`demand_forecast` 테이블)로 맞춥니다. `horizon`·`p50_method` 정의는 예측 담당 확인이 필요합니다.
- **검색기**: 현재 매핑은 결정적 별칭 규칙뿐입니다. 검색기는 같은 입력(`MappingHold` 후보)에 후보를 제안하는 방식으로 붙이되, 승인 전에는 어떤 값도 DB 에 쓰지 않는 원칙은 유지합니다.

## 보안·데이터 원칙

- `.env`, 업로드 원본, 기업 데이터, DB 파일/덤프는 Git 에 올리지 않습니다 ([.gitignore](.gitignore)). `.env.example` 은 자리표시자만 담습니다.
- 공개 저장소입니다. 실제 기업 데이터·개인정보·비밀값을 커밋하지 마세요.
