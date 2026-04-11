"""Alert intake: normalize, dedup, create incident. NO auto-suppress."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.redis_client import RedisService
from app.repositories.incident_repo import IncidentRepository
from app.schemas.schemas import AlertManagerAlert, ALERT_RESOURCE_MAP, SUPERVISOR_ALERT_NAMES, IncidentStatus

logger = get_logger(__name__)


def _parse_datetime(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _detect_host_role(labels: dict) -> str:
    job = labels.get("job", "").lower()
    if any(k in job for k in ("mysql", "mariadb", "postgres", "db")):
        return "db"
    if any(k in job for k in ("nginx", "proxy", "haproxy")):
        return "proxy"
    if any(k in job for k in ("jenkins",)):
        return "jenkins"
    if any(k in job for k in ("batch", "redis", "kafka")):
        return "batch"
    return "app"


class AlertIntakeService:
    def __init__(self, db: AsyncSession, redis_svc: RedisService):
        self.repo = IncidentRepository(db)
        self.redis = redis_svc
        self.db = db

    async def process_alert(self, alert: AlertManagerAlert) -> Optional[str]:
        """Process alert: normalize, dedup, create incident, queue for processing.
        
        IMPORTANT: NO auto-suppress. ALL alerts go through full pipeline.
        Only operator can mark as noise AFTER reviewing the RCA result.
        """
        labels = alert.labels
        alert_name = labels.get("alertname", "UnknownAlert")
        instance = labels.get("instance", "unknown")
        fingerprint = alert.fingerprint or hashlib.md5(
            f"{alert_name}:{instance}".encode()
        ).hexdigest()

        logger.info(f"[INTAKE] {'='*50}")
        logger.info(f"[INTAKE] ▶ Alert received: {alert_name} instance={instance} "
                    f"status={alert.status} fingerprint={fingerprint}")

        # 1. Save raw
        raw_id = await self.repo.save_alert_raw(
            fingerprint=fingerprint,
            payload=alert.model_dump(),
        )
        logger.info(f"[INTAKE] 💾 Saved alerts_raw id={raw_id}")

        # 2. Normalize
        resource_type = ALERT_RESOURCE_MAP.get(alert_name, "UNKNOWN")
        severity = labels.get("severity", "warning")
        job_name = labels.get("job", "")
        service_name = labels.get("service", "")
        entity_name = labels.get("entity", instance.split(":")[0] if ":" in instance else instance)
        cluster_name = labels.get("cluster", "")
        component_type = _detect_host_role(labels)

        # Detect supervisor alerts → domain_type=SUPERVISOR
        is_supervisor = (
            alert_name in SUPERVISOR_ALERT_NAMES
            or job_name.lower() in ("supervisor", "supervisord")
            or labels.get("process_name", "") != ""
        )
        if is_supervisor:
            domain_type = "SUPERVISOR"
            resource_type = "PROCESS"
            entity_name = labels.get("process_name", labels.get("name", entity_name))
            service_name = labels.get("group_name", labels.get("group", service_name))
            logger.info(f"[INTAKE] 🔧 Detected SUPERVISOR alert: process={entity_name}")
        else:
            domain_type = "HOST"

        alert_key = f"{alert_name}:{instance}:{resource_type}"

        norm_id = await self.repo.save_alert_normalized(
            raw_id=raw_id,
            alert_name=alert_name,
            status=alert.status,
            severity=severity,
            instance=instance,
            job_name=job_name,
            resource_type=resource_type,
            domain_type=domain_type,
            component_type=component_type,
            service_name=service_name,
            entity_name=entity_name,
            cluster_name=cluster_name,
            alert_key=alert_key,
            labels_json=labels,
            annotations_json=alert.annotations,
            starts_at=_parse_datetime(alert.startsAt),
            ends_at=_parse_datetime(alert.endsAt),
        )
        logger.info(f"[INTAKE] 📋 Normalized id={norm_id}: resource={resource_type} "
                    f"role={component_type} entity={entity_name}")

        # 3. Dedup check — ONLY short-term duplicate within Redis TTL
        if await self.redis.check_dedup(fingerprint):
            logger.info(f"[INTAKE] 🔁 Deduplicated (same alert within {self.redis.settings.redis_dedup_ttl}s TTL)")
            await self.repo.save_audit(
                event_type="alert_deduplicated",
                entity_type="alert", entity_id=str(norm_id),
                details={"alert_name": alert_name, "instance": instance},
            )
            await self.db.commit()
            return None

        # 4. Create incident — ALWAYS process, never auto-suppress
        logger.info(f"[INTAKE] 🆕 Creating new incident...")
        inc_id = await self.repo.create_incident(
            alert_name=alert_name,
            title=f"{alert_name} on {instance}",
            status=IncidentStatus.NEW,
            severity=severity,
            instance=instance,
            resource_type=resource_type,
            domain_type=domain_type,
            component_type=component_type,
            service_name=service_name,
            entity_name=entity_name,
            cluster_name=cluster_name,
            context_json={"labels": labels, "annotations": alert.annotations},
        )

        await self.redis.set_dedup(fingerprint, inc_id)

        # 5. Audit + event
        await self.repo.save_audit(
            event_type="incident_created",
            entity_type="incident", entity_id=inc_id,
            details={"alert_name": alert_name, "instance": instance, "severity": severity},
        )
        await self.repo.save_incident_event(
            inc_id, "incident_created",
            {"alert_name": alert_name, "instance": instance},
        )

        # 6. Queue for worker
        await self.redis.push_incident(inc_id)
        logger.info(f"[INTAKE] ✅ Incident {inc_id} created and queued for processing")

        await self.db.commit()

        # 7. Publish realtime
        await self.redis.publish_event("incident_created", {
            "incident_id": inc_id, "alert_name": alert_name,
            "instance": instance, "severity": severity,
        })

        return inc_id
