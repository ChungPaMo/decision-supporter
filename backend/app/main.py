from fastapi import FastAPI

from app.api.health import router as health_router
from app.api.ingest import router as ingest_router
from app.config import get_settings

app = FastAPI(title="충파모 과제 7 MVP", version=get_settings().app_version)
app.include_router(health_router)
app.include_router(ingest_router)
