import json
from typing import Optional
import redis.asyncio as aioredis
from app.core.config import get_settings
_redis = None
async def get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(get_settings().redis_url, decode_responses=True)
    return _redis
class OrchestratorRedis:
    def __init__(self, redis):
        self.redis = redis
        self.settings = get_settings()
    async def check_dedup(self, fp): return await self.redis.exists(f"orch:dedup:{fp}") > 0
    async def set_dedup(self, fp, iid, ttl=None): await self.redis.setex(f"orch:dedup:{fp}", ttl or self.settings.redis_dedup_ttl, iid)
    async def push_to_agent(self, q, job): await self.redis.lpush(q, json.dumps(job, default=str))
    async def publish_event(self, t, d): await self.redis.publish("orch:events", json.dumps({"event_type": t, "data": d}, default=str))
    async def set_skip_llm(self, iid): await self.redis.setex(f"agent:skip_llm:{iid}", 86400, "1")
    async def clear_skip_llm(self, iid): await self.redis.delete(f"agent:skip_llm:{iid}")
