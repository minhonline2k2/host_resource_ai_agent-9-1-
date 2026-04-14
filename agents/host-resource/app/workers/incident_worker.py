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
    """Main worker loop — polls both incident queue and execution queue."""
    from app.core.logging import setup_logging
    setup_logging("DEBUG" if settings.app_debug else "INFO")
    
    print("[WORKER] Connecting to Redis queues...")
    redis = await get_redis()
    redis_svc = RedisService(redis)
    
    print("[WORKER] ✅ Polling: incidents + executions")
    logger.info("=" * 60)
    logger.info("WORKER STARTED — polling incident + execution queues")
    logger.info("=" * 60)

    # Run both loops concurrently
    await asyncio.gather(
        _incident_loop(redis_svc),
        _execution_loop(redis_svc),
    )


async def _execution_loop(redis_svc: RedisService):
    """Poll execution queue and run approved commands via SSH."""
    import json as _json
    logger.info("[EXEC] Execution loop started — polling agent:queue:host_resource:execute")
    while True:
        try:
            raw = await redis_svc.pop_exec_job(timeout=2)
            if not raw:
                continue

            job = _json.loads(raw)
            incident_id = job.get("incident_id", "")
            option_id = job.get("option_id", "")
            instance = job.get("instance", "")
            commands = job.get("commands", [])
            host = instance.split(":")[0] if ":" in instance else instance

            logger.info(f"[EXEC] 🔧 Executing {len(commands)} commands for {incident_id} on {host}")

            if not commands:
                logger.warning(f"[EXEC] No commands to execute")
                continue

            # Execute via SSH
            from app.collectors.ssh_collector import SSHCollector
            collector = SSHCollector(host)
            results = []
            overall_success = True

            for i, cmd in enumerate(commands):
                logger.info(f"[EXEC]   Step {i+1}/{len(commands)}: {cmd[:100]}")
                r = collector.run_command(cmd)
                success = r.get("exit_code", -1) == 0
                if not success:
                    overall_success = False
                results.append({
                    "step_no": i + 1,
                    "command": cmd,
                    "stdout": r.get("stdout", "")[:5000],
                    "stderr": r.get("stderr", "")[:2000],
                    "exit_code": r.get("exit_code", -1),
                    "success": success,
                })
                logger.info(f"[EXEC]   → exit={r.get('exit_code')} {'✅' if success else '❌'}")

            # Save results to DB
            async with get_session_factory()() as db:
                from app.repositories.incident_repo import IncidentRepository
                from app.schemas.schemas import IncidentStatus
                repo = IncidentRepository(db)

                for r in results:
                    await repo.save_execution_log(
                        incident_id=incident_id,
                        action_proposal_id=option_id,
                        step_no=r["step_no"],
                        step_name=r["command"][:200],
                        status="success" if r["success"] else "failed",
                        command=r["command"],
                        stdout=r["stdout"],
                        stderr=r["stderr"],
                        exit_code=r["exit_code"],
                    )

                new_status = "executed" if overall_success else "execution_failed"
                await repo.update_incident(incident_id, status=new_status)
                await repo.save_incident_event(incident_id, "execution_completed", {
                    "success": overall_success,
                    "steps": len(commands),
                    "option_id": option_id,
                })
                await db.commit()

            # Push result back to orchestrator
            try:
                from app.core.orchestrator import push_result_to_orchestrator
                await push_result_to_orchestrator({
                    "incident_id": incident_id,
                    "agent_id": settings.agent_id,
                    "status": new_status,
                    "execution_results": results,
                })
            except Exception:
                pass

            logger.info(f"[EXEC] {'✅' if overall_success else '❌'} Execution done: {new_status}")

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"[EXEC] Error: {e}", exc_info=True)
            await asyncio.sleep(3)


async def _incident_loop(redis_svc: RedisService):
    """Poll incident queue for new analysis jobs."""
    import json as _json

    while True:
        try:
            raw = await redis_svc.pop_incident(timeout=settings.worker_poll_interval)
            if not raw:
                continue

            # Parse: could be plain incident_id (standalone) or JSON job (orchestrator)
            import json as _json
            job = None
            incident_id = raw
            try:
                job = _json.loads(raw)
                if isinstance(job, dict):
                    incident_id = job.get("incident_id", raw)
            except (ValueError, TypeError):
                job = None

            print(f"[WORKER] 📥 Picked up: {incident_id}")
            logger.info(f"{'=' * 60}")
            logger.info(f"[WORKER] incident_id={incident_id} orchestrated={job is not None}")
            logger.info(f"{'=' * 60}")

            async with get_session_factory()() as db:
                try:
                    # If orchestrated job, create local incident if not exists
                    if job and isinstance(job, dict):
                        repo = IncidentRepository(db)
                        existing = await repo.get_incident(incident_id)
                        if not existing:
                            await repo.create_incident(
                                id_override=incident_id,
                                alert_name=job.get("alert_name", "Unknown"),
                                title=f"{job.get('alert_name', '')} on {job.get('instance', '')}",
                                status=IncidentStatus.NEW,
                                severity=job.get("severity", "warning"),
                                instance=job.get("instance", ""),
                                resource_type=job.get("resource_type", "UNKNOWN"),
                                domain_type="HOST",
                                component_type="app",
                                service_name=job.get("labels", {}).get("service", ""),
                                entity_name=job.get("instance", "").split(":")[0],
                                context_json={"labels": job.get("labels", {}), "annotations": job.get("annotations", {})},
                            )
                            await db.commit()
                            logger.info(f"[WORKER] ✅ Created local incident for {incident_id}")

                    await process_incident(db, redis_svc, incident_id)
                except Exception as e:
                    logger.error(f"[WORKER] ❌ Error: {e}", exc_info=True)
                    try:
                        repo = IncidentRepository(db)
                        await repo.update_incident(incident_id, status=IncidentStatus.FAILED)
                        await db.commit()
                    except Exception:
                        pass

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

    # 4e. Process LLM result — use parsed Pydantic OR raw JSON fallback
    llm_data = None  # raw dict from LLM
    if llm_response:
        llm_data = llm_response.model_dump()
    elif llm_raw_text:
        # Pydantic parse failed but raw JSON might be valid — use it directly
        try:
            import json as _json
            cleaned = llm_raw_text.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
                if cleaned.endswith("```"):
                    cleaned = cleaned[:-3]
                cleaned = cleaned.strip()
            llm_data = _json.loads(cleaned)
            logger.info(f"[PHASE 4] 🔄 Pydantic failed but raw JSON valid — using raw dict directly")
            logger.info(f"[PHASE 4]    Keys: {list(llm_data.keys())}")
            logger.info(f"[PHASE 4]    Options: {len(llm_data.get('remediation_options', []))}")
        except Exception as e:
            logger.error(f"[PHASE 4] ❌ Raw JSON also invalid: {e}")

    if llm_data:
        root_cause = llm_data.get("root_cause_hypothesis", "") or llm_data.get("root_cause", "")
        canonical_root_cause = llm_data.get("canonical_root_cause", "")
        issue_subtype = llm_data.get("issue_subtype", "")
        confidence = float(llm_data.get("confidence", 0) or 0)
        summary = llm_data.get("summary", "")
        operator_msg = llm_data.get("operator_message_vi", "") or summary

        signature_v2 = build_signature_v2(
            "HOST", incident.alert_name, incident.entity_name or host,
            llm_data.get("suspected_service", "") or "",
            issue_subtype, canonical_root_cause,
        )

        await repo.update_incident(incident_id,
            root_cause=root_cause,
            immediate_cause=llm_data.get("immediate_cause", ""),
            canonical_root_cause=canonical_root_cause,
            issue_subtype=issue_subtype,
            root_cause_signature_v2=signature_v2,
            root_cause_summary=summary,
            llm_confidence=confidence,
            rca_level=llm_data.get("rca_level", "probable_root_cause"),
            verification_status=llm_data.get("verification_status", "medium"),
            knowledge_source="llm",
            summary=operator_msg,
            ai_analysis_json=llm_data,
            status=IncidentStatus.ACTION_PROPOSED,
        )

        # Save remediation options from LLM
        raw_options = llm_data.get("remediation_options", [])
        options = []
        for i, opt in enumerate(raw_options):
            # Normalize list fields
            cmds = opt.get("commands", [])
            if isinstance(cmds, str): cmds = [cmds] if cmds.strip() else []
            rollback = opt.get("rollback_commands", [])
            if isinstance(rollback, str): rollback = [rollback] if rollback.strip() else []
            pre = opt.get("pre_checks", [])
            if isinstance(pre, str): pre = [pre] if pre.strip() else []
            post = opt.get("post_checks", [])
            if isinstance(post, str): post = [post] if post.strip() else []
            warns = opt.get("warnings", [])
            if isinstance(warns, str): warns = [warns] if warns.strip() else []

            options.append({
                "priority": opt.get("priority", i + 1),
                "title": opt.get("title", f"Option {i+1}"),
                "description": opt.get("description", ""),
                "risk_level": opt.get("risk_level", "medium"),
                "needs_approval": opt.get("needs_approval", True),
                "action_type": opt.get("action_type", ""),
                "target": opt.get("target", ""),
                "params_json": opt.get("params", {}),
                "commands_json": cmds,
                "expected_effect": opt.get("expected_effect", ""),
                "rollback_commands_json": rollback,
                "pre_checks_json": pre,
                "post_checks_json": post,
                "warnings_json": warns,
                "source": "llm",
            })
        await repo.save_remediation_options(incident_id, options)
        logger.info(f"[PHASE 4] ✅ Saved {len(options)} LLM remediation options to DB")
        for i, o in enumerate(options):
            logger.info(f"[PHASE 4]    DB Option {i+1}: {o['title']} | {len(o['commands_json'])} cmds")
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
        "knowledge_source": "llm" if llm_data else ("rule_fallback" if rule_result.matched else "none"),
        "rule_matched": rule_result.matched,
        "llm_success": llm_data is not None,
        "prompt_length": prompt_length,
    })
    await db.commit()

    final_status = IncidentStatus.ACTION_PROPOSED if llm_data or rule_result.matched else IncidentStatus.ANALYSIS_FAILED
    await redis_svc.publish_event("incident_analyzed", {
        "incident_id": incident_id, "status": final_status,
    })

    # Push result to orchestrator
    try:
        from app.core.orchestrator import push_result_to_orchestrator
        inc_updated = await repo.get_incident(incident_id)
        opts_db = await repo.get_remediation_options(incident_id)
        await push_result_to_orchestrator({
            "incident_id": incident_id,
            "agent_id": getattr(settings, 'agent_id', 'host-resource-agent'),
            "status": final_status,
            "root_cause": inc_updated.root_cause if inc_updated else None,
            "root_cause_summary": inc_updated.root_cause_summary if inc_updated else None,
            "canonical_root_cause": inc_updated.canonical_root_cause if inc_updated else None,
            "confidence": inc_updated.llm_confidence if inc_updated else None,
            "knowledge_source": inc_updated.knowledge_source if inc_updated else None,
            "operator_message_vi": inc_updated.summary if inc_updated else None,
            "ai_analysis_json": inc_updated.ai_analysis_json if inc_updated else None,
            "llm_prompt_text": inc_updated.llm_prompt_text if inc_updated else None,
            "llm_raw_response": inc_updated.llm_raw_response if inc_updated else None,
            "evidence_count": len(parsed_evidence),
            "remediation_options": [
                {"priority": o.priority, "title": o.title, "description": o.description,
                 "risk_level": o.risk_level, "commands_json": o.commands_json or [],
                 "expected_effect": o.expected_effect, "warnings_json": o.warnings_json or [],
                 "rollback_commands_json": o.rollback_commands_json or [], "source": o.source}
                for o in opts_db
            ],
        })
    except Exception as e:
        logger.warning(f"[ORCH] Push result failed: {e}")

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
