import subprocess
import sys
import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.config import get_settings

BACKEND = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session", autouse=True)
def isolate_default_persistence():
    # 로컬 실행 설정이 postgres여도 기본값(501) 시험은 disabled 환경에서 수행한다.
    # 실제 PostgreSQL API 시험은 명시적으로 postgres Settings를 주입한다.
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("PERSISTENCE", "disabled")
        yield


def pytest_addoption(parser):
    parser.addoption("--no-db", action="store_true", help="DB 필수 테스트를 접속 시도 없이 skip")


@pytest.fixture(scope="session", autouse=True)
def prevent_db_connections(request):
    if not request.config.getoption("--no-db"):
        yield
        return
    from sqlalchemy.engine import Engine
    import psycopg

    def forbidden(*args, **kwargs):
        raise AssertionError("--no-db: 데이터베이스 접속은 금지되어 있습니다.")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(Engine, "connect", forbidden)
        patch.setattr(psycopg, "connect", forbidden)
        yield


def _test_url() -> str:
    """검증된 충파모 loopback 주소/포트/역할에서 chungpamo_test만 초기화한다."""
    url = make_url(get_settings().database_url)
    if (url.drivername != "postgresql+psycopg" or url.host != "127.0.0.1" or url.port != 55432
            or url.username != "chungpamo_app" or url.database not in ("chungpamo", "chungpamo_test")):
        raise RuntimeError("DB 테스트는 127.0.0.1:55432의 충파모 전용 DB만 허용한다")
    url = url.set(database="chungpamo_test")
    return url.render_as_string(hide_password=False)


@pytest.fixture(scope="session")
def test_engine(request):
    """마이그레이션을 처음부터 적용한 테스트 DB. 도달할 수 없으면 해당 테스트는 건너뛴다(통과로 보고하지 않는다)."""
    if request.config.getoption("--no-db"):
        pytest.skip("--no-db: 실제 PostgreSQL 검증 제외 (접속 시도 없음)")
    url = _test_url()
    engine = create_engine(url, connect_args={"connect_timeout": 3})
    try:
        with engine.connect() as c:
            c.execute(text("SELECT 1"))
    except Exception as exc:
        pytest.skip(f"전용 PostgreSQL 컨테이너에 연결할 수 없음 (docker compose up -d db 필요): {type(exc).__name__}")
    with engine.begin() as c:   # 테스트 DB 만 비운다
        assert c.execute(text("SELECT current_database()")).scalar_one() == "chungpamo_test"
        c.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
    r = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"],
                       cwd=BACKEND, capture_output=True, text=True,
                       env={**os.environ, "DATABASE_URL": url})
    assert r.returncode == 0, r.stderr
    yield engine
    engine.dispose()


@pytest.fixture
def conn(test_engine):
    """각 테스트를 트랜잭션 안에서 실행하고 롤백한다."""
    with test_engine.connect() as c:
        tx = c.begin()
        yield c
        tx.rollback()
