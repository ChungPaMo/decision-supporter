from alembic import context
from sqlalchemy import create_engine, pool

from app.config import get_settings
from app.models import Base

config = context.config
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(url=get_settings().database_url, target_metadata=target_metadata, literal_binds=True,
                      dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # 테스트가 전용 DB 를 지정할 수 있도록 -x url=... 를 허용한다. 지정하지 않으면 .env / 기본값(프로젝트 전용 컨테이너).
    url = context.get_x_argument(as_dictionary=True).get("url") or get_settings().database_url
    engine = create_engine(url, poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
