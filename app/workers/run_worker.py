"""Worker runner entrypoint — can be run as:
  python3 -m app.workers.run_worker
  python3 app/workers/run_worker.py
"""

import asyncio
import sys
import os

# Ensure project root is in path
project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, project_root)
os.chdir(project_root)  # so .env is found

print(f"[WORKER] Project root: {project_root}")
print(f"[WORKER] Working dir: {os.getcwd()}")
print(f"[WORKER] Starting worker process...")


async def main():
    # Test connections before starting
    from app.core.config import get_settings
    settings = get_settings()
    print(f"[WORKER] DB: {settings.database_url.split('@')[1] if '@' in settings.database_url else '?'}")
    print(f"[WORKER] Redis: {settings.redis_url}")
    print(f"[WORKER] Prometheus: {settings.prometheus_url}")

    # Test Redis
    try:
        from app.core.redis_client import get_redis, RedisService
        redis = await get_redis()
        pong = await redis.ping()
        qlen = await redis.llen("agent:incident:queue")
        print(f"[WORKER] ✅ Redis connected (ping={pong}, queue_length={qlen})")
    except Exception as e:
        print(f"[WORKER] ❌ Redis connection FAILED: {e}")
        print(f"[WORKER] Worker cannot start without Redis. Exiting.")
        sys.exit(1)

    # Test DB
    try:
        from app.core.database import get_engine
        engine = get_engine()
        async with engine.connect() as conn:
            from sqlalchemy import text
            result = await conn.execute(text("SELECT 1"))
            print(f"[WORKER] ✅ Database connected")
    except Exception as e:
        print(f"[WORKER] ❌ Database connection FAILED: {e}")
        print(f"[WORKER] Worker cannot start without DB. Exiting.")
        sys.exit(1)

    print(f"[WORKER] ========================================")
    print(f"[WORKER] All connections OK — starting poll loop")
    print(f"[WORKER] ========================================")

    from app.workers.incident_worker import run_worker
    await run_worker()


asyncio.run(main())
