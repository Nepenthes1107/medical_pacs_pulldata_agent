import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from src.core.settings import settings
from src.infrastructure.db.session import init_db
from src.schema.api import HealthResponse
from src.service.pacs_routes import router as pacs_router
from src.service.service import router as platform_agent_router


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Schema creation is an explicit development opt-in. Production deployments
    # must run config/schema.sql and migrations before starting the service.
    if os.getenv("AUTO_CREATE_SCHEMA", "false").lower() in ("1", "true", "yes", "on"):
        init_db()
    yield


app = FastAPI(title="Medical PACS Pull Data API", lifespan=lifespan)


@app.get("/health", response_model=HealthResponse)
def health():
    return HealthResponse(service=settings.app.name)


if settings.app.legacy_agent_api_enabled:
    from app.api.routes_agent import router as agent_router
    app.include_router(agent_router)
app.include_router(pacs_router)
app.include_router(platform_agent_router)
