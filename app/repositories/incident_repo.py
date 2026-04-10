"""Repository layer for database operations."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select, func, update, and_, or_, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import (
    AlertRaw, AlertNormalized, Incident, IncidentEvidence,
    RemediationOption, Approval, ExecutionLog, VerificationResult,
    RemediationKnowledge, IncidentPattern, AuditEvent, IncidentEvent,
)
from app.schemas.schemas import IncidentStatus


def gen_id() -> str:
    return str(uuid.uuid4())


def gen_incident_number() -> str:
    now = datetime.now(timezone.utc)
    return f"INC-{now.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"


class IncidentRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    # --- Alert Raw ---
    async def save_alert_raw(self, fingerprint: str, payload: dict) -> int:
        obj = AlertRaw(fingerprint=fingerprint, payload_json=payload)
        self.db.add(obj)
        await self.db.flush()
        return obj.id

    # --- Alert Normalized ---
    async def save_alert_normalized(self, raw_id: int, **kwargs) -> int:
        obj = AlertNormalized(raw_alert_id=raw_id, **kwargs)
        self.db.add(obj)
        await self.db.flush()
        return obj.id

    # --- Incidents ---
    async def create_incident(self, **kwargs) -> str:
        inc_id = gen_id()
        inc = Incident(
            id=inc_id,
            incident_number=gen_incident_number(),
            **kwargs,
        )
        self.db.add(inc)
        await self.db.flush()
        return inc_id

    async def get_incident(self, incident_id: str) -> Optional[Incident]:
        result = await self.db.execute(select(Incident).where(Incident.id == incident_id))
        return result.scalar_one_or_none()

    async def update_incident(self, incident_id: str, **kwargs) -> None:
        await self.db.execute(
            update(Incident).where(Incident.id == incident_id).values(**kwargs, updated_at=func.now())
        )
        await self.db.flush()

    async def list_incidents(self, limit: int = 50, offset: int = 0) -> list[Incident]:
        result = await self.db.execute(
            select(Incident).order_by(desc(Incident.created_at)).limit(limit).offset(offset)
        )
        return list(result.scalars().all())

    async def get_stats(self) -> dict:
        total = await self.db.scalar(select(func.count()).select_from(Incident))
        active = await self.db.scalar(
            select(func.count()).select_from(Incident).where(
                Incident.status.in_(list(IncidentStatus.ACTIVE_STATUSES))
            )
        )
        pending = await self.db.scalar(
            select(func.count()).select_from(Incident).where(
                Incident.status == IncidentStatus.ACTION_PROPOSED
            )
        )
        return {"total": total or 0, "active": active or 0, "pending_approvals": pending or 0}

    # --- Evidence ---
    async def save_evidence(self, incident_id: str, evidence_list: list[dict]) -> None:
        for ev in evidence_list:
            obj = IncidentEvidence(incident_id=incident_id, **ev)
            self.db.add(obj)
        await self.db.flush()

    async def get_evidence(self, incident_id: str) -> list[IncidentEvidence]:
        result = await self.db.execute(
            select(IncidentEvidence).where(IncidentEvidence.incident_id == incident_id)
        )
        return list(result.scalars().all())

    # --- Remediation Options ---
    async def save_remediation_options(self, incident_id: str, options: list[dict]) -> list[str]:
        ids = []
        for i, opt in enumerate(options):
            opt = dict(opt)  # copy to avoid mutating caller
            opt_id = opt.pop("id", None) or gen_id()
            opt.pop("incident_id", None)
            opt.pop("option_no", None)
            obj = RemediationOption(
                id=opt_id, incident_id=incident_id, option_no=i + 1, **opt
            )
            self.db.add(obj)
            ids.append(opt_id)
        await self.db.flush()
        return ids

    async def get_remediation_options(self, incident_id: str) -> list[RemediationOption]:
        result = await self.db.execute(
            select(RemediationOption).where(RemediationOption.incident_id == incident_id)
            .order_by(RemediationOption.priority)
        )
        return list(result.scalars().all())

    async def get_remediation_option(self, option_id: str) -> Optional[RemediationOption]:
        result = await self.db.execute(select(RemediationOption).where(RemediationOption.id == option_id))
        return result.scalar_one_or_none()

    async def update_option_status(self, option_id: str, status: str) -> None:
        await self.db.execute(
            update(RemediationOption).where(RemediationOption.id == option_id).values(status=status)
        )

    async def update_option_commands(self, option_id: str, commands: list[str]) -> None:
        await self.db.execute(
            update(RemediationOption).where(RemediationOption.id == option_id)
            .values(commands_json=commands)
        )

    # --- Approvals ---
    async def save_approval(self, incident_id: str, action_proposal_id: str,
                            decision: str, decided_by: str, reason: str = None) -> int:
        obj = Approval(
            incident_id=incident_id, action_proposal_id=action_proposal_id,
            decision=decision, decided_by=decided_by, reason=reason,
        )
        self.db.add(obj)
        await self.db.flush()
        return obj.id

    async def get_approvals(self, incident_id: str) -> list[Approval]:
        result = await self.db.execute(
            select(Approval).where(Approval.incident_id == incident_id)
        )
        return list(result.scalars().all())

    # --- Execution Logs ---
    async def save_execution_log(self, **kwargs) -> int:
        obj = ExecutionLog(**kwargs)
        self.db.add(obj)
        await self.db.flush()
        return obj.id

    async def get_execution_logs(self, incident_id: str) -> list[ExecutionLog]:
        result = await self.db.execute(
            select(ExecutionLog).where(ExecutionLog.incident_id == incident_id)
            .order_by(ExecutionLog.step_no)
        )
        return list(result.scalars().all())

    # --- Verification ---
    async def save_verification(self, **kwargs) -> int:
        obj = VerificationResult(**kwargs)
        self.db.add(obj)
        await self.db.flush()
        return obj.id

    async def get_verifications(self, incident_id: str) -> list[VerificationResult]:
        result = await self.db.execute(
            select(VerificationResult).where(VerificationResult.incident_id == incident_id)
        )
        return list(result.scalars().all())

    # --- Knowledge ---
    async def find_knowledge_exact(self, signature: str) -> Optional[RemediationKnowledge]:
        result = await self.db.execute(
            select(RemediationKnowledge)
            .where(RemediationKnowledge.root_cause_signature_v2 == signature)
            .where(RemediationKnowledge.success_count > 0)
            .order_by(desc(RemediationKnowledge.confidence))
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def find_knowledge_partial(
        self, domain_type: str, alert_name: str, resource_type: str,
        canonical_root_cause: str = None, limit: int = 3
    ) -> list[RemediationKnowledge]:
        conditions = [
            RemediationKnowledge.domain_type == domain_type,
            or_(
                RemediationKnowledge.alert_name == alert_name,
                RemediationKnowledge.resource_type == resource_type,
            ),
            RemediationKnowledge.success_count > RemediationKnowledge.failure_count,
        ]
        if canonical_root_cause:
            conditions.append(RemediationKnowledge.canonical_root_cause == canonical_root_cause)

        result = await self.db.execute(
            select(RemediationKnowledge)
            .where(and_(*conditions))
            .order_by(desc(RemediationKnowledge.confidence), desc(RemediationKnowledge.success_count))
            .limit(limit)
        )
        return list(result.scalars().all())

    async def save_knowledge(self, **kwargs) -> int:
        obj = RemediationKnowledge(**kwargs)
        self.db.add(obj)
        await self.db.flush()
        return obj.id

    async def update_knowledge_success(self, knowledge_id: int) -> None:
        await self.db.execute(
            update(RemediationKnowledge)
            .where(RemediationKnowledge.id == knowledge_id)
            .values(
                success_count=RemediationKnowledge.success_count + 1,
                usage_count=RemediationKnowledge.usage_count + 1,
                last_used_at=func.now(), last_success_at=func.now(),
            )
        )

    async def update_knowledge_failure(self, knowledge_id: int) -> None:
        await self.db.execute(
            update(RemediationKnowledge)
            .where(RemediationKnowledge.id == knowledge_id)
            .values(
                failure_count=RemediationKnowledge.failure_count + 1,
                usage_count=RemediationKnowledge.usage_count + 1,
                last_used_at=func.now(), last_failure_at=func.now(),
            )
        )

    # --- Patterns ---
    async def find_matching_pattern(
        self, domain_type: str, alert_name: str = None, entity: str = None
    ) -> Optional[IncidentPattern]:
        conditions = [
            IncidentPattern.active == True,
            IncidentPattern.domain_type == domain_type,
        ]
        result = await self.db.execute(
            select(IncidentPattern).where(and_(*conditions)).limit(5)
        )
        patterns = result.scalars().all()
        # Simple pattern matching
        for p in patterns:
            if p.entity_pattern and entity and p.entity_pattern in entity:
                return p
            if p.root_cause_signature_v2 and alert_name and alert_name in (p.root_cause_signature_v2 or ""):
                return p
        return None

    async def save_pattern(self, **kwargs) -> int:
        obj = IncidentPattern(**kwargs)
        self.db.add(obj)
        await self.db.flush()
        return obj.id

    async def deactivate_patterns_by_signature(self, signature: str) -> None:
        await self.db.execute(
            update(IncidentPattern)
            .where(IncidentPattern.root_cause_signature_v2 == signature)
            .values(active=False)
        )
        await self.db.flush()

    # --- Recent similar incidents ---
    async def find_recent_similar(
        self, alert_name: str, instance: str, limit: int = 5
    ) -> list[Incident]:
        result = await self.db.execute(
            select(Incident)
            .where(
                Incident.alert_name == alert_name,
                Incident.instance == instance,
                Incident.root_cause.isnot(None),
            )
            .order_by(desc(Incident.created_at))
            .limit(limit)
        )
        return list(result.scalars().all())

    # --- Audit ---
    async def save_audit(self, event_type: str, entity_type: str = None,
                         entity_id: str = None, actor: str = "system",
                         action: str = None, details: dict = None) -> None:
        obj = AuditEvent(
            event_type=event_type, entity_type=entity_type,
            entity_id=entity_id, actor=actor, action=action,
            details_json=details,
        )
        self.db.add(obj)
        await self.db.flush()

    async def list_audit(self, limit: int = 100) -> list[AuditEvent]:
        result = await self.db.execute(
            select(AuditEvent).order_by(desc(AuditEvent.created_at)).limit(limit)
        )
        return list(result.scalars().all())

    # --- Incident Events ---
    async def save_incident_event(self, incident_id: str, event_type: str, data: dict = None) -> None:
        obj = IncidentEvent(incident_id=incident_id, event_type=event_type, event_data_json=data)
        self.db.add(obj)
        await self.db.flush()

    async def get_incident_events(self, incident_id: str) -> list[IncidentEvent]:
        result = await self.db.execute(
            select(IncidentEvent).where(IncidentEvent.incident_id == incident_id)
            .order_by(IncidentEvent.created_at)
        )
        return list(result.scalars().all())

    # --- Delete ---
    async def delete_incident(self, incident_id: str) -> None:
        """Delete incident and all related child records."""
        from sqlalchemy import delete
        for model in (IncidentEvent, VerificationResult, ExecutionLog, Approval,
                      RemediationOption, IncidentEvidence):
            await self.db.execute(delete(model).where(model.incident_id == incident_id))
        await self.db.execute(delete(Incident).where(Incident.id == incident_id))
        await self.db.flush()
