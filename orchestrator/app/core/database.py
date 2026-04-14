from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from app.core.config import get_settings
_engine = None
_factory = None
def get_engine():
    global _engine
    if _engine is None:
        _engine = create_async_engine(get_settings().database_url, echo=False, pool_size=20, pool_pre_ping=True)
    return _engine
def get_session_factory():
    global _factory
    if _factory is None:
        _factory = async_sessionmaker(get_engine(), class_=AsyncSession, expire_on_commit=False)
    return _factory
class Base(DeclarativeBase):
    pass
async def get_db():
    async with get_session_factory()() as s:
        try: yield s
        finally: await s.close()
