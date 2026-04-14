from typing import Optional
from sqlalchemy import select, update, func
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.models import AgentRegistry
from app.core.logging import get_logger
logger = get_logger(__name__)
DEFAULT_ROUTING = {
    # Host Resource alerts
    "HostCPUHigh":"host_resource","HostLoadHigh":"host_resource","HostIOWaitHigh":"host_resource","HostStealHigh":"host_resource",
    "HostMemoryHigh":"host_resource","HostAvailableMemoryLow":"host_resource","HostSwapHigh":"host_resource","HostOOMRisk":"host_resource",
    "HostDiskUsageHigh":"host_resource","HostDiskUsageCritical":"host_resource","HostDiskInodeHigh":"host_resource",
    "HostDiskIOHigh":"host_resource","HostDiskLatencyHigh":"host_resource",
    # Supervisor alerts
    "SupervisorProcessDown":"supervisor","SupervisorProcessFatal":"supervisor",
    "SupervisorProcessExited":"supervisor","SupervisorProcessBackoff":"supervisor",
    "SupervisorProcessRestarting":"supervisor",
}
class AgentRegistryService:
    def __init__(self, db: AsyncSession): self.db = db
    async def register(self, agent_id, agent_type, supported_alerts, base_url, queue_name, version="1.0.0"):
        existing = (await self.db.execute(select(AgentRegistry).where(AgentRegistry.agent_id == agent_id))).scalar_one_or_none()
        if existing:
            await self.db.execute(update(AgentRegistry).where(AgentRegistry.agent_id == agent_id).values(
                supported_alerts=supported_alerts, base_url=base_url, queue_name=queue_name, version=version, status="active", last_heartbeat=func.now()))
        else:
            self.db.add(AgentRegistry(agent_id=agent_id, agent_type=agent_type, supported_alerts=supported_alerts, base_url=base_url, queue_name=queue_name, version=version))
        await self.db.flush()
        logger.info(f"[REGISTRY] Agent: {agent_id} ({agent_type}) → {len(supported_alerts)} alerts")
    async def find_agent_for_alert(self, alert_name) -> Optional[dict]:
        agents = (await self.db.execute(select(AgentRegistry).where(AgentRegistry.status == "active"))).scalars().all()
        for a in agents:
            if alert_name in (a.supported_alerts or []):
                return {"agent_id": a.agent_id, "queue_name": a.queue_name, "base_url": a.base_url}
        t = DEFAULT_ROUTING.get(alert_name)
        return {"agent_id": t, "queue_name": f"agent:queue:{t}", "base_url": ""} if t else None
    async def list_agents(self):
        r = (await self.db.execute(select(AgentRegistry))).scalars().all()
        return [{"agent_id":a.agent_id,"agent_type":a.agent_type,"status":a.status,"supported_alerts":a.supported_alerts,"base_url":a.base_url,"version":a.version,"last_heartbeat":str(a.last_heartbeat)} for a in r]
    async def heartbeat(self, agent_id):
        await self.db.execute(update(AgentRegistry).where(AgentRegistry.agent_id == agent_id).values(last_heartbeat=func.now(), status="active"))
        await self.db.flush()
