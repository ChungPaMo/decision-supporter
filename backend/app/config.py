from typing import Literal

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Compose와 같은 루트 .env를 실행 디렉터리에 관계없이 읽는다.
    model_config = SettingsConfigDict(env_file=Path(__file__).resolve().parents[2] / ".env", extra="ignore")

    # 로컬 개발용 기본값(compose.yaml 의 전용 컨테이너, 127.0.0.1 한정). 공유·운영 환경은 반드시 .env 로 덮어쓴다.
    database_url: str = "postgresql+psycopg://chungpamo_app:change_me_app_dev_only@127.0.0.1:55432/chungpamo"
    app_version: str = "0.1.0"
    # 배치 저장(업로드 보관·승인·조회) API. 기본은 꺼짐: DB 통합이 검증되기 전에는 영구 저장을 하는 척하지 않는다.
    # 'postgres' 로 켜면 SqlBatchRepository 를 쓴다 — Docker 개발 PC 에서 검증 전까지 미검증 경로.
    persistence: Literal["disabled", "postgres"] = "disabled"
    # 쉼표로 구분한 브라우저 허용 출처 (예: http://localhost:5173). 비우면 CORS 를 켜지 않는다. '*' 는 허용하지 않는다.
    cors_allow_origins: str = ""

    def cors_origins(self) -> list[str]:
        origins = [o.strip().rstrip("/") for o in self.cors_allow_origins.split(",") if o.strip()]
        if "*" in origins:
            raise ValueError("CORS_ALLOW_ORIGINS 에 '*' 는 쓸 수 없습니다. 출처를 명시하세요.")
        return origins


def get_settings() -> Settings:
    return Settings()
