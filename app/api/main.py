from fastapi import FastAPI

from app.api.routes_agent import router as agent_router
from app.core.config import settings
from app.core.database import init_db
from app.core.schemas import HealthResponse


app = FastAPI(title="Medical PACS Pull Data API")


@app.on_event("startup")
def startup():
    init_db()


@app.get("/health", response_model=HealthResponse)
def health():
    return HealthResponse(service=settings.app.name)


app.include_router(agent_router)
