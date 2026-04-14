"""Orchestrator client — registers supervisor agent and pushes results back."""
import httpx
from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

SUPPORTED_ALERTS = [
    "SupervisorProcessDown",
    "SupervisorProcessFatal",
    "SupervisorProcessExited",
    "SupervisorProcessBackoff",
    "SupervisorProcessRestarting",
]


async def register_with_orchestrator():
    s = get_settings()
    if not s.orchestrator_url:
        return
    payload = {
        "agent_id": s.agent_id,
        "agent_type": "supervisor",
        "supported_alerts": SUPPORTED_ALERTS,
        "base_url": f"http://{s.agent_host}:{s.app_port}",
        "queue_name": "agent:queue:supervisor",
        "version": "1.0.0",
    }
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(
                f"{s.orchestrator_url}/api/v1/agents/register", json=payload
            )
            logger.info(
                f"[ORCH] {'✅' if r.status_code == 200 else '❌'} Register: {r.status_code}"
            )
    except Exception as e:
        logger.warning(f"[ORCH] Register failed: {e}")


async def push_result_to_orchestrator(result: dict):
    s = get_settings()
    if not s.orchestrator_url:
        return
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(
                f"{s.orchestrator_url}/api/v1/agents/result", json=result
            )
            logger.info(f"[ORCH] Result push: {r.status_code}")
    except Exception as e:
        logger.error(f"[ORCH] Result push error: {e}")
