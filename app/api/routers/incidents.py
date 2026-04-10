"""API routers for incidents, alerts, approvals, audit, SSE."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Body
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.redis_client import get_redis, RedisService
from app.core.logging import get_logger
from app.repositories.incident_repo import IncidentRepository
from app.schemas.schemas import (
    AlertManagerWebhook, ApprovalRequest, IncidentStats,
    IncidentListItem, IncidentDetail, IncidentStatus,
    EvidenceItem, ActionProposal, ApprovalItem, ExecutionItem, EventItem,
    LLMAnalysis, RootCauseItem, AuditItem,
)
from app.services.alert_intake import AlertIntakeService
from app.services.execution_service import ExecutionService
from app.services.verification_service import VerificationService

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1")


# === Delete incident ===
@router.delete("/incidents/{incident_id}")
async def delete_incident(
    incident_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Delete incident and all related data from DB."""
    repo = IncidentRepository(db)
    incident = await repo.get_incident(incident_id)
    if not incident:
        raise HTTPException(404, "Incident not found")

    await repo.delete_incident(incident_id)
    await db.commit()

    redis = await get_redis()
    await RedisService(redis).publish_event("incident_deleted", {"incident_id": incident_id})
    return {"status": "ok"}


# === Health ===
@router.get("/health")
async def health():
    return {"status": "ok", "service": "host_resource_ai_agent"}


# === Alert Webhook ===
@router.post("/alerts/webhook")
async def receive_alert(
    webhook: AlertManagerWebhook,
    db: AsyncSession = Depends(get_db),
):
    redis = await get_redis()
    redis_svc = RedisService(redis)
    intake = AlertIntakeService(db, redis_svc)

    created_ids = []
    for alert in webhook.alerts:
        try:
            inc_id = await intake.process_alert(alert)
            if inc_id:
                created_ids.append(inc_id)
        except Exception as e:
            logger.error(f"Alert processing error: {e}")

    return {"status": "ok", "incidents_created": len(created_ids), "incident_ids": created_ids}


# === Incidents ===
@router.get("/incidents/stats")
async def get_stats(db: AsyncSession = Depends(get_db)):
    repo = IncidentRepository(db)
    stats = await repo.get_stats()
    return IncidentStats(**stats)


@router.get("/incidents")
async def list_incidents(
    limit: int = 50, offset: int = 0,
    db: AsyncSession = Depends(get_db),
):
    repo = IncidentRepository(db)
    incidents = await repo.list_incidents(limit=limit, offset=offset)
    return [
        IncidentListItem(
            id=inc.id,
            incident_number=inc.incident_number,
            title=inc.title,
            status=inc.status,
            severity=inc.severity,
            alert_type=inc.alert_name,
            instance=inc.instance,
            llm_confidence=inc.llm_confidence,
            created_at=inc.created_at,
            updated_at=inc.updated_at,
        )
        for inc in incidents
    ]


@router.get("/incidents/{incident_id}")
async def get_incident_detail(incident_id: str, db: AsyncSession = Depends(get_db)):
    repo = IncidentRepository(db)
    incident = await repo.get_incident(incident_id)
    if not incident:
        raise HTTPException(404, "Incident not found")

    evidence = await repo.get_evidence(incident_id)
    options = await repo.get_remediation_options(incident_id)
    approvals = await repo.get_approvals(incident_id)
    exec_logs = await repo.get_execution_logs(incident_id)
    verifications = await repo.get_verifications(incident_id)
    events = await repo.get_incident_events(incident_id)

    # Build LLM analysis from ai_analysis_json
    llm_analysis = None
    if incident.ai_analysis_json:
        ai = incident.ai_analysis_json
        llm_analysis = LLMAnalysis(
            summary=ai.get("summary", incident.summary or ""),
            root_causes=[RootCauseItem(**rc) for rc in ai.get("root_causes", [])],
            confidence=ai.get("confidence", incident.llm_confidence or 0),
        )
    elif incident.root_cause:
        llm_analysis = LLMAnalysis(
            summary=incident.summary or incident.root_cause,
            root_causes=[RootCauseItem(
                name=incident.root_cause,
                confidence=incident.llm_confidence or 0,
                why=incident.root_cause_summary or "",
            )],
            confidence=incident.llm_confidence or 0,
        )

    return IncidentDetail(
        incident={
            "id": incident.id,
            "incident_number": incident.incident_number,
            "title": incident.title,
            "status": incident.status,
            "severity": incident.severity,
            "alert_type": incident.alert_name,
            "instance": incident.instance,
            "resource_type": incident.resource_type,
            "component_type": incident.component_type,
            "root_cause_summary": incident.root_cause_summary or incident.summary,
            "llm_confidence": incident.llm_confidence,
            "knowledge_source": incident.knowledge_source,
            "llm_prompt_text": incident.llm_prompt_text,
            "llm_raw_response": incident.llm_raw_response,
            "created_at": str(incident.created_at) if incident.created_at else None,
            "updated_at": str(incident.updated_at) if incident.updated_at else None,
        },
        alerts=[{
            "id": "alert-1",
            "alert_name": incident.alert_name,
            "severity": incident.severity,
            "status": "firing",
            "received_at": str(incident.created_at) if incident.created_at else None,
        }],
        evidence=[
            EvidenceItem(
                id=e.id,
                command_id=e.command_id,
                command_text=e.command_text,
                evidence_type=e.evidence_type,
                raw_text=e.raw_text,
                parsed_json=e.parsed_json,
                exit_code=e.exit_code,
                duration_ms=e.duration_ms,
                is_key_evidence=e.is_key_evidence or False,
                collected_at=e.observed_at,
            )
            for e in evidence if e.source_type == "ssh"
        ],
        llm_analysis=llm_analysis,
        action_proposals=[
            ActionProposal(
                id=o.id,
                priority=o.priority,
                title=o.title,
                description=o.description,
                risk_level=o.risk_level,
                commands=o.commands_json or [],
                expected_effect=o.expected_effect,
                rollback_commands=o.rollback_commands_json or [],
                warnings=o.warnings_json or [],
                status=o.status,
                created_at=o.created_at,
            )
            for o in options
        ],
        approvals=[
            ApprovalItem(
                id=a.id,
                action_proposal_id=a.action_proposal_id,
                decision=a.decision,
                decided_by=a.decided_by,
                reason=a.reason,
                decided_at=a.decided_at,
            )
            for a in approvals
        ],
        execution_results=[
            ExecutionItem(
                id=el.id,
                step_no=el.step_no,
                step_name=el.step_name,
                status=el.status,
                command=el.command,
                stdout=el.stdout,
                stderr=el.stderr,
                exit_code=el.exit_code,
                started_at=el.started_at,
                finished_at=el.finished_at,
            )
            for el in exec_logs
        ],
        events=[
            EventItem(
                event_type=ev.event_type,
                event_data=ev.event_data_json,
                created_at=ev.created_at,
            )
            for ev in events
        ],
    )


# === Approvals ===
@router.post("/approvals")
async def create_approval(
    req: ApprovalRequest,
    db: AsyncSession = Depends(get_db),
):
    redis = await get_redis()
    redis_svc = RedisService(redis)
    repo = IncidentRepository(db)

    # Find option and incident
    option = await repo.get_remediation_option(req.action_proposal_id)
    if not option:
        raise HTTPException(404, "Action proposal not found")

    incident = await repo.get_incident(option.incident_id)
    if not incident:
        raise HTTPException(404, "Incident not found")

    # Save approval
    await repo.save_approval(
        incident_id=incident.id,
        action_proposal_id=req.action_proposal_id,
        decision=req.decision,
        decided_by=req.decided_by,
        reason=req.reason,
    )

    if req.decision == "approved":
        await repo.update_incident(incident.id,
            status=IncidentStatus.APPROVED,
            selected_option_id=req.action_proposal_id,
        )
        # If operator selected specific commands, update the option
        if req.selected_commands is not None:
            all_cmds = option.commands_json or []
            filtered = [all_cmds[i] for i in req.selected_commands if i < len(all_cmds)]
            await repo.update_option_commands(req.action_proposal_id, filtered)
            logger.info(f"[APPROVAL] Operator selected {len(filtered)}/{len(all_cmds)} commands")

        await repo.update_option_status(req.action_proposal_id, "approved")

        await repo.save_audit(
            event_type="action_approved", entity_type="incident",
            entity_id=incident.id, actor=req.decided_by,
            action="approve", details={"option_id": req.action_proposal_id, "title": option.title},
        )

        await db.commit()

        # Execute in background
        host = incident.instance.split(":")[0] if ":" in incident.instance else incident.instance
        asyncio.create_task(_handle_execution(incident.id, req.action_proposal_id, host, redis_svc))

        await redis_svc.publish_event("action_approved", {
            "incident_id": incident.id, "option_id": req.action_proposal_id,
        })

    elif req.decision == "canceled":
        await repo.update_incident(incident.id, status=IncidentStatus.CANCELED)
        await repo.update_option_status(req.action_proposal_id, "canceled")
        await repo.save_audit(
            event_type="action_canceled", entity_type="incident",
            entity_id=incident.id, actor=req.decided_by,
            action="cancel",
        )
        await db.commit()

        await redis_svc.publish_event("action_canceled", {
            "incident_id": incident.id,
        })

    return {"status": "ok", "decision": req.decision}


# === Mark as noise / suppress ===
@router.post("/incidents/{incident_id}/suppress")
async def suppress_incident(
    incident_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Operator marks incident as noise AFTER reviewing RCA. Optionally creates pattern."""
    redis = await get_redis()
    redis_svc = RedisService(redis)
    repo = IncidentRepository(db)

    incident = await repo.get_incident(incident_id)
    if not incident:
        raise HTTPException(404, "Incident not found")

    # Update incident status
    await repo.update_incident(incident_id,
        status=IncidentStatus.SUPPRESSED,
        final_status=IncidentStatus.SUPPRESSED,
    )

    # Create pattern so future IDENTICAL alerts (same alert+instance+root_cause) can be flagged
    # But NOT auto-suppressed — operator still decides
    if incident.canonical_root_cause:
        await repo.save_pattern(
            pattern_type="known_noise",
            domain_type=incident.domain_type or "HOST",
            component_type=incident.component_type or "",
            entity_pattern=incident.instance.split(":")[0] if incident.instance else "",
            root_cause_signature_v2=incident.root_cause_signature_v2 or "",
            description=f"Operator đánh dấu noise: {incident.root_cause or incident.alert_name}",
            created_by="operator",
        )

    await repo.save_audit(
        event_type="incident_suppressed_by_operator",
        entity_type="incident", entity_id=incident_id,
        actor="operator", action="suppress",
        details={"alert_name": incident.alert_name, "root_cause": incident.canonical_root_cause},
    )
    await repo.save_incident_event(incident_id, "suppressed_by_operator", {
        "root_cause": incident.canonical_root_cause,
    })
    await db.commit()

    await redis_svc.publish_event("incident_suppressed", {"incident_id": incident_id})

    return {"status": "ok", "message": "Incident đã được đánh dấu không cảnh báo"}


# === Skip LLM — operator chooses to not query LLM ===
@router.post("/incidents/{incident_id}/skip-llm")
async def skip_llm(
    incident_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Operator requests to skip LLM analysis. Agent will only collect evidence."""
    redis = await get_redis()
    redis_svc = RedisService(redis)
    repo = IncidentRepository(db)

    incident = await repo.get_incident(incident_id)
    if not incident:
        raise HTTPException(404, "Incident not found")

    # Set skip flag in Redis (TTL 24h)
    await redis.setex(f"agent:skip_llm:{incident_id}", 86400, "1")

    await repo.save_audit(
        event_type="llm_skipped_by_operator",
        entity_type="incident", entity_id=incident_id,
        actor="operator", action="skip_llm",
    )
    await repo.save_incident_event(incident_id, "llm_skip_requested", {})
    await db.commit()

    await redis_svc.publish_event("llm_skipped", {"incident_id": incident_id})
    return {"status": "ok", "message": "Đã yêu cầu bỏ qua LLM. Agent chỉ thu thập dữ liệu."}


# === Query LLM — trigger LLM analysis for a paused incident ===
@router.post("/incidents/{incident_id}/query-llm")
async def query_llm(
    incident_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Operator triggers LLM analysis for an incident that has evidence but skipped LLM."""
    redis = await get_redis()
    redis_svc = RedisService(redis)

    from app.workers.incident_worker import trigger_llm_analysis
    result = await trigger_llm_analysis(db, redis_svc, incident_id)

    if result.get("status") == "error":
        raise HTTPException(400, result["message"])

    return result


# === Unsuppress — re-enable alerting ===
@router.post("/incidents/{incident_id}/unsuppress")
async def unsuppress_incident(
    incident_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Operator re-enables alerting for a suppressed incident."""
    redis = await get_redis()
    redis_svc = RedisService(redis)
    repo = IncidentRepository(db)

    incident = await repo.get_incident(incident_id)
    if not incident:
        raise HTTPException(404, "Incident not found")
    if incident.status != IncidentStatus.SUPPRESSED:
        raise HTTPException(400, "Incident is not suppressed")

    # Restore to action_proposed so operator can review again
    new_status = IncidentStatus.ACTION_PROPOSED if incident.root_cause else IncidentStatus.NEW
    await repo.update_incident(incident_id,
        status=new_status,
        final_status=None,
    )

    # Deactivate any noise pattern created for this
    if incident.root_cause_signature_v2:
        await repo.deactivate_patterns_by_signature(incident.root_cause_signature_v2)

    await repo.save_audit(
        event_type="incident_unsuppressed",
        entity_type="incident", entity_id=incident_id,
        actor="operator", action="unsuppress",
    )
    await repo.save_incident_event(incident_id, "unsuppressed_by_operator", {})
    await db.commit()

    await redis_svc.publish_event("incident_unsuppressed", {"incident_id": incident_id})
    return {"status": "ok", "message": "Đã bật lại cảnh báo cho incident này"}


# === Monitor / Watch — extend dedup then re-check ===
@router.post("/incidents/{incident_id}/monitor")
async def monitor_incident(
    incident_id: str,
    db: AsyncSession = Depends(get_db),
    body: dict = Body(default={}),
):
    """Operator chooses to monitor: dedup this alert for N minutes, then re-create if still firing."""
    from app.schemas.schemas import MonitorRequest
    duration = (body or {}).get("duration_minutes", 15)
    if duration < 1 or duration > 1440:
        duration = 15

    redis = await get_redis()
    redis_svc = RedisService(redis)
    repo = IncidentRepository(db)

    incident = await repo.get_incident(incident_id)
    if not incident:
        raise HTTPException(404, "Incident not found")

    # Set extended dedup TTL for this alert fingerprint
    import hashlib
    fingerprint = hashlib.md5(
        f"{incident.alert_name}:{incident.instance}".encode()
    ).hexdigest()
    ttl_seconds = duration * 60
    key = f"agent:dedup:{fingerprint}"
    await redis.setex(key, ttl_seconds, incident_id)

    # Update status
    await repo.update_incident(incident_id,
        status="monitoring",
        summary=f"Đang theo dõi {duration} phút. Nếu alert vẫn firing sau đó sẽ tạo incident mới.",
    )
    await repo.save_audit(
        event_type="incident_monitoring",
        entity_type="incident", entity_id=incident_id,
        actor="operator", action="monitor",
        details={"duration_minutes": duration},
    )
    await repo.save_incident_event(incident_id, "monitoring_started", {
        "duration_minutes": duration,
    })
    await db.commit()

    await redis_svc.publish_event("incident_monitoring", {
        "incident_id": incident_id, "duration_minutes": duration,
    })

    return {"status": "ok", "duration_minutes": duration,
            "message": f"Sẽ theo dõi {duration} phút, sau đó alert sẽ được xử lý lại nếu vẫn firing"}


async def _handle_execution(incident_id: str, option_id: str, host: str, redis_svc: RedisService):
    """Handle execution and verification in background."""
    from app.core.database import get_session_factory
    try:
        factory = get_session_factory()
        async with factory() as db:
            exec_svc = ExecutionService(db, redis_svc)
            result = await exec_svc.execute_approved_action(incident_id, option_id, host)

            if result.get("status") == "success":
                await asyncio.sleep(30)
                async with factory() as db2:
                    verify_svc = VerificationService(db2)
                    await verify_svc.verify_incident(incident_id)
    except Exception as e:
        logger.error(f"Background execution failed: {e}")


# === Audit ===
@router.get("/audit")
async def get_audit(limit: int = 100, db: AsyncSession = Depends(get_db)):
    repo = IncidentRepository(db)
    events = await repo.list_audit(limit=limit)
    return [
        AuditItem(
            id=e.id,
            event_type=e.event_type,
            entity_type=e.entity_type,
            entity_id=e.entity_id,
            actor=e.actor,
            action=e.action,
            details=e.details_json,
            created_at=e.created_at,
        )
        for e in events
    ]


# === SSE Events ===
@router.get("/events/stream")
async def event_stream(request: Request):
    """Server-Sent Events endpoint for realtime updates."""

    async def generate():
        redis = await get_redis()
        pubsub = redis.pubsub()
        await pubsub.subscribe("agent:events")

        try:
            while True:
                if await request.is_disconnected():
                    break

                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if message and message["type"] == "message":
                    data = message["data"]
                    yield f"data: {data}\n\n"

                # Heartbeat every 15s
                yield f": heartbeat\n\n"
                await asyncio.sleep(1)
        finally:
            await pubsub.unsubscribe("agent:events")
            await pubsub.close()

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
