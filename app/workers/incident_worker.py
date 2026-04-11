"""Incident worker: processes incidents through the full pipeline with detailed logging."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.database import get_session_factory
from app.core.redis_client import get_redis, RedisService
from app.clients.prometheus_client import PrometheusClient
from app.clients.llm_client import LLMClient
from app.collectors.ssh_collector import SSHCollector, build_command_pack
from app.collectors.evidence_builder import parse_evidence, build_evidence_pack
from app.services.rule_rca import run_rule_rca
from app.services.knowledge_service import KnowledgeService, build_signature_v2
from app.prompts.rca_prompt import build_llm_prompt
from app.repositories.incident_repo import IncidentRepository
from app.schemas.schemas import IncidentStatus

logger = get_logger(__name__)
settings = get_settings()


async def run_worker():
    """Main worker loop."""
    from app.core.logging import setup_logging
    setup_logging("DEBUG" if settings.app_debug else "INFO")
    
    print("[WORKER] Connecting to Redis queue...")
    redis = await get_redis()
    redis_svc = RedisService(redis)
    
    print("[WORKER] ✅ Worker is now polling for incidents...")
    logger.info("=" * 60)
    logger.info("WORKER STARTED — polling Redis queue for incidents...")
    logger.info("=" * 60)

    while True:
        try:
            incident_id = await redis_svc.pop_incident(timeout=settings.worker_poll_interval)
            if not incident_id:
                continue

            print(f"[WORKER] 📥 Picked up incident: {incident_id}")
            logger.info(f"{'=' * 60}")
            logger.info(f"[WORKER] Picked up incident: {incident_id}")
            logger.info(f"{'=' * 60}")

            async with get_session_factory()() as db:
                try:
                    await process_incident(db, redis_svc, incident_id)
                except Exception as e:
                    logger.error(f"[WORKER] ❌ Error processing incident {incident_id}: {e}", exc_info=True)
                    repo = IncidentRepository(db)
                    await repo.update_incident(incident_id, status=IncidentStatus.FAILED)
                    await repo.save_audit(
                        event_type="incident_processing_failed",
                        entity_type="incident", entity_id=incident_id,
                        details={"error": str(e)},
                    )
                    await db.commit()

        except asyncio.CancelledError:
            logger.info("[WORKER] Shutting down...")
            break
        except Exception as e:
            logger.error(f"[WORKER] Loop error: {e}", exc_info=True)
            await asyncio.sleep(5)


async def process_incident(db: AsyncSession, redis_svc: RedisService, incident_id: str):
    """Full incident processing pipeline with detailed logging."""
    repo = IncidentRepository(db)
    incident = await repo.get_incident(incident_id)
    if not incident:
        logger.error(f"[PIPELINE] Incident {incident_id} not found in DB")
        return

    # === ROUTE: Supervisor vs Host Resource ===
    if incident.domain_type == "SUPERVISOR":
        logger.info(f"[PIPELINE] 🔧 Routing to SUPERVISOR pipeline: {incident_id}")
        from app.workers.supervisor_worker import process_supervisor_incident
        await process_supervisor_incident(db, redis_svc, incident_id)
        return

    logger.info(f"[PIPELINE] 🖥️ Routing to HOST RESOURCE pipeline: {incident_id}")

    instance = incident.instance
    host = instance.split(":")[0] if ":" in instance else instance
    resource_type = incident.resource_type or "UNKNOWN"
    component_type = incident.component_type or "app"

    logger.info(f"[PIPELINE] ▶ START processing: alert={incident.alert_name} instance={instance} "
                f"resource={resource_type} host_role={component_type}")

    # ════════════════════════════════════════════════════════════════
    # PHASE 3: Evidence Collection
    # ════════════════════════════════════════════════════════════════
    logger.info(f"[PHASE 3] 📡 Evidence collection starting...")
    await repo.update_incident(incident_id, status=IncidentStatus.EVIDENCE_COLLECTING)
    await repo.save_incident_event(incident_id, "evidence_collection_started", {
        "resource_type": resource_type, "host_role": component_type,
    })
    await db.commit()
    await redis_svc.publish_event("status_changed", {
        "incident_id": incident_id, "status": IncidentStatus.EVIDENCE_COLLECTING,
    })

    # 3a. Prometheus snapshot
    logger.info(f"[PHASE 3] 📊 Querying Prometheus for {instance}...")
    prom = PrometheusClient()
    prometheus_snapshot = {}
    prometheus_trends = {}
    try:
        prometheus_snapshot = await prom.collect_host_snapshot(instance)
        prometheus_trends = await prom.collect_trends(instance)
        logger.info(f"[PHASE 3] ✅ Prometheus: {len(prometheus_snapshot)} metrics collected")
        for k, v in prometheus_snapshot.items():
            logger.info(f"[PHASE 3]    {k} = {v}")
    except Exception as e:
        logger.warning(f"[PHASE 3] ⚠️  Prometheus collection failed: {e}")

    # 3b. SSH evidence
    command_pack = build_command_pack(resource_type, component_type)
    logger.info(f"[PHASE 3] 🔌 SSH connecting to {host}, running {len(command_pack)} commands...")
    ssh_results = []
    try:
        collector = SSHCollector(host)
        ssh_results = collector.run_command_pack(command_pack)
        success_count = sum(1 for r in ssh_results if r.get("exit_code") == 0)
        fail_count = len(ssh_results) - success_count
        logger.info(f"[PHASE 3] ✅ SSH: {success_count} succeeded, {fail_count} failed out of {len(ssh_results)} commands")

        # Log failed commands
        for r in ssh_results:
            if r.get("exit_code") != 0 and r.get("exit_code") != -1:
                logger.warning(f"[PHASE 3]    ❌ {r.get('command_id')}: exit={r.get('exit_code')}")
    except Exception as e:
        logger.error(f"[PHASE 3] ❌ SSH collection failed: {e}")

    # 3c. Parse and save evidence
    parsed_evidence = parse_evidence(ssh_results)
    key_evidence = [e for e in parsed_evidence if e.get("is_key_evidence")]
    logger.info(f"[PHASE 3] 🔍 Parsed {len(parsed_evidence)} evidence items, {len(key_evidence)} key evidence")
    for ke in key_evidence:
        logger.info(f"[PHASE 3]    🔑 KEY: {ke.get('command_id')} (weight={ke.get('severity_weight', 0):.1f})")

    evidence_records = []
    for ev in parsed_evidence:
        evidence_records.append({
            "domain_type": ev.get("domain_type", "HOST"),
            "source_type": ev.get("source_type", "ssh"),
            "evidence_type": ev.get("evidence_type", ""),
            "command_id": ev.get("command_id", ""),
            "command_text": ev.get("command_text", ""),
            "raw_text": ev.get("raw_text", ""),
            "parsed_json": ev.get("parsed_json"),
            "severity_weight": ev.get("severity_weight", 0),
            "evidence_ref": ev.get("command_id", ""),
            "exit_code": ev.get("exit_code"),
            "duration_ms": ev.get("duration_ms"),
            "source_host": ev.get("source_host", host),
            "collector_name": "ssh_collector",
            "is_key_evidence": ev.get("is_key_evidence", False),
            "observed_at": datetime.now(timezone.utc),
        })

    # Save Prometheus as evidence too
    for metric_name, metric_value in prometheus_snapshot.items():
        evidence_records.append({
            "domain_type": "HOST",
            "source_type": "prometheus",
            "evidence_type": "prometheus_snapshot",
            "metric_name": metric_name,
            "metric_value": float(metric_value) if metric_value else None,
            "collector_name": "prometheus_client",
            "observed_at": datetime.now(timezone.utc),
        })

    await repo.save_evidence(incident_id, evidence_records)
    await repo.update_incident(incident_id, status=IncidentStatus.EVIDENCE_COLLECTED)
    await repo.save_incident_event(incident_id, "evidence_collected", {
        "ssh_commands": len(ssh_results),
        "prometheus_metrics": len(prometheus_snapshot),
        "key_evidence_count": len(key_evidence),
    })
    await db.commit()
    logger.info(f"[PHASE 3] ✅ Evidence saved to DB ({len(evidence_records)} records)")

    # ════════════════════════════════════════════════════════════════
    # CHECK: Did operator request to skip LLM?
    # ════════════════════════════════════════════════════════════════
    skip_key = f"agent:skip_llm:{incident_id}"
    if await redis_svc.redis.exists(skip_key):
        logger.info(f"[PHASE 4] ⏸️  LLM SKIPPED — operator requested skip. "
                    f"Waiting for manual 'Query LLM' trigger.")
        await repo.save_incident_event(incident_id, "llm_skipped_by_operator", {})
        await redis_svc.publish_event("status_changed", {
            "incident_id": incident_id, "status": IncidentStatus.EVIDENCE_COLLECTED,
        })
        return

    # Continue to LLM analysis
    await _run_llm_analysis(db, redis_svc, repo, incident, incident_id, host,
                            resource_type, component_type, parsed_evidence,
                            prometheus_snapshot, prometheus_trends)


async def _run_llm_analysis(
    db: AsyncSession, redis_svc: RedisService, repo: IncidentRepository,
    incident, incident_id: str, host: str,
    resource_type: str, component_type: str,
    parsed_evidence: list, prometheus_snapshot: dict, prometheus_trends: dict,
):
    """Phase 4: RCA analysis — extracted so it can be called by worker or API trigger."""
    instance = incident.instance

    # ════════════════════════════════════════════════════════════════
    # PHASE 4: RCA — Rule engine → Knowledge → ALWAYS LLM
    # ════════════════════════════════════════════════════════════════
    logger.info(f"[PHASE 4] 🧠 RCA starting...")
    await repo.update_incident(incident_id, status=IncidentStatus.ANALYZING)
    await redis_svc.publish_event("status_changed", {
        "incident_id": incident_id, "status": IncidentStatus.ANALYZING,
    })

    # 4a. Rule-based RCA (for reference, not final)
    logger.info(f"[PHASE 4] 📐 Running rule-based RCA...")
    rule_result = run_rule_rca(resource_type, incident.alert_name, parsed_evidence, prometheus_snapshot)
    if rule_result.matched:
        logger.info(f"[PHASE 4] 📐 Rule matched: {rule_result.canonical_root_cause} "
                    f"(confidence={rule_result.confidence:.2f})")
        logger.info(f"[PHASE 4]    Explanation: {rule_result.explanation}")
    else:
        logger.info(f"[PHASE 4] 📐 No rule match")

    # 4b. Knowledge lookup (for context, not final)
    logger.info(f"[PHASE 4] 📚 Knowledge lookup...")
    knowledge_svc = KnowledgeService(db)
    signature_v2 = None
    if rule_result.matched:
        signature_v2 = build_signature_v2(
            "HOST", incident.alert_name, incident.entity_name or host,
            rule_result.canonical_root_cause.split("_")[0] if rule_result.canonical_root_cause else "",
            rule_result.issue_subtype, rule_result.canonical_root_cause,
        )

    knowledge_result = await knowledge_svc.lookup(
        domain_type="HOST", alert_name=incident.alert_name,
        resource_type=resource_type, instance=instance,
        entity_name=incident.entity_name or "",
        canonical_root_cause=rule_result.canonical_root_cause if rule_result.matched else None,
        signature_v2=signature_v2,
    )
    logger.info(f"[PHASE 4] 📚 Knowledge result: source={knowledge_result['source']} "
                f"confidence={knowledge_result.get('confidence', 0):.2f}")

    # 4c. ALWAYS send to LLM — build evidence pack and prompt
    logger.info(f"[PHASE 4] 🤖 Building evidence pack for LLM...")

    # Get recent history for context
    recent = await repo.find_recent_similar(incident.alert_name, instance, limit=3)
    known_history = [
        {"created_at": str(r.created_at), "root_cause": r.root_cause, "final_status": r.final_status}
        for r in recent if r.id != incident_id  # exclude current incident
    ]
    logger.info(f"[PHASE 4] 📚 Found {len(known_history)} recent similar incidents for context")

    incident_info = {
        "alert_name": incident.alert_name,
        "instance": instance,
        "severity": incident.severity,
        "resource_type": resource_type,
        "component_type": component_type,
        "service_name": incident.service_name or "",
    }

    evidence_pack = build_evidence_pack(
        incident_info, prometheus_snapshot, prometheus_trends,
        parsed_evidence, known_history,
    )

    # Add rule-based hints to help LLM
    if rule_result.matched:
        evidence_pack += f"\n\n[RULE_ENGINE_HINT]\nRule engine đã phát hiện: {rule_result.root_cause}\n"
        evidence_pack += f"Canonical: {rule_result.canonical_root_cause}\n"
        evidence_pack += f"Confidence: {rule_result.confidence}\n"
        evidence_pack += f"Explanation: {rule_result.explanation}\n[/RULE_ENGINE_HINT]"

    prompt = build_llm_prompt(evidence_pack)
    prompt_length = len(prompt)
    logger.info(f"[PHASE 4] 📝 Prompt built: {prompt_length} characters")

    # Save prompt to DB for debugging
    await repo.update_incident(incident_id, llm_prompt_text=prompt)
    await db.commit()

    # 4d. Call LLM
    logger.info(f"[PHASE 4] 🤖 Sending prompt to LLM ({settings.gemini_model})...")
    llm_response = None
    llm_raw_text = None
    try:
        llm_client = LLMClient()
        llm_response, llm_raw_text = await llm_client.analyze_incident(prompt)
        
        # Save raw response to DB for debugging (always, even if parse failed)
        if llm_raw_text:
            await repo.update_incident(incident_id, llm_raw_response=llm_raw_text)
            await db.commit()
        
        if llm_response:
            logger.info(f"[PHASE 4] ✅ LLM response parsed OK:")
            logger.info(f"[PHASE 4]    confidence={llm_response.confidence:.2f}")
            logger.info(f"[PHASE 4]    canonical_root_cause={llm_response.canonical_root_cause}")
            logger.info(f"[PHASE 4]    remediation_options={len(llm_response.remediation_options)}")
            logger.info(f"[PHASE 4]    symptom: {llm_response.symptom}")
            logger.info(f"[PHASE 4]    root_cause: {llm_response.root_cause_hypothesis}")
            logger.info(f"[PHASE 4]    summary: {llm_response.summary}")
            for i, opt in enumerate(llm_response.remediation_options):
                logger.info(f"[PHASE 4]    Option {i+1}: {opt.title} | {len(opt.commands)} cmds | risk={opt.risk_level}")
        else:
            logger.error(f"[PHASE 4] ❌ LLM parse failed. Raw saved to DB for debugging.")
            if llm_raw_text:
                logger.error(f"[PHASE 4]    Raw first 500 chars: {llm_raw_text[:500]}")
    except Exception as e:
        logger.error(f"[PHASE 4] ❌ LLM call exception: {e}", exc_info=True)

    # 4e. Process LLM result
    if llm_response:
        root_cause = llm_response.root_cause_hypothesis
        canonical_root_cause = llm_response.canonical_root_cause
        issue_subtype = llm_response.issue_subtype
        confidence = llm_response.confidence
        summary = llm_response.summary

        signature_v2 = build_signature_v2(
            "HOST", incident.alert_name, incident.entity_name or host,
            llm_response.suspected_service or "",
            issue_subtype, canonical_root_cause,
        )

        await repo.update_incident(incident_id,
            root_cause=root_cause,
            immediate_cause=llm_response.immediate_cause,
            canonical_root_cause=canonical_root_cause,
            issue_subtype=issue_subtype,
            root_cause_signature_v2=signature_v2,
            root_cause_summary=summary,
            llm_confidence=confidence,
            rca_level=llm_response.rca_level,
            verification_status=llm_response.verification_status,
            knowledge_source="llm",
            summary=llm_response.operator_message_vi or summary,
            ai_analysis_json=llm_response.model_dump(),
            status=IncidentStatus.ACTION_PROPOSED,
        )

        # Save remediation options
        options = []
        for opt in llm_response.remediation_options:
            options.append({
                "priority": opt.priority,
                "title": opt.title,
                "description": opt.description,
                "risk_level": opt.risk_level,
                "needs_approval": opt.needs_approval,
                "action_type": opt.action_type,
                "target": opt.target,
                "params_json": opt.params,
                "commands_json": opt.commands,
                "expected_effect": opt.expected_effect,
                "rollback_commands_json": opt.rollback_commands,
                "pre_checks_json": opt.pre_checks,
                "post_checks_json": opt.post_checks,
                "warnings_json": opt.warnings,
                "source": "llm",
            })
        await repo.save_remediation_options(incident_id, options)
        logger.info(f"[PHASE 4] ✅ Saved {len(options)} remediation options to DB")
        logger.info(f"[PHASE 4] ✅ Incident status → action_proposed")

    else:
        # LLM failed — fallback to rule-based if available
        logger.warning(f"[PHASE 4] ⚠️  LLM failed, falling back to rule-based RCA")

        if rule_result.matched:
            sig = signature_v2 or build_signature_v2(
                "HOST", incident.alert_name, incident.entity_name or host,
                "", rule_result.issue_subtype, rule_result.canonical_root_cause,
            )
            await repo.update_incident(incident_id,
                root_cause=rule_result.root_cause,
                canonical_root_cause=rule_result.canonical_root_cause,
                issue_subtype=rule_result.issue_subtype,
                root_cause_signature_v2=sig,
                root_cause_summary=rule_result.explanation,
                llm_confidence=rule_result.confidence,
                rca_level="probable_root_cause",
                knowledge_source="rule_fallback",
                summary=rule_result.explanation,
                status=IncidentStatus.ACTION_PROPOSED,
            )
            default_options = _generate_rule_based_options(resource_type, rule_result, incident.alert_name, host)
            await repo.save_remediation_options(incident_id, default_options)
            logger.info(f"[PHASE 4] ✅ Used rule-based fallback with {len(default_options)} options")
        else:
            await repo.update_incident(incident_id,
                status=IncidentStatus.ANALYSIS_FAILED,
                knowledge_source="none",
                summary="LLM không phản hồi và rule engine không match. Cần kiểm tra thủ công.",
            )
            await repo.save_audit(
                event_type="analysis_failed", entity_type="incident", entity_id=incident_id,
                details={"reason": "llm_failed_and_no_rule_match"},
            )
            logger.error(f"[PHASE 4] ❌ Analysis failed — no LLM response and no rule match")

    await repo.save_incident_event(incident_id, "analysis_completed", {
        "knowledge_source": "llm" if llm_response else ("rule_fallback" if rule_result.matched else "none"),
        "rule_matched": rule_result.matched,
        "llm_success": llm_response is not None,
        "prompt_length": prompt_length,
    })
    await db.commit()

    final_status = IncidentStatus.ACTION_PROPOSED if llm_response or rule_result.matched else IncidentStatus.ANALYSIS_FAILED
    await redis_svc.publish_event("incident_analyzed", {
        "incident_id": incident_id, "status": final_status,
    })

    logger.info(f"[PIPELINE] ✅ DONE processing incident {incident_id} → status={final_status}")
    logger.info(f"{'=' * 60}")


def _generate_rule_based_options(resource_type: str, rule_result, alert_name: str, host: str) -> list[dict]:
    """Generate safe default remediation options from rule-based RCA."""
    options = []
    rt = resource_type.upper()

    if rt == "CPU":
        if "backup" in (rule_result.canonical_root_cause or ""):
            options = [
                {"priority": 1, "title": "Chờ backup hoàn tất",
                 "description": "Đợi backup job chạy xong, CPU sẽ tự giảm",
                 "risk_level": "low", "needs_approval": False, "action_type": "wait",
                 "commands_json": [], "source": "rule"},
                {"priority": 2, "title": "Giảm nice level backup process",
                 "description": "Renice backup process để giảm priority CPU",
                 "risk_level": "low", "needs_approval": True, "action_type": "renice",
                 "commands_json": ["renice 19 -p $(pgrep -f 'rsync|tar|gzip' | head -1)"],
                 "rollback_commands_json": [], "source": "rule"},
                {"priority": 3, "title": "Dời lịch backup sang giờ thấp tải",
                 "description": "Sửa crontab để chạy backup lúc ít traffic hơn",
                 "risk_level": "low", "needs_approval": True, "action_type": "manual",
                 "commands_json": [], "source": "rule"},
            ]
        else:
            options = [
                {"priority": 1, "title": "Restart service nghi vấn",
                 "description": "Restart service đang chiếm CPU cao nhất",
                 "risk_level": "medium", "needs_approval": True, "action_type": "restart",
                 "commands_json": ["# Xác định service cần restart trước"], "source": "rule"},
                {"priority": 2, "title": "Kill process runaway",
                 "risk_level": "high", "needs_approval": True, "action_type": "kill",
                 "commands_json": ["# ps aux --sort=-%cpu | head -5"], "source": "rule"},
                {"priority": 3, "title": "Theo dõi thêm 15 phút",
                 "risk_level": "low", "needs_approval": False, "action_type": "wait",
                 "commands_json": [], "source": "rule"},
            ]
    elif rt == "DISK":
        options = [
            {"priority": 1, "title": "Cleanup log files cũ",
             "risk_level": "low", "needs_approval": True, "action_type": "cleanup",
             "commands_json": ["find /var/log -name '*.log.*' -mtime +7 -delete",
                              "find /tmp -type f -mtime +3 -delete"], "source": "rule"},
            {"priority": 2, "title": "Restart service giữ deleted files",
             "risk_level": "medium", "needs_approval": True, "action_type": "restart",
             "commands_json": ["# Xem lsof +L1 trước"], "source": "rule"},
            {"priority": 3, "title": "Cleanup backup cũ",
             "risk_level": "medium", "needs_approval": True, "action_type": "cleanup",
             "commands_json": ["find /backup -name '*.gz' -mtime +14 -ls"], "source": "rule"},
        ]
    elif rt == "RAM":
        options = [
            {"priority": 1, "title": "Restart service chiếm RAM cao",
             "risk_level": "medium", "needs_approval": True, "action_type": "restart",
             "commands_json": ["# ps aux --sort=-rss | head -5"], "source": "rule"},
            {"priority": 2, "title": "Clear page cache",
             "risk_level": "low", "needs_approval": True, "action_type": "cache_clear",
             "commands_json": ["sync", "echo 3 > /proc/sys/vm/drop_caches"],
             "rollback_commands_json": [], "source": "rule"},
            {"priority": 3, "title": "Theo dõi memory trend",
             "risk_level": "low", "needs_approval": False, "action_type": "wait",
             "commands_json": [], "source": "rule"},
        ]

    while len(options) < 3:
        options.append({
            "priority": len(options) + 1, "title": "Kiểm tra thủ công",
            "risk_level": "low", "needs_approval": False, "action_type": "manual",
            "commands_json": [], "source": "rule",
        })

    return options


async def trigger_llm_analysis(db: AsyncSession, redis_svc: RedisService, incident_id: str):
    """Trigger LLM analysis for an incident that has evidence but skipped LLM.
    Called by API when operator clicks 'Query LLM'.
    """
    repo = IncidentRepository(db)
    incident = await repo.get_incident(incident_id)
    if not incident:
        return {"status": "error", "message": "Incident not found"}

    if incident.status not in (IncidentStatus.EVIDENCE_COLLECTED, IncidentStatus.ANALYSIS_FAILED,
                                IncidentStatus.MANUAL_REQUIRED):
        return {"status": "error", "message": f"Cannot query LLM in status '{incident.status}'"}

    # Remove skip flag
    await redis_svc.redis.delete(f"agent:skip_llm:{incident_id}")

    instance = incident.instance
    host = instance.split(":")[0] if ":" in instance else instance
    resource_type = incident.resource_type or "UNKNOWN"
    component_type = incident.component_type or "app"

    # Reload evidence from DB
    evidence_rows = await repo.get_evidence(incident_id)
    parsed_evidence = []
    prometheus_snapshot = {}

    for ev in evidence_rows:
        if ev.source_type == "prometheus" and ev.metric_name and ev.metric_value is not None:
            prometheus_snapshot[ev.metric_name] = ev.metric_value
        elif ev.source_type == "ssh":
            parsed_evidence.append({
                "command_id": ev.command_id or "",
                "command_text": ev.command_text or "",
                "evidence_type": ev.evidence_type or "",
                "raw_text": ev.raw_text or "",
                "exit_code": ev.exit_code,
                "duration_ms": ev.duration_ms,
                "source_host": ev.source_host or host,
                "parsed_json": ev.parsed_json or {},
                "severity_weight": ev.severity_weight or 0,
                "is_key_evidence": ev.is_key_evidence or False,
            })

    logger.info(f"[TRIGGER] 🤖 Triggering LLM for {incident_id} with {len(parsed_evidence)} evidence items")

    await repo.save_incident_event(incident_id, "llm_triggered_by_operator", {})
    await db.commit()

    await _run_llm_analysis(
        db, redis_svc, repo, incident, incident_id, host,
        resource_type, component_type, parsed_evidence,
        prometheus_snapshot, {},
    )

    return {"status": "ok", "message": "LLM analysis triggered"}
