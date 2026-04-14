"""Host Resource Agent — FastAPI application."""
from __future__ import annotations
from contextlib import asynccontextmanager
from pathlib import Path
import asyncio
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from app.core.config import get_settings
from app.core.logging import setup_logging, get_logger
from app.core.database import get_engine, Base
from app.models.models import *  # noqa
settings = get_settings()
logger = get_logger(__name__)
STATIC_DIR = Path(__file__).parent.parent / "static"

@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging("DEBUG" if settings.app_debug else "INFO")
    engine = get_engine()

    if settings.orchestrator_url:
        # Orchestrator mode: wait for tables to be ready (orchestrator creates them)
        logger.info(f"Orchestrator mode: waiting for DB tables from {settings.orchestrator_url}...")
        for attempt in range(30):
            try:
                async with engine.connect() as conn:
                    from sqlalchemy import text
                    await conn.execute(text("SELECT 1 FROM incidents LIMIT 1"))
                logger.info("✅ DB tables ready")
                break
            except Exception:
                if attempt < 29:
                    await asyncio.sleep(3)
                else:
                    logger.error("❌ DB tables not ready after 90s — starting anyway")

        # Register with orchestrator
        try:
            from app.core.orchestrator import register_with_orchestrator
            await register_with_orchestrator()
        except Exception as e:
            logger.warning(f"Orchestrator registration: {e}")
    else:
        # Standalone mode: create all tables ourselves
        logger.info("Standalone mode: creating DB tables...")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("✅ Tables created")

    yield
    await engine.dispose()

app = FastAPI(title="Host Resource AI Agent", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

from app.api.routers.incidents import router
app.include_router(router)

@app.get("/", response_class=HTMLResponse)
async def root():
    f = STATIC_DIR / "index.html"
    return HTMLResponse(f.read_text()) if f.exists() else HTMLResponse("<h1>Host Resource Agent</h1>")

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
