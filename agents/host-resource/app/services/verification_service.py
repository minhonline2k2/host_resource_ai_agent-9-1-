"""Verification service: verify incident resolution after execution."""

from __future__ import annotations

from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.clients.prometheus_client import PrometheusClient
from app.collectors.ssh_collector import SSHCollector
from app.repositories.incident_repo import IncidentRepository
from app.schemas.schemas import IncidentStatus

logger = get_logger(__name__)


class VerificationService:
    def __init__(self, db: AsyncSession):
        self.repo = IncidentRepository(db)
        self.db = db
        self.prom = PrometheusClient()

    async def verify_incident(self, incident_id: str) -> str:
        """Verify if incident is resolved after execution. Returns result status."""

        incident = await self.repo.get_incident(incident_id)
        if not incident:
            return "unknown"

        instance = incident.instance
        resource_type = incident.resource_type
        host = instance.split(":")[0] if ":" in instance else instance

        checks = []

        # 1. Prometheus metric check
        prom_result = await self._check_prometheus(instance, resource_type)
        checks.append(prom_result)

        # 2. SSH read-only check
        ssh_result = self._check_ssh(host, resource_type)
        checks.append(ssh_result)

        # Determine overall result
        if all(c["result"] == "success" for c in checks):
            overall = "success"
            new_status = IncidentStatus.RESOLVED
        elif any(c["result"] == "failed" for c in checks):
            overall = "failed"
            new_status = IncidentStatus.EXECUTION_FAILED
        elif any(c["result"] == "partial" for c in checks):
            overall = "partial"
            new_status = IncidentStatus.MANUAL_REQUIRED
        else:
            overall = "unknown"
            new_status = IncidentStatus.MANUAL_REQUIRED

        # Save verification
        await self.repo.save_verification(
            incident_id=incident_id,
            verification_type="post_execution",
            result=overall,
            details_json={"checks": checks},
        )

        await self.repo.update_incident(
            incident_id,
            status=new_status,
            verification_status=overall,
            final_status=new_status,
        )

        await self.repo.save_incident_event(incident_id, "verification_completed", {
            "result": overall, "new_status": new_status,
        })

        await self.db.commit()
        return overall

    async def _check_prometheus(self, instance: str, resource_type: str) -> dict:
        """Check Prometheus metrics post-execution."""
        try:
            snapshot = await self.prom.collect_host_snapshot(instance)
            rt = resource_type.upper()

            if rt == "CPU":
                cpu = snapshot.get("cpu_usage", 100)
                if cpu < 70:
                    return {"type": "prometheus_cpu", "result": "success", "value": cpu}
                elif cpu < 85:
                    return {"type": "prometheus_cpu", "result": "partial", "value": cpu}
                return {"type": "prometheus_cpu", "result": "failed", "value": cpu}

            elif rt == "RAM":
                mem = snapshot.get("memory_used_pct", 100)
                if mem < 80:
                    return {"type": "prometheus_memory", "result": "success", "value": mem}
                elif mem < 90:
                    return {"type": "prometheus_memory", "result": "partial", "value": mem}
                return {"type": "prometheus_memory", "result": "failed", "value": mem}

            elif rt == "DISK":
                disks = await self.prom.collect_disk_snapshot(instance)
                max_usage = max((d["usage_pct"] for d in disks), default=0)
                if max_usage < 80:
                    return {"type": "prometheus_disk", "result": "success", "value": max_usage}
                elif max_usage < 90:
                    return {"type": "prometheus_disk", "result": "partial", "value": max_usage}
                return {"type": "prometheus_disk", "result": "failed", "value": max_usage}

        except Exception as e:
            logger.error(f"Prometheus verification failed: {e}")

        return {"type": "prometheus", "result": "unknown", "error": "check failed"}

    def _check_ssh(self, host: str, resource_type: str) -> dict:
        """Run read-only SSH checks."""
        try:
            collector = SSHCollector(host)
            rt = resource_type.upper()

            if rt == "CPU":
                result = collector.run_command("cat /proc/loadavg")
                if result["exit_code"] == 0:
                    parts = result["stdout"].strip().split()
                    if parts:
                        load1 = float(parts[0])
                        if load1 < 4:
                            return {"type": "ssh_load", "result": "success", "value": load1}
                        elif load1 < 8:
                            return {"type": "ssh_load", "result": "partial", "value": load1}
                        return {"type": "ssh_load", "result": "failed", "value": load1}

            elif rt == "RAM":
                result = collector.run_command("free -m | awk 'NR==2{printf \"%.1f\", $3/$2*100}'")
                if result["exit_code"] == 0:
                    try:
                        pct = float(result["stdout"].strip())
                        if pct < 80:
                            return {"type": "ssh_memory", "result": "success", "value": pct}
                        elif pct < 90:
                            return {"type": "ssh_memory", "result": "partial", "value": pct}
                        return {"type": "ssh_memory", "result": "failed", "value": pct}
                    except ValueError:
                        pass

            elif rt == "DISK":
                result = collector.run_command("df -h / --output=pcent | tail -1 | tr -d ' %'")
                if result["exit_code"] == 0:
                    try:
                        pct = float(result["stdout"].strip())
                        if pct < 80:
                            return {"type": "ssh_disk", "result": "success", "value": pct}
                        elif pct < 90:
                            return {"type": "ssh_disk", "result": "partial", "value": pct}
                        return {"type": "ssh_disk", "result": "failed", "value": pct}
                    except ValueError:
                        pass

        except Exception as e:
            logger.error(f"SSH verification failed: {e}")

        return {"type": "ssh", "result": "unknown", "error": "check failed"}
