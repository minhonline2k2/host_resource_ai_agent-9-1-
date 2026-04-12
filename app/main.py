"""Main FastAPI application."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from app.core.config import get_settings
from app.core.logging import setup_logging, get_logger
from app.core.database import get_engine, Base
from app.api.routers.incidents import router as incidents_router

# Import all models so metadata is populated for create_all
from app.models.models import *  # noqa

settings = get_settings()
logger = get_logger(__name__)

STATIC_DIR = Path(__file__).parent.parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging("DEBUG" if settings.app_debug else "INFO")

    # Track background tasks so they can be awaited on shutdown
    app.state.background_tasks = set()

    # Auto-create all tables on startup
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database tables created/verified")

    yield

    # Wait for background tasks to complete on shutdown
    if app.state.background_tasks:
        logger.info(f"Waiting for {len(app.state.background_tasks)} background tasks...")
        await asyncio.gather(*app.state.background_tasks, return_exceptions=True)

    await engine.dispose()


app = FastAPI(
    title="Host Resource AI Agent",
    version="1.0.0",
    description="AI-powered incident management for Prometheus host resource alerts",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# API routes
app.include_router(incidents_router)


# UI route — serve static/index.html at root
@app.get("/", response_class=HTMLResponse)
async def serve_ui():
    index_path = STATIC_DIR / "index.html"
    if index_path.exists():
        return HTMLResponse(content=index_path.read_text(encoding="utf-8"))
    return HTMLResponse(content="<h1>Host Resource AI Agent</h1><p>UI not found. Place index.html in static/</p>")


# Serve static assets
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
