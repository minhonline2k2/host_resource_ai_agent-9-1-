"""Redis client for queue, dedup, event bus, and locks."""

from __future__ import annotations

import json
import asyncio
from typing import Any, Optional

import redis.asyncio as aioredis

from app.core.config import get_settings

settings = get_settings()

_redis: Optional[aioredis.Redis] = None


async def get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    return _redis


class RedisKeys:
    QUEUE = "agent:queue:host_resource"
    EXEC_QUEUE = "agent:queue:host_resource:execute"
    DEDUP_PREFIX = "agent:dedup:"
    APPROVAL_PREFIX = "agent:approval:"
    EXEC_LOCK_PREFIX = "agent:exec_lock:"
    EVENT_CHANNEL = "agent:events"
    STATS_CACHE = "agent:stats:cache"


class RedisService:
    def __init__(self, redis: aioredis.Redis):
        self.redis = redis
        self.settings = get_settings()

    # --- Dedup ---
    async def check_dedup(self, fingerprint: str) -> bool:
        key = f"{RedisKeys.DEDUP_PREFIX}{fingerprint}"
        return await self.redis.exists(key) > 0

    async def set_dedup(self, fingerprint: str, incident_id: str) -> None:
        key = f"{RedisKeys.DEDUP_PREFIX}{fingerprint}"
        await self.redis.setex(key, self.settings.redis_dedup_ttl, incident_id)

    # --- Queue ---
    async def push_incident(self, incident_id: str) -> None:
        await self.redis.lpush(RedisKeys.QUEUE, incident_id)

    async def pop_incident(self, timeout: int = 5) -> Optional[str]:
        result = await self.redis.brpop(RedisKeys.QUEUE, timeout=timeout)
        return result[1] if result else None

    async def pop_exec_job(self, timeout: int = 1) -> Optional[str]:
        result = await self.redis.brpop(RedisKeys.EXEC_QUEUE, timeout=timeout)
        return result[1] if result else None

    async def queue_length(self) -> int:
        return await self.redis.llen(RedisKeys.QUEUE)

    # --- Event Bus ---
    async def publish_event(self, event_type: str, data: dict) -> None:
        payload = json.dumps({"event_type": event_type, "data": data}, default=str)
        await self.redis.publish(RedisKeys.EVENT_CHANNEL, payload)

    # --- Approval state ---
    async def set_approval_pending(self, incident_id: str, option_ids: list[str]) -> None:
        key = f"{RedisKeys.APPROVAL_PREFIX}{incident_id}"
        await self.redis.setex(key, self.settings.redis_approval_ttl, json.dumps(option_ids))

    async def get_approval_pending(self, incident_id: str) -> Optional[list[str]]:
        key = f"{RedisKeys.APPROVAL_PREFIX}{incident_id}"
        val = await self.redis.get(key)
        return json.loads(val) if val else None

    async def clear_approval(self, incident_id: str) -> None:
        await self.redis.delete(f"{RedisKeys.APPROVAL_PREFIX}{incident_id}")

    # --- Execution lock ---
    async def acquire_exec_lock(self, incident_id: str) -> bool:
        key = f"{RedisKeys.EXEC_LOCK_PREFIX}{incident_id}"
        return await self.redis.set(key, "1", nx=True, ex=self.settings.redis_exec_lock_ttl)

    async def release_exec_lock(self, incident_id: str) -> None:
        await self.redis.delete(f"{RedisKeys.EXEC_LOCK_PREFIX}{incident_id}")
