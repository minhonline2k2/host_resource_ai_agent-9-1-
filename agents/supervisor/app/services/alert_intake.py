"""Alert intake for Supervisor Agent: normalize, dedup, create incident."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.redis_client import RedisService
from app.repositories.incident_repo import IncidentRepository
from app.schemas.schemas import (
    AlertManagerAlert,
    ALERT_RESOURCE_MAP,
    SUPERVISOR_ALERT_NAMES,
    IncidentStatus,
)

logger = get_logger(__name__)


def _parse_datetime(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


class AlertIntakeService:
    def __init__(self, db: AsyncSession, redis_svc: RedisService):
        self.repo = IncidentRepository(db)
        self.redis = redis_svc
        self.db = db

    async def process_alert(self, alert: AlertManagerAlert) -> Optional[str]:
        """Process supervisor alert: normalize, dedup, create incident, queue.

        IMPORTANT: NO auto-suppress. ALL alerts go through full pipeline.
        Only operator can mark as noise AFTER reviewing the RCA result.
        """
        labels = alert.labels
        alert_name = labels.get("alertname", "UnknownAlert")
        instance = labels.get("instance", "unknown")
        process_name = labels.get("process_name", labels.get("name", ""))
        group_name = labels.get("group", "")
        fingerprint = alert.fingerprint or hashlib.md5(
            f"{alert_name}:{instance}:{process_name}".encode()
        ).hexdigest()

        logger.info(f"[INTAKE] {'=' * 50}")
        logger.info(
            f"[INTAKE] ▶ Alert: {alert_name} instance={instance} "
            f"process={process_name} status={alert.status} fp={fingerprint}"
        )

        # Validate this is a supervisor alert
        job_name = labels.get("job", "")
        is_supervisor = (
            alert_name in SUPERVISOR_ALERT_NAMES
            or job_name in ("supervisor", "supervisord")
            or bool(process_name)
        )
        if not is_supervisor:
            logger.warning(
                f"[INTAKE] ⚠️ Not a supervisor alert: {alert_name} job={job_name}"
            )

        # 1. Save raw
        raw_id = await self.repo.save_alert_raw(
            fingerprint=fingerprint,
            payload=alert.model_dump(),
        )
        logger.info(f"[INTAKE] 💾 Saved alerts_raw id={raw_id}")

        # 2. Normalize
        resource_type = ALERT_RESOURCE_MAP.get(alert_name, "PROCESS")
        severity = labels.get("severity", "critical")
        service_name = labels.get("service", process_name)
        entity_name = process_name or instance.split(":")[0]
        alert_key = f"{alert_name}:{instance}:{process_name}"

        norm_id = await self.repo.save_alert_normalized(
            raw_id=raw_id,
            alert_name=alert_name,
            status=alert.status,
            severity=severity,
            instance=instance,
            job_name=job_name,
            resource_type=resource_type,
            domain_type="SUPERVISOR",
            component_type="supervisor",
            service_name=service_name,
            entity_name=entity_name,
            cluster_name=labels.get("cluster", ""),
            alert_key=alert_key,
            labels_json=labels,
            annotations_json=alert.annotations,
            starts_at=_parse_datetime(alert.startsAt),
            ends_at=_parse_datetime(alert.endsAt),
        )
        logger.info(
            f"[INTAKE] 📋 Normalized id={norm_id}: process={process_name} "
            f"entity={entity_name}"
        )

        # 3a. Redis dedup
        if await self.redis.check_dedup(fingerprint):
            logger.info(
                f"[INTAKE] 🔁 Deduplicated (Redis TTL)"
            )
            await self.repo.save_audit(
                event_type="alert_deduplicated",
                entity_type="alert",
                entity_id=str(norm_id),
                details={"alert_name": alert_name, "instance": instance,
                         "process_name": process_name},
            )
            await self.db.commit()
            return None

        # 3b. DB-level dedup — check for actively processing incidents
        #     for the same process on the same instance
        existing = await self.repo.find_open_incident(alert_name, instance)
        if existing:
            logger.info(
                f"[INTAKE] 🔁 DB-level dedup: incident {existing.id} "
                f"still processing (status={existing.status})"
            )
            await self.repo.save_audit(
                event_type="alert_deduplicated_db",
                entity_type="alert",
                entity_id=str(norm_id),
                details={
                    "alert_name": alert_name,
                    "instance": instance,
                    "process_name": process_name,
                    "existing_incident": existing.id,
                    "existing_status": existing.status,
                },
            )
            await self.redis.set_dedup(fingerprint, existing.id)
            await self.db.commit()
            return None

        # 4. Create incident
        logger.info("[INTAKE] 🆕 Creating new supervisor incident...")
        inc_id = await self.repo.create_incident(
            alert_name=alert_name,
            title=f"{alert_name}: {process_name} on {instance}",
            status=IncidentStatus.NEW,
            severity=severity,
            instance=instance,
            resource_type=resource_type,
            domain_type="SUPERVISOR",
            component_type="supervisor",
            service_name=service_name,
            entity_name=entity_name,
            cluster_name="",
            context_json={
                "labels": labels,
                "annotations": alert.annotations,
                "process_name": process_name,
                "group_name": group_name,
            },
        )

        # 5. Audit + event — COMMIT first, then queue
        await self.repo.save_audit(
            event_type="incident_created",
            entity_type="incident",
            entity_id=inc_id,
            details={
                "alert_name": alert_name,
                "instance": instance,
                "process_name": process_name,
                "severity": severity,
            },
        )
        await self.repo.save_incident_event(
            inc_id,
            "incident_created",
            {"alert_name": alert_name, "instance": instance,
             "process_name": process_name},
        )

        # IMPORTANT: commit BEFORE pushing to Redis queue (race condition fix)
        await self.db.commit()

        await self.redis.set_dedup(fingerprint, inc_id)
        await self.redis.push_incident(inc_id)
        logger.info(f"[INTAKE] ✅ Incident {inc_id} created and queued")

        # Publish realtime
        await self.redis.publish_event(
            "incident_created",
            {
                "incident_id": inc_id,
                "alert_name": alert_name,
                "instance": instance,
                "process_name": process_name,
                "severity": severity,
            },
        )

        return inc_id
