"""Orchestrator — creates ALL database tables, serves API only."""
from contextlib import asynccontextmanager
import asyncio
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.core.config import get_settings
from app.core.logging import setup_logging, get_logger
from app.core.database import get_engine, Base
from app.models.models import *  # noqa — imports ALL tables
from app.api.routes import router

settings = get_settings()

@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging("DEBUG" if settings.app_debug else "INFO")
    log = get_logger(__name__)
    engine = get_engine()
    for attempt in range(30):
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            log.info("✅ Orchestrator started — ALL tables created")
            break
        except Exception as e:
            log.warning(f"DB not ready ({attempt+1}/30): {e}")
            await asyncio.sleep(3)
    yield
    await engine.dispose()

app = FastAPI(title="AI Alert Platform — Orchestrator", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
app.include_router(router)

@app.get("/")
async def root():
    return {"service": "orchestrator", "ui": settings.ui_base_url}
