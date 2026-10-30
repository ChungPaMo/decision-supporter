from functools import lru_cache

from sqlalchemy import Engine, create_engine

from app.config import get_settings


@lru_cache
def get_engine() -> Engine:
    # pool_pre_ping: 서버 재시작 후 끊어진 연결을 요청 시점에 자동 복구
    return create_engine(get_settings().database_url, pool_pre_ping=True, connect_args={"connect_timeout": 3})
