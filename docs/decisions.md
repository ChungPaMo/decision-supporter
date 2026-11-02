# 기술 결정 기록

## D-001 DB: PostgreSQL 16 (Docker Compose) 채택 — 2026-10-30

| 후보 | 장점 | 단점 |
|---|---|---|
| SQLite (PoC 사용) | 설치 불필요, 파일 1개 | 동시 접속 약함, FK 기본 OFF·`date('2026-02-30')` 보정 같은 PoC 실측 함정(E05), 활성 BOM 기간 겹침을 제약으로 못 막음, 팀원 간 공유 DB 불가 |
| **PostgreSQL** | NUMERIC 정밀도, `EXCLUDE` 로 BOM 기간 겹침 차단, 부분 유니크 인덱스(중복 업로드), 트리거·뷰로 승인 게이트, 정식 마이그레이션 생태계 | 서버가 필요 → Docker Compose 로 격리 |

**결정**: 기본 DB는 PostgreSQL. PoC에서 SQLite가 쓰인 것은 설치 부담을 피한 PoC 편의였고, MVP 요구(동시 사용, 마이그레이션, 제약 표현력)에는 PostgreSQL이 맞다. 제약: 로컬 PC에 DB를 직접 설치하지 않고 **Docker Compose 전용 컨테이너**만 사용(프로젝트 `chungpamo-mvp`, 전용 볼륨·네트워크, 호스트 노출은 127.0.0.1 한정).

**이미지 `postgres:16.6`**: 16 계열은 장기 지원 안정판이며 패치 버전까지 고정해 팀원 간 재현성을 확보한다. Debian 기반을 택한 이유는 contrib(`btree_gist`)·로케일이 기본 포함되기 때문이다. 17 은 아직 이 팀의 검증 이력이 없어 선택하지 않았다.
**리스크**: Docker Desktop 설치가 팀원 PC 요건이 된다(Windows: WSL2). 이 작업 PC에는 Docker 가 없어 컨테이너 기동 검증을 못 했다(README 참고).

## D-002 ORM·마이그레이션: SQLAlchemy 2 + Alembic + psycopg 3

- SQLAlchemy: 모델이 계약·마이그레이션의 기준이 되고 PostgreSQL 전용 기능(ExcludeConstraint, 부분 인덱스)을 지원.
- Alembic: 스키마 변경을 버전 파일로 추적. 초기 마이그레이션(`0001`)은 모델에서 생성한 DDL을 **고정 문자열로 박아** 모델이 바뀌어도 과거 마이그레이션이 변하지 않게 했다. 트리거·뷰·시드는 수기 작성.
- psycopg 3 (`psycopg[binary]`): SQLAlchemy 2 권장 드라이버, Windows 에서 컴파일러 불필요.
- Pydantic 은 FastAPI 의존성으로 이미 들어오며 계약 모델(`app/contracts.py`)에 사용. PoC 교훈에 따라 float·NaN 거부, 불명 필드 거부(`extra="forbid"`).

## D-003 의존성 최소화

추가한 직접 의존성: fastapi, uvicorn, sqlalchemy, alembic, psycopg, pydantic-settings, pytest, httpx(TestClient 필요). 이유는 `backend/requirements.txt` 주석. 파서용 openpyxl 등은 업로드 API 단계에서 추가(아직 사용하지 않으므로 넣지 않음).

## D-004 수량 저장 방식 변경 (PoC → MVP)

PoC SQLite 는 수량을 TEXT 10진 문자열로 저장했다(REAL 변환 시 판정 차이, E05). PostgreSQL 은 `NUMERIC(18,4)` 로 같은 정밀도를 보장하므로 TEXT 를 버린다.

## D-005 PoC 에서 가져온 것과 가져오지 않은 것

| 가져옴(개념·규칙 재작성) | 근거 파일 |
|---|---|
| staging→승인→본 테이블 + DB 이중 안전장치 | e09 §2 "채택", e10_data_contract §0, e10_mock_erp.sql 트리거 |
| 승인 없는 쓰기 차단, 승인 후 불변, 검증 ERROR 있으면 승인 불가 | e10_mock_erp.sql `trg_no_approve_with_errors`, `trg_decision_immutable` |
| 코드·품명 충돌은 덮어쓰지 않고 차단, 자동 승인 기본 OFF | e10_m13_fix_report |
| 재반입(중복 업로드) 탐지: 원본 sha256 | e10_data_contract §0, ImportBatch |
| 단위 dimension·표준 단위 환산 개념 | src/normalization/values.py (코드는 업로드 단계에서 재사용 예정) |
| 엔터티·필드 이름(제품·자재·BOM·재고·자원), BOM 활성/기간 제약 | e10_mock_erp.sql, e11_scenario_contract.json |
| 미승인·미확인 데이터는 FEASIBLE 로 취급하지 않는다는 원칙 | e11_scenario_contract |

| 가져오지 않음 | 이유 |
|---|---|
| `mvp/` 초안, SQLite DDL 그대로 | 요청에 따라 자동 복사 안 함. DB 방언이 달라 새로 작성 |
| 문서 파서(pdf/docx/pptx), RDF/SHACL, Upstage | MVP 범위(CSV·Excel) 밖, 보류 판정 |
| `src/parsers/tabular.py`, `src/normalization/values.py` | **다음 단계(업로드 API)에서 선별 재사용**. 이번 범위에 파싱이 없어 아직 복사하지 않음 |
| `scripts/e05_validate_python.py` 의 규칙 | 업로드·검증 단계에서 규칙별로 옮김(41개 규칙 전체가 필요한지 판단 후) |

참고로 읽은 기존 자료: CLAUDE.md, poc_personal_final_summary, e09, e10_data_contract, e10_m13_fix_report, e10_mock_erp.sql(앞 230줄), e11_scenario_contract.json, tabular.py, values.py, e05_validate_python.py(앞 120줄). `src/validation/` 은 `.gitkeep` 만 있어 코드가 없었다. `scripts/e10_mock_erp.py`와 `tests/test_e10_mock_erp.py` 는 이번 범위(승인 게이트는 DB 트리거로 구현)에서 열어보지 않았고, 승인 로직을 애플리케이션 계층으로 옮기는 업로드/승인 API 단계에서 읽는다.

## D-006 CSV/XLSX 파싱·매핑·검증 모듈 (DB 비의존) — 2026-10-30

- **의존성 추가**: `openpyxl`(PoC E02 에서 검증한 XLSX 라이브러리), `python-multipart`(FastAPI `UploadFile` 필수). pandas 는 추가하지 않음(PoC D-016 보류 유지, 표준 csv 로 충분).
- **PoC 에서 가져온 것**: `src/parsers/tabular.py` 의 인코딩 판별(BOM→UTF-8→CP949)과 XLSX 1행 병합 헤더 → 2단 헤더 복원, `src/normalization/values.py` 의 숫자 정규식(`\d` → `[0-9]` 로 바꿔 전각 숫자 거부), `scripts/e10_mock_erp.py` 의 M-24/M-20(같은 코드·다른 값은 덮어쓰지 않고 ERROR) 규칙, E10 M13 교훈(코드·품명 충돌 시 한쪽을 고르지 않음).
- **PoC 와 달라진 것**: ① 구분자 판별을 "모든 행 열 수 동일"에서 "헤더 줄 열 수 최대"로 바꾸고, 열 수가 어긋난 행은 `ROW_COLUMN_COUNT` 로 행 위치와 함께 보고(파일 전체를 거부하지 않음). ② 업체별 설정 JSON 대신 별칭 사전(`ingest/mapping.py`). ③ 날짜 셀은 ISO 문자열로, 숫자 셀은 지수 없는 10진 문자열로 보존. ④ 파일 경로 대신 바이트를 받아 메모리에서만 처리(원본 수정 불가).
- **가져오지 않은 것**: 업체 A/B 코드 대응표와 `vendor_parser.py`(E01 합성 데이터 전용 설정, 낯선 업체에는 일반화되지 않음 — PoC 한계 문서 참고), 채점 코드(`e02_grade.py`), 성능 측정 하네스(`e02_run.py`, `e02_parse_one.py`). `scripts/e05_validate_python.py` 의 41개 규칙은 전부 옮기지 않았다 — 이번 엔터티(월별 수요)에 해당하는 형식·범위·중복 규칙만 새로 작성. 참조 무결성(R01~)·BOM 규칙은 해당 엔터티를 업로드할 때 옮긴다.
- **매핑 방식**: 정규화한 헤더가 별칭과 정확히 일치할 때만 매핑. 점수는 만들지 않는다(PoC: 추정 신뢰도 금지). 강한 별칭은 확정, 약한 별칭(`qty`, `수량`, `code` 등)은 매핑하되 `review_required`. 필수 필드 누락·후보 2개 이상은 `HOLD` 로 검증을 건너뛰고 사용자가 열을 지정(`column_overrides`)해야 한다. 검색기(별도 담당)와 무관한 결정적 규칙이다.
- **자동 승인 없음**: 응답에 `approval_required: true`, `auto_approve: false` 고정. 승인·저장 연결은 Docker 개발 PC 에서 DB 검증 후.

## D-007 승인 흐름 설계: 임시 보관, 승인 서비스, DB 게이트 보강 — 2026-10-30

**승인 게이트 분석 (0001 → 허점 7개)**: ① REJECTED 를 다시 APPROVED 로 바꿀 수 있었음 ② 배치를 처음부터 APPROVED 로 INSERT 가능 ③ 승인된 배치에 나중에 행 추가·수정 가능 ④ 업무 행 DELETE 미차단 ⑤ 승인 커밋에 행이 실제로 들어갔는지 DB 가 확인 안 함 ⑥ 상태값에 매핑 보류·오류 구분 없음, 컬럼 길이 12 ⑦ 승인 전 원본·설정을 둘 곳이 없음. → 마이그레이션 `0002` 로 보강(작성만, 어떤 DB 에도 미적용). 0001 은 이미 공유되었으므로 수정하지 않고 새 마이그레이션으로 처리.

**임시 데이터 = DB staging (같은 PostgreSQL), 별도 저장 계층 아님.** 대안(파일 시스템/오브젝트 스토어에 원본 보관)은 승인과 원본 조회가 한 트랜잭션에 묶이지 않고 불변성(append-only) 강제가 약하다. 업로드 한도가 10MB 라 `bytea` 로 충분하다. 저장하는 것은 원본 바이트 + 설정 3종뿐이고, **파싱된 레코드는 저장하지 않는다**: 파싱·검증은 결정적이라 재계산하면 같은 결과가 나오며, 승인 시 원본을 다시 검증하므로 "화면 캐시와 승인 데이터가 어긋나는" 경로가 없다. 규칙이 바뀌어 재계산 결과가 달라지면 `RESULT_CHANGED` 로 멈추고 재검증을 요구한다. 화면 표시용 요약(`validation_result`, 레코드 앞 100건)만 캐시한다.

**승인 = 한 트랜잭션**: 배치 행 잠금(FOR UPDATE) → 상태·승인자·`result_digest` 확인 → 원본 재검증 → 제품 마스터·키 중복 확인 → 배치 APPROVED → 업무 행 INSERT → (커밋 시) DB 가 행 수 대조. 어느 단계든 예외면 전체 롤백. `approved_at` 을 트랜잭션 시작 시각(`now()`)으로 두어 "승인한 그 트랜잭션에서만" 쓰기를 허용한다 — 앱이 다른 연결로 나중에 행을 끼워 넣어도 DB 가 거부한다.

**의도적으로 하지 않은 것**: 같은 키 덮어쓰기(UPDATE 승인)는 미지원(`KEYS_ALREADY_APPROVED`) — 갱신 정책(이전 값 보존, 변경 승인)은 도메인 합의 후. 승인자 인증/권한은 미구현(역할 코드 문자열만 요구하며 위조 방지가 아님). 셀 단위 편집 API 없음(열 지정·재업로드만).

**저장소 인터페이스**: `BatchRepository`/`BatchTx`(Protocol). 구현은 `SqlBatchRepository`(미검증)와 테스트 전용 `tests/fakes.py::InMemoryBatchRepository`. Fake 는 서비스 로직 검증용이며 DB 트리거·잠금·제약 동작을 증명하지 않는다. 설정 `PERSISTENCE=disabled`(기본)에서는 `/batches*` 가 501 을 돌려 영구 저장이 되는 척하지 않는다.

**Docker 개발 PC 에서 검증할 것**: `alembic upgrade head`(0002 포함) 적용, `tests/test_db_rules.py`, `tests/test_sql_repository.py`, 0001 → 0002 업그레이드·다운그레이드 경로, 동시 승인(두 연결) 시 한 쪽만 성공하는지.

## D-008 제품 마스터를 배치 entity 로 처리 — 2026-11-02

**별도 API 없이 `entity=product`**: 업로드·재검증·승인·반려와 승인 게이트가 entity 와 무관하므로 `/batches` 를 재사용한다. 추가한 경로는 조회용 `GET /products` 하나다. 매핑은 `ENTITY_FIELDS`, 검증은 `VALIDATORS` 로 entity 별 분기하고 코드 보존·충돌·단위·수량 규칙은 월별 수요와 같은 함수를 쓴다.

**미등록 제품은 승인 때가 아니라 검증 때 잡는다 (D-007 변경)**: 업로드·재검증·승인 때마다 승인된 제품 목록을 검증에 넘긴다. 승인 시점 오류였던 `UNKNOWN_PRODUCTS` 는 없앴다: 셀 위치가 없고, 재검증으로 풀 수 없었으며, 같은 트랜잭션의 재검증과 겹쳐 도달할 수 없는 코드가 되기 때문이다. 이제 `PRODUCT_NOT_REGISTERED` 검증 오류이고 제품 승인 후 `revalidate` 하면 풀린다. FK 는 유지해 최종 방어선으로 둔다. 저장 없는 `/ingest/preview` 는 기존 제품과 대조하지 않으며 `master_data_checked: false` 로 표시한다.

**덮어쓰기 없음**: 같은 코드에 다른 값은 `EXISTING_PRODUCT_CONFLICT`. 값이 같으면 건너뛰고 경고한다. 제품 정보 변경(변경 이력, 승인 절차)은 도메인 합의 후 별도 설계한다. 파일 안의 제품코드 중복은 값이 같아도 오류다(월별 수요는 같은 값이면 첫 행 사용) — 마스터는 제품당 한 행이어야 하고 어느 행이 맞는지 고르지 않는다.

**값을 지어내지 않는다**: `product_type` 은 계약상 선택인데 DB 가 NOT NULL 이었다. 기본값을 넣으면 없는 사실을 만드는 것이므로 `0003` 에서 NULL 허용으로 바꿨다. `safety_stock_qty` 는 기존 계약(기본 0)을 따르되 0 으로 채웠음을 경고한다.

**마이그레이션 0003 필요** (작성만, 미적용): `ck_batch_entity` 확장, 커밋 시 행 수 대조에 product 추가, `product_type` NULL 허용, 중복 업로드 기준을 (entity, sha)로 변경(한 통합문서의 제품·수요 시트를 각각 업로드 가능), 배치 entity 와 대상 테이블 일치 강제. 0002 도 미적용이지만 원격에 없는 작업분이라도 단계 구분을 유지하려고 수정하지 않고 새 파일로 추가했다.

**알려진 한계**: 같은 제품을 등록하는 두 배치를 동시에 승인하면 진 쪽은 PK 위반으로 롤백되어 `PERSISTENCE_ERROR`(500)가 된다. 데이터는 안전하지만 메시지가 친절하지 않다. 승인할 때마다 제품 전체를 읽으므로 제품 수가 매우 많아지면 조회 범위를 줄여야 한다(미측정).
