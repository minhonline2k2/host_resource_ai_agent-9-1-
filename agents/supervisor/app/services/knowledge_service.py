"""Knowledge service: lookup, reuse, and learning."""

from __future__ import annotations

from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.repositories.incident_repo import IncidentRepository

logger = get_logger(__name__)


def build_signature_v2(
    domain_type: str, alert_name: str, entity_name: str,
    service_or_process: str, issue_subtype: str, canonical_root_cause: str,
) -> str:
    """Build root_cause_signature_v2."""
    parts = [
        domain_type or "HOST",
        alert_name or "Unknown",
        entity_name or "*",
        service_or_process or "*",
        issue_subtype or "*",
        canonical_root_cause or "*",
    ]
    return "|".join(parts)


class KnowledgeService:
    def __init__(self, db: AsyncSession):
        self.repo = IncidentRepository(db)
        self.db = db

    async def lookup(
        self,
        domain_type: str,
        alert_name: str,
        resource_type: str,
        instance: str,
        entity_name: str = "",
        canonical_root_cause: str = None,
        signature_v2: str = None,
    ) -> dict:
        """
        Run knowledge lookup in order:
        1. exact signature match
        2. partial match
        3. recent similar incidents
        Returns dict with source, knowledge, confidence.
        """

        # 1. Exact match
        if signature_v2:
            exact = await self.repo.find_knowledge_exact(signature_v2)
            if exact and exact.confidence >= 0.6 and exact.success_count > 0:
                logger.info(f"Knowledge exact match: {signature_v2}")
                return {
                    "source": "knowledge_exact",
                    "knowledge": exact,
                    "confidence": exact.confidence,
                    "knowledge_id": exact.id,
                    "remediation_steps": exact.remediation_steps_json,
                    "short_title": exact.short_title,
                }

        # 2. Partial match
        partials = await self.repo.find_knowledge_partial(
            domain_type=domain_type,
            alert_name=alert_name,
            resource_type=resource_type,
            canonical_root_cause=canonical_root_cause,
        )
        if partials:
            best = partials[0]
            if best.confidence >= 0.5 and best.success_count >= 2:
                logger.info(f"Knowledge partial match: {best.canonical_root_cause}")
                return {
                    "source": "knowledge_partial",
                    "knowledge": best,
                    "confidence": best.confidence * 0.8,  # Discount partial
                    "knowledge_id": best.id,
                    "remediation_steps": best.remediation_steps_json,
                    "short_title": best.short_title,
                }

        # 3. Recent similar incidents
        similar = await self.repo.find_recent_similar(alert_name, instance)
        if similar:
            best_inc = similar[0]
            if best_inc.root_cause and best_inc.final_status == "resolved":
                logger.info(f"Found recent similar incident: {best_inc.id}")
                return {
                    "source": "recent_similar",
                    "knowledge": None,
                    "confidence": 0.4,
                    "incident_ref": best_inc.id,
                    "root_cause": best_inc.root_cause,
                    "canonical_root_cause": best_inc.canonical_root_cause,
                }

        return {"source": "none", "confidence": 0.0}

    async def learn_from_incident(
        self,
        incident_id: str,
        domain_type: str,
        alert_name: str,
        resource_type: str,
        canonical_root_cause: str,
        issue_subtype: str,
        signature_v2: str,
        short_title: str,
        remediation_steps: list[dict],
        success: bool,
        component_type: str = "",
        service_name: str = "",
    ) -> None:
        """Learn from a completed incident."""

        existing = await self.repo.find_knowledge_exact(signature_v2)
        if existing:
            if success:
                await self.repo.update_knowledge_success(existing.id)
            else:
                await self.repo.update_knowledge_failure(existing.id)
            await self.db.flush()
            return

        # Create new knowledge entry
        if success:
            await self.repo.save_knowledge(
                domain_type=domain_type,
                component_type=component_type,
                service_name=service_name,
                alert_name=alert_name,
                resource_type=resource_type,
                canonical_root_cause=canonical_root_cause,
                issue_subtype=issue_subtype,
                root_cause_signature_v2=signature_v2,
                short_title=short_title,
                remediation_steps_json=remediation_steps,
                source="learned",
                confidence=0.6,
                success_count=1,
                incident_id_ref=incident_id,
            )
            await self.db.flush()
