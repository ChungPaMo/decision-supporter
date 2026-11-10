# 테스트 정리 및 실제 등록·승인 연결 — 2026-11-10

두 MVP 저장소를 별도로 유지했다. 기존 미커밋 예측 API·DB 검증 변경은 보존하고 그 위에 추가했다.
PoC·sisicallcall Docker 자원·Windows 서비스 변경 및 commit/push는 하지 않았다.

## 검증 결과

- 전체 백엔드·예측 엔진 pytest: **460 passed, 0 failed, 0 skipped**.
- 프론트엔드 `npm run type-check`, `npm run build`: 통과.
- Chromium 브라우저 E2E: **6 passed, 0 failed, 0 skipped** (등록/실패 처리 5개 + 실제 API 프로세스 재시작 후 복원 1개).
- 미승인 데이터는 제품·자재·BOM·재고·월별 수요 조회에서 각각 0건임을 확인하고, 승인 후에만 조회되는 것을 검증했다.
- 기존 개발 DB는 초기화하거나 테스트 데이터를 쓰지 않았다. E2E는 `chungpamo_test`만 초기화한다.

## 실제 E2E 범위

1. 선행 마스터가 없으면 BOM 등록 입력을 비활성화한다.
2. 잘못된 단위가 있는 합성 제품 CSV를 등록 → 오류 표시·승인 차단 → 사유 입력 후 실제 반려한다.
3. 수정된 제품 XLSX → 자재 CSV → BOM CSV → 재고 CSV → 월별 수요 CSV를 순서대로 승인한다.
4. 월별 수요 HOLD의 열을 지정한다. 다른 클라이언트의 재검증으로 실제 VERSION_CONFLICT를 만든 뒤 UI가 최신 버전을 다시 읽는 것을 확인한다.
5. UI가 본 뒤 다른 클라이언트가 설정을 변경해 실제 RESULT_CHANGED를 만든다. 자동 재승인하지 않고 최신 결과를 본 후 다시 승인한다.
6. 승인 요청에 서버의 result_digest가 그대로 전달되는 것을 검사한다. 새로고침마다 GET 목록·상세로 승인 상태를 복원한다.
7. 테스트 API 프로세스를 종료하고 새 PID로 실행한다. DB를 건드리지 않고 새 브라우저 컨텍스트에서도 다섯 entity의 승인 상태를 복원한다.
8. 422/501/503/네트워크 오류 시험에서는 해당 응답·통신 실패만 브라우저에서 주입한다. 이 시험을 실제 DB 중단 시험으로 표현하지 않는다. Fixture 승인 전환이 없음을 확인한다.

실제 오류 응답에는 `location=null`인 검증 항목이 있었다. 기존 프론트 타입의 non-null 가정을 수정하고 화면에 파일 전체 오류로 표시한다.
테스트 폼의 접근 가능한 이름을 명시했으며 Playwright 변환 캐시를 프로젝트 `.e2e` 하위로 분리했다.
테스트 서버는 실행기가 생성한 PID만 종료한다. 다른 프로젝트 캐시·프로세스는 정리하지 않는다.

## 파일별 변경 이유 — 이번 단계

백엔드 저장소:

| 파일 | 변경 이유 |
|---|---|
| `backend/generate_api_examples.py` | 로컬 저장 활성 설정과 무관하게 기본 disabled 예시를 재현하도록 격리 |
| `backend/export_contract_schema.py` | 실제 예측 요청·응답 모델의 JSON Schema 생성 |
| `backend/tests/conftest.py` | 기본값 시험은 disabled로 격리하고 실제 PostgreSQL 시험의 명시적 postgres 설정 유지 |
| `backend/tests/test_forecast_api.py` | 실제 엔진 연결·결과 보존·이력 부족·입력 오류·run=null·안전한 내부 오류 시험 |
| `backend/tests/test_contract_schema_sync.py` | 개발 예측 JSON Schema 동기화 검사 추가 |
| `contracts/openapi.json`, `contracts/dev_forecast_draft.schema.json` | 생성기로 현재 API·모델과 동기화 |
| `docs/api_contract_forecasts.md`, `docs/frontend_guide.md`, `docs/engine_forecast_plan_contract.md` | 개발 API 경계·오류·실제 등록 연결 상태 문서화 |
| `README.md` | 통합 의존성 설치 방법·최신 검증 결과 안내 |

기존 데이터/Excel 계약·응답 예시는 생성 결과의 논리적 내용이 같아 계약 변경이 없다. 기존 두 실패는 예측 route 추가 뒤 정적 OpenAPI와 안내 문서가 갱신되지 않은 문제였다. 테스트를 삭제·완화·skip 처리하지 않고 생성 결과와 문서를 고쳤다.

프론트엔드 저장소:

| 파일 | 변경 이유 |
|---|---|
| `src/screens/DataUploadScreen.tsx` | 실제 업로드·재검증·승인·반려·이력·복원·오류 처리로 전환 |
| `src/services/api.ts` | 타입이 있는 배치 페이지, 전체 배치 페이지 조회, 승인 데이터 조회, 네트워크 오류 정규화 |
| `src/types/api.ts` | 실제 Summary/Detail/settings 및 nullable 계약 반영 |
| `src/store/productionStore.ts` | 서버에서 확인한 승인 entity와 조회 완료 여부 관리 |
| `src/layouts/MainLayout.tsx` | 서버 확인 전 다음 단계 잠금, 남은 예시 계산 화면 명시 |
| `vite.config.ts` | .env.local 로딩·정확한 loopback 주소·포트 고정 |
| `package.json`, `package-lock.json`, `playwright.config.ts` | 브라우저 E2E 의존성·실행 명령·구성 |
| `tests/e2e/registration.spec.ts`, `tests/run_postgres_e2e.py` | 실제 브라우저/DB 승인 및 재시작 검증, 전용 테스트 DB 제한 |
| `.gitignore`, `README.md` | 테스트 로그·캐시·화면·결과 제외 및 실제 실행 방법 안내 |

## 실행 설정과 데이터 보존

- 백엔드 루트의 Git 제외 `.env`: 기존 비밀번호 유지, `PERSISTENCE=postgres`, `CORS_ALLOW_ORIGINS=http://127.0.0.1:5173`.
- 개발 DATABASE_URL: 충파모 앱 역할, `127.0.0.1:55432/chungpamo`만 사용한다.
- 프론트 Git 제외 `.env.local`: `VITE_API_URL=http://127.0.0.1:8000`. 프론트에 DB 비밀번호를 넣지 않는다.
- E2E 프로세스에서만 `DATABASE_URL`의 DB를 `chungpamo_test`로 바꾸고, API 8001 / 프론트 5174 / 해당 origin 하나를 쓴다.
- 테스트 실행기는 충파모 Docker 프로젝트·health·볼륨, 고정 주소/포트/역할 및 실제 DB 이름을 확인한 후에만 테스트 스키마를 초기화한다.
- 브라우저 저장소에는 배치 ID만 보관한다. version/digest/승인 상태는 GET 응답을 다시 읽으며, 네트워크 오류 시 예시로 대체하지 않는다.

## 예측 API 및 남은 Mock

`POST /dev/forecasts`는 합성 JSON 전용 실제 엔진 API다. 요청 구조는 엔진 모델을 재사용하고 개발용 source_type/날짜/수량 제한만 좁힌다. 엔진의 run/forecasts/skipped/adjustments/warnings/errors를 보존한다. INPUT_INVALID는 HTTP 422와 run=null로 반환한다.

등록 화면에서는 Fixture 승인·예시 전체 승인·로컬 반려 생성을 제거했다. 예측/계획 비교/진단 화면의 예시 JSON과 계획 승인 메모리 기록은 남아 있으며 명시적으로 예시임을 표시한다. Excel도 예시 계획 JSON으로 만드는 DEMO/UNVERIFIED 출력이다.

다음 단계는 승인된 수요 조회 → 예측 실행·결과 저장을 연결하고 예측 화면을 실제 API로 교체하는 것이다. 이때 run=null, DATA_INSUFFICIENT, 제외 제품·경고를 처리해야 한다. 계획 엔진·서버 계획 승인·정식 출력·인증/권한은 별도 후속 범위다.
