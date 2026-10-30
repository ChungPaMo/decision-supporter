import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.config import get_settings

BACKEND = Path(__file__).resolve().parents[1]


def _test_url() -> str:
    """전용 컨테이너의 chungpamo_test DB. 로컬호스트·`_test` 이름이 아니면 어떤 경우에도 쓰지 않는다."""
    url = make_url(get_settings().database_url).set(database="chungpamo_test")
    if url.host not in ("127.0.0.1", "localhost") or not url.database.endswith("_test"):
        raise RuntimeError("테스트는 로컬 전용 *_test DB 에서만 실행한다")
    return url.render_as_string(hide_password=False)


@pytest.fixture(scope="session")
def test_engine():
    """마이그레이션을 처음부터 적용한 테스트 DB. 도달할 수 없으면 해당 테스트는 건너뛴다(통과로 보고하지 않는다)."""
    url = _test_url()
    engine = create_engine(url, connect_args={"connect_timeout": 3})
    try:
        with engine.connect() as c:
            c.execute(text("SELECT 1"))
    except Exception as exc:
        pytest.skip(f"전용 PostgreSQL 컨테이너에 연결할 수 없음 (docker compose up -d db 필요): {type(exc).__name__}")
    with engine.begin() as c:   # 테스트 DB 만 비운다
        c.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
    r = subprocess.run([sys.executable, "-m", "alembic", "-x", f"url={url}", "upgrade", "head"],
                       cwd=BACKEND, capture_output=True, text=True)
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
