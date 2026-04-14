"""Database engine, session factory, and base model."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

from app.core.config import get_settings

_engine = None
_factory = None


def get_engine():
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(
            settings.database_url,
            echo=False,
            pool_size=10,
            max_overflow=20,
            pool_pre_ping=True,
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _factory
    if _factory is None:
        _factory = async_sessionmaker(get_engine(), class_=AsyncSession, expire_on_commit=False)
    return _factory


# Backwards compat alias used by worker
def async_session_factory():
    """Return a NEW session (caller must close/use as context manager)."""
    factory = get_session_factory()
    return factory()


class Base(DeclarativeBase):
    pass


async def get_db():
    """FastAPI dependency that yields a session."""
    async with get_session_factory()() as session:
        try:
            yield session
        finally:
            await session.close()
