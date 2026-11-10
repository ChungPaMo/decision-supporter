# PostgreSQL 통합 검증 — 2026-11-10

실제 PostgreSQL 검증 결과다. 합성 데이터로만 시험했으며 기업 운영 환경 검증은 아니다.
중단된 예측 API 변경은 작업 시작 시 이미 미커밋 상태였고 보존했다.

## 전용 환경과 데이터 안전성

- Docker Engine 29.4.0, Compose v5.1.2, `postgres:16.6` (PostgreSQL 16.6 Debian).
- 프로젝트 `chungpamo-mvp`, 컨테이너 `chungpamo-mvp-db`.
- 새 볼륨 `chungpamo-mvp-pgdata`, 새 네트워크 `chungpamo-mvp-net`.
- 시작 전 동일 이름의 자원 없음, 55432 리스너 없음, PostgreSQL Windows 서비스 없음 확인.
- 호스트 바인딩 `127.0.0.1:55432` → 컨테이너 5432. 초기화 스크립트 마운트는 읽기 전용.
- 루트 `.env`를 새로 생성하고 관리자/앱 비밀번호를 각각 임의 생성했다. Git ignore 확인, 비밀값은 보고서에 기록하지 않는다.
- 앱 역할 `chungpamo_app`: superuser/createdb/createrole 모두 false.
- 개발 DB `chungpamo`, 테스트 DB `chungpamo_test`. 기존 DB/볼륨 재사용 없음.
- 테스트의 스키마 초기화와 데이터 쓰기는 `chungpamo_test`에만 수행. 종료 확인에서 개발 DB의 모든 업무 테이블은 0행.
- 기존 `sisicallcall-postgres`는 동일 ID의 exited 상태를 유지했다. 세 기존 볼륨의 이름/생성 시각, 기존 네트워크 ID도 유지됐다. 해당 DB에 접속하지 않았다.
- Windows 서비스 변경, 자원 삭제, commit/push 없음. PoC·프론트엔드 작업 트리 변경 없음.

## Alembic 및 스키마

새 개발 DB가 비어 있음을 확인하고 `0001 → 0002 → 0003 → 0004`를 순차 적용했다.
개발/테스트 DB 모두 최종 `0004 (head)`. public 테이블 13개(버전 테이블 포함), `btree_gist` 1.7,
사용자 정의 트리거 22개를 확인했다. 테스트 DB에서는 `0004 → 0001 → 0002 → 0003 → 0004` 왕복도 실행했다.

## 실제 발견 및 수정

1. **실행 위치에 따른 설정 누락**: 문서대로 `backend`에서 실행하면 상대 경로 `.env`가 루트 설정을 읽지 못했다.
   `backend/app/config.py`가 저장소 루트 `.env`를 절대 경로로 읽도록 수정했다. 환경변수와 `_env_file` override는 그대로 지원한다.
2. **0002 적용 실패**: 0001의 수요 입력 뷰가 `import_batch.status`를 참조하므로 PostgreSQL이 열 타입 변경을 거부했다.
   0002 upgrade/downgrade에서 뷰를 먼저 DROP하고 열 변경 후 해당 revision의 원래 정의로 재생성한다.
   동일 DDL 트랜잭션 안에서 처리하며 CASCADE나 테이블 데이터 삭제를 쓰지 않는다. revision 번호/체인/승인 규칙은 바꾸지 않았다.
   0002에서 실패하므로 뒤에 새 revision을 추가하는 것만으로는 신규 설치가 복구되지 않아 0002 자체를 최소 수정했다.
   이미 0002 이상인 DB에는 Alembic이 이 수정을 재실행하지 않는다. 외부 의존 뷰·추가 권한이 있는 운영 DB 업그레이드는 이번 검증 대상이 아니다.
3. **기존 DB 테스트 두 건의 결함**: 최종 상태 INSERT 차단 예외의 정규식을 실제 문구에 맞추고 SQLSTATE 23514도 확인한다.
   잘못된 entity로 BOM을 쓰는 시험의 빈 inventory 승인 배치는 savepoint 안에서 생성·롤백해 다른 deferred count 검사에 간섭하지 않게 했다.
   savepoint 정리는 assertion 실패 시에도 실행한다. 기존 차단 검증은 유지했으며 테스트를 삭제하거나 skip으로 바꾸지 않았다.
4. **테스트 DB 제한 강화**: fixture가 localhost의 임의 `_test` DB 대신 `127.0.0.1:55432`, `chungpamo_app`, 충파모 DB 이름만 허용한다.
   DROP SCHEMA 직전 `current_database() = chungpamo_test`를 확인한다. Alembic 자식 프로세스에는 비밀 URL을 명령행 대신 환경변수로 전달한다.

## 테스트 결과

작업 경로 `decision-supporter/backend`, 기존 가상환경 `.venv` 사용.

```powershell
..\.venv\Scripts\python.exe -m pytest tests/test_db_rules.py tests/test_sql_repository.py tests/test_health.py tests/test_postgres_integration.py tests/test_settings.py -q --tb=short
# 43 passed, 0 failed, 0 skipped (9.41s)

..\.venv\Scripts\python.exe -m pytest tests ../engine/tests -q --tb=short
# 443 passed, 2 failed, 0 skipped (14.41s)
```

- 이전 DB 필수 테스트 31개를 실제 실행했다. 위 43개에는 DB 불필요 health 오류 시험과 설정 경로 시험도 포함된다.
- 승인 상태 전이, 무승인 쓰기/삭제 차단, 원본 append-only, deferred 행 수 확인, 커밋 후 쓰기 차단, 실패 시 전체 롤백 통과.
- BOM 활성 유효기간 겹침 제약, 제품·자재·단위·BOM 구성품·두 종류 재고 참조 무결성 통과.
- 실제 SQL 저장소의 생성·재검증·승인·반려·조회와 FastAPI의 다섯 entity 등록/승인/조회 통과.
- 동시 승인 시험은 두 별도 DB 연결을 쓰고 `pg_blocking_pids`로 실제 대기를 확인한다. 선발 승인 성공, 후발 409 `ALREADY_APPROVED`, 수요 데이터는 한 번만 3행 저장됨.
- 의존성 검사 `pip check` 통과. Starlette/httpx 사용 중단 예정 경고 1건은 남아 있다.

전체 실행의 실패 두 건은 작업 시작 전에 있던 예측 API 변경과 기존 문서의 불일치다.

1. `test_openapi_snapshot_is_current`: 미커밋 `/dev/forecasts`와 예측 오류 코드가 정적 `contracts/openapi.json`에 반영되지 않음.
2. `test_guide_lists_every_api_path_and_common_error_code`: `docs/frontend_guide.md`에 `/dev/forecasts` 안내 없음.

DB 수정으로 이 두 실패를 숨기거나 skip 처리하지 않았다. 예측 API 코드·계약 스냅샷·해당 안내 문서는 이번 작업에서 변경하지 않았다.

## 프론트엔드 연동과 남은 작업

- 등록·재검증·승인·반려·목록 API의 PostgreSQL 경로는 합성 데이터로 검증되어 프론트엔드 연결 작업을 시작할 수 있다.
- 로컬 `.env`의 `PERSISTENCE=disabled`는 유지했다. 실제 저장 API를 켜려면 `PERSISTENCE=postgres`로 설정하고 서버를 재시작한다.
- 브라우저 origin에 맞는 `CORS_ALLOW_ORIGINS`를 지정한다. 현재 프론트엔드의 fixture 동작을 실제 API 호출로 교체해야 한다.
- 예측 API의 미완료 문서/스냅샷 동기화 및 테스트를 별도 마무리해야 한다. 승인 데이터 조회 → 예측 실행·저장은 아직 연결되지 않았다.
- 계획 생성·서버 계획 승인·정식 출력·인증/권한·실기업 데이터 검증은 이번 완료 범위가 아니다.
- 전용 DB 컨테이너는 healthy 상태로 유지했다. 서버 프로세스를 별도로 상시 실행하지 않았다.
