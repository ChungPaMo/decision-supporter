from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # 로컬 개발용 기본값(compose.yaml 의 전용 컨테이너, 127.0.0.1 한정). 공유·운영 환경은 반드시 .env 로 덮어쓴다.
    database_url: str = "postgresql+psycopg://chungpamo_app:change_me_app_dev_only@127.0.0.1:55432/chungpamo"
    app_version: str = "0.1.0"


def get_settings() -> Settings:
    return Settings()
