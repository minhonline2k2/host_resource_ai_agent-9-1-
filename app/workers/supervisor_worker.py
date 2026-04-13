"""Supervisor incident worker: processes supervisor alerts through the full pipeline."""

from __future__ import annotations

import re
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.redis_client import RedisService
from app.clients.llm_client import LLMClient
from app.collectors.ssh_collector import SSHCollector
from app.collectors.supervisor_collector import (
    build_supervisor_command_pack,
    parse_supervisor_status,
    parse_supervisor_config,
)
from app.prompts.supervisor_prompt import (
    build_supervisor_evidence_pack,
    build_supervisor_llm_prompt,
)
from app.services.supervisor_rule_rca import run_supervisor_rule_rca
from app.repositories.incident_repo import IncidentRepository
from app.schemas.schemas import IncidentStatus

logger = get_logger(__name__)
settings = get_settings()


async def process_supervisor_incident(
    db: AsyncSession, redis_svc: RedisService, incident_id: str
):
    """Full pipeline for supervisor incidents."""
    repo = IncidentRepository(db)
    incident = await repo.get_incident(incident_id)
    if not incident:
        logger.error(f"[SUP_PIPELINE] Incident {incident_id} not found in DB")
        return

    instance = incident.instance
    host = instance.split(":")[0] if ":" in instance else instance
    context = incident.context_json or {}
    labels = context.get("labels", {})

    # Extract supervisor-specific fields from labels
    process_name = labels.get("process_name", labels.get("name", ""))
    group_name = labels.get("group_name", labels.get("group", process_name))

    if not process_name:
        # Try to extract from alert_name
        if incident.entity_name:
            process_name = incident.entity_name
        else:
            process_name = "unknown"

    logger.info(f"[SUP_PIPELINE] ▶ START supervisor incident: process={process_name} "
                f"group={group_name} host={host}")

    # ════════════════════════════════════════════════════════════════
    # PHASE 1: SSH Evidence Collection
    # ════════════════════════════════════════════════════════════════
    logger.info(f"[SUP_PHASE1] 📡 Evidence collection starting for {process_name}...")
    await repo.update_incident(incident_id, status=IncidentStatus.EVIDENCE_COLLECTING)
    await repo.save_incident_event(incident_id, "supervisor_evidence_started", {
        "process_name": process_name, "group_name": group_name,
    })
    await db.commit()
    await redis_svc.publish_event("status_changed", {
        "incident_id": incident_id, "status": IncidentStatus.EVIDENCE_COLLECTING,
    })

    # Build command pack
    command_pack = build_supervisor_command_pack(process_name)
    logger.info(f"[SUP_PHASE1] 🔌 SSH connecting to {host}, running {len(command_pack)} commands...")

    ssh_results = []
    try:
        collector = SSHCollector(host)
        ssh_results = collector.run_command_pack(command_pack)
        success_count = sum(1 for r in ssh_results if r.get("exit_code") == 0)
        logger.info(f"[SUP_PHASE1] ✅ SSH: {success_count}/{len(ssh_results)} commands succeeded")
    except Exception as e:
        logger.error(f"[SUP_PHASE1] ❌ SSH collection failed: {e}")

    # Parse SSH results into named evidence
    evidence_map = {}
    for r in ssh_results:
        cmd_id = r.get("command_id", "")
        evidence_map[cmd_id] = r.get("raw_text", "")

    # Parse supervisor status
    sup_status_raw = evidence_map.get("sup_status_process", evidence_map.get("sup_status_all", ""))
    sup_status = parse_supervisor_status(sup_status_raw)

    # Find specific process status
    proc_info = None
    for p in sup_status.get("processes", []):
        if p["name"] == process_name:
            proc_info = p
            break

    process_state = proc_info["state"] if proc_info else "UNKNOWN"
    process_pid = proc_info.get("pid", 0) if proc_info else 0
    process_uptime = proc_info.get("uptime", "0:00:00") if proc_info else "0:00:00"
    process_exit_code = proc_info.get("exit_code") if proc_info else None

    # Parse uptime to seconds
    uptime_sec = 0
    if process_uptime:
        parts = process_uptime.split(":")
        try:
            if len(parts) == 3:
                uptime_sec = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
            elif len(parts) == 2:
                uptime_sec = int(parts[0]) * 60 + int(parts[1])
        except ValueError:
            pass

    # Determine exit_code
    exit_code = process_exit_code if process_exit_code is not None else 1

    # Parse supervisor config
    sup_config_raw = evidence_map.get("sup_config", "")
    sup_config = parse_supervisor_config(sup_config_raw)

    # Extract evidence data
    stderr_content = evidence_map.get("sup_stderr", "")
    stdout_content = evidence_map.get("sup_stdout", "")
    mem_free_mb = evidence_map.get("sup_mem_free", "0").strip()
    mem_detail = evidence_map.get("sup_mem_detail", "")
    disk_pct = evidence_map.get("sup_disk_usage", "0%").strip()
    disk_detail = evidence_map.get("sup_disk_detail", "")
    syslog_content = evidence_map.get("sup_syslog_oom", "")
    supervisord_log = evidence_map.get("sup_supervisord_log", "")
    dmesg_recent = evidence_map.get("sup_dmesg_recent", "")
    top_mem = evidence_map.get("sup_top_mem", "")
    top_cpu = evidence_map.get("sup_top_cpu", "")
    recent_restarts = evidence_map.get("sup_recent_restarts", "")
    proc_detail = evidence_map.get("sup_proc_detail", "")
    proc_env = evidence_map.get("sup_proc_env", "")
    restart_history = evidence_map.get("sup_restart_history", "")
    network_info = evidence_map.get("sup_network", "")
    journal_log = evidence_map.get("sup_journal", "")
    uptime_load = evidence_map.get("sup_uptime_load", "")
    referenced_paths = evidence_map.get("sup_referenced_paths", "")
    workdir_files = evidence_map.get("sup_workdir_files", "")

    # Detect OOM and signal flags from syslog
    syslog_lower = syslog_content.lower()
    oom_flag = "oom" in syslog_lower or "out of memory" in syslog_lower
    signal_flag = "signal" in syslog_lower or "killed" in syslog_lower
    signal = ""
    if signal_flag:
        sig_match = re.search(r'signal\s+(\d+|SIG\w+)', syslog_content, re.IGNORECASE)
        if sig_match:
            signal = sig_match.group(1)

    # Count restarts from supervisord log
    restart_count = 0
    if recent_restarts:
        restart_count = len([l for l in recent_restarts.split("\n")
                           if "exited" in l.lower() or "starting" in l.lower()])

    logger.info(f"[SUP_PHASE1] 📊 Process state: {process_state}, exit={exit_code}, "
                f"uptime={uptime_sec}s, restarts={restart_count}, "
                f"oom={oom_flag}, signal={signal_flag}")

    # Save evidence to DB
    evidence_records = []
    for r in ssh_results:
        evidence_records.append({
            "domain_type": "SUPERVISOR",
            "source_type": "ssh",
            "evidence_type": r.get("evidence_type", ""),
            "command_id": r.get("command_id", ""),
            "command_text": r.get("command_text", ""),
            "raw_text": r.get("raw_text", ""),
            "exit_code": r.get("exit_code"),
            "duration_ms": r.get("duration_ms"),
            "source_host": r.get("source_host", host),
            "collector_name": "supervisor_ssh_collector",
            "is_key_evidence": r.get("command_id") in ("sup_stderr", "sup_stdout", "sup_status_process", "sup_config"),
            "observed_at": datetime.now(timezone.utc),
        })

    await repo.save_evidence(incident_id, evidence_records)
    await repo.update_incident(incident_id, status=IncidentStatus.EVIDENCE_COLLECTED)
    await repo.save_incident_event(incident_id, "supervisor_evidence_collected", {
        "ssh_commands": len(ssh_results),
        "process_state": process_state,
        "exit_code": exit_code,
        "restart_count": restart_count,
    })
    await db.commit()
    logger.info(f"[SUP_PHASE1] ✅ Evidence saved ({len(evidence_records)} records)")

    # ════════════════════════════════════════════════════════════════
    # CHECK: skip LLM?
    # ════════════════════════════════════════════════════════════════
    skip_key = f"agent:skip_llm:{incident_id}"
    if await redis_svc.redis.exists(skip_key):
        logger.info(f"[SUP_PHASE2] ⏸️ LLM SKIPPED — operator requested")
        await repo.save_incident_event(incident_id, "llm_skipped_by_operator", {})
        return

    # ════════════════════════════════════════════════════════════════
    # PHASE 2: Rule-based pre-analysis (Guardrails)
    # ════════════════════════════════════════════════════════════════
    logger.info(f"[SUP_PHASE2] 📐 Running guardrails rules...")
    rule_result = run_supervisor_rule_rca(
        process_name=process_name,
        exit_code=exit_code,
        signal=signal,
        uptime_sec=uptime_sec,
        restart_count=restart_count,
        oom_flag=oom_flag,
        signal_flag=signal_flag,
        disk_pct=disk_pct,
        stderr_content=stderr_content,
        stdout_content=stdout_content,
    )

    if rule_result.matched:
        logger.info(f"[SUP_PHASE2] 📐 Rule matched: [{rule_result.category}] "
                    f"{rule_result.summary_vi} (conf={rule_result.confidence:.2f})")
    else:
        logger.info(f"[SUP_PHASE2] 📐 No rule match — proceeding to LLM")

    # ════════════════════════════════════════════════════════════════
    # PHASE 3: Build evidence pack → call LLM (1 lần duy nhất)
    # ════════════════════════════════════════════════════════════════
    logger.info(f"[SUP_PHASE3] 🤖 Building supervisor prompt...")
    await repo.update_incident(incident_id, status=IncidentStatus.ANALYZING)
    await redis_svc.publish_event("status_changed", {
        "incident_id": incident_id, "status": IncidentStatus.ANALYZING,
    })

    alert_time = incident.created_at.isoformat() if incident.created_at else datetime.now(timezone.utc).isoformat()

    evidence_pack = build_supervisor_evidence_pack(
        process_name=process_name,
        group_name=group_name,
        status=process_state,
        exit_code=exit_code,
        signal=signal,
        uptime_sec=uptime_sec,
        retry_count=restart_count,
        alert_time=alert_time,
        stderr_content=stderr_content,
        stdout_content=stdout_content,
        supervisor_conf=sup_config_raw,
        mem_free_mb=mem_free_mb,
        disk_pct=disk_pct,
        oom_flag=oom_flag,
        signal_flag=signal_flag,
        supervisord_log=supervisord_log,
        dmesg_recent=dmesg_recent,
        top_mem=top_mem,
        proc_detail=proc_detail,
        proc_env=proc_env,
        restart_history=restart_history,
        top_cpu=top_cpu,
        network_info=network_info,
        journal_log=journal_log,
        mem_detail=mem_detail,
        disk_detail=disk_detail,
        uptime_load=uptime_load,
        referenced_paths=referenced_paths,
        workdir_files=workdir_files,
    )

    # Add rule hints if matched
    if rule_result.matched:
        evidence_pack += f"\n\n[RULE_ENGINE_HINT]\n"
        evidence_pack += f"Rule engine đã phân loại: [{rule_result.category}]\n"
        evidence_pack += f"Summary: {rule_result.summary_vi}\n"
        evidence_pack += f"Confidence: {rule_result.confidence}\n"
        evidence_pack += f"Severity: {rule_result.severity}\n"
        if rule_result.escalate:
            evidence_pack += f"Escalate: true — {rule_result.escalate_reason}\n"
        if rule_result.extra_commands:
            evidence_pack += f"Extra commands: {rule_result.extra_commands}\n"
        evidence_pack += "[/RULE_ENGINE_HINT]"

    prompt = build_supervisor_llm_prompt(evidence_pack)
    prompt_length = len(prompt)
    logger.info(f"[SUP_PHASE3] 📝 Prompt built: {prompt_length} chars")

    # Save prompt for debugging
    await repo.update_incident(incident_id, llm_prompt_text=prompt)
    await db.commit()

    # Call LLM
    logger.info(f"[SUP_PHASE3] 🤖 Sending to LLM ({settings.gemini_model})...")
    llm_response = None
    llm_raw_text = None
    try:
        llm_client = LLMClient()
        llm_response, llm_raw_text = await llm_client.analyze_supervisor_incident(prompt)

        if llm_raw_text:
            await repo.update_incident(incident_id, llm_raw_response=llm_raw_text)
            await db.commit()

        if llm_response:
            logger.info(f"[SUP_PHASE3] ✅ LLM response parsed OK:")
            logger.info(f"[SUP_PHASE3]    category={llm_response.get('root_cause', {}).get('category')}")
            logger.info(f"[SUP_PHASE3]    severity={llm_response.get('severity')}")
            logger.info(f"[SUP_PHASE3]    summary={llm_response.get('root_cause', {}).get('summary_vi')}")
            logger.info(f"[SUP_PHASE3]    commands={llm_response.get('immediate_action', {}).get('commands')}")
        else:
            logger.error(f"[SUP_PHASE3] ❌ LLM parse failed")
    except Exception as e:
        logger.error(f"[SUP_PHASE3] ❌ LLM call exception: {e}", exc_info=True)

    # ════════════════════════════════════════════════════════════════
    # PHASE 4: Save results to DB
    # ════════════════════════════════════════════════════════════════
    if llm_response:
        root_cause_data = llm_response.get("root_cause", {})
        category = root_cause_data.get("category", "UNKNOWN")
        summary_vi = root_cause_data.get("summary_vi", "")
        evidence_str = root_cause_data.get("evidence", "")
        confidence = root_cause_data.get("confidence", 0.0)
        severity = llm_response.get("severity", "MEDIUM")
        immediate_action = llm_response.get("immediate_action", {})
        root_fix = llm_response.get("root_fix", {})
        escalate = llm_response.get("escalate", False)

        await repo.update_incident(incident_id,
            root_cause=summary_vi,
            immediate_cause=evidence_str,
            canonical_root_cause=f"supervisor_{category.lower()}",
            issue_subtype=category,
            root_cause_summary=summary_vi,
            llm_confidence=confidence,
            rca_level="probable_root_cause",
            verification_status="medium",
            knowledge_source="llm",
            summary=f"[{category}] {summary_vi}",
            ai_analysis_json=llm_response,
            status=IncidentStatus.ACTION_PROPOSED,
        )

        # Build remediation options from LLM response
        options = []

        # Option 1: Immediate action
        if immediate_action.get("commands"):
            options.append({
                "priority": 1,
                "title": immediate_action.get("description_vi", "Hành động khẩn cấp"),
                "description": immediate_action.get("description_vi", ""),
                "risk_level": "medium" if severity in ("HIGH", "CRITICAL") else "low",
                "needs_approval": True,
                "action_type": "execute",
                "commands_json": immediate_action.get("commands", []),
                "expected_effect": f"TTR ước tính: {immediate_action.get('estimated_ttr_s', 60)}s",
                "rollback_commands_json": [],
                "source": "llm",
            })

        # Option 2: Root fix
        if root_fix.get("steps_vi"):
            fix_commands = []
            # Mở rộng danh sách bash commands có thể thực thi được
            bash_prefixes = (
                "sudo ", "supervisorctl ", "systemctl ", "service ",
                "cp ", "mv ", "rm ", "mkdir ", "rmdir ", "ln ", "touch ",
                "chmod ", "chown ", "chgrp ",
                "echo ", "cat ", "tee ", "sed ", "awk ", "grep ",
                "ls ", "find ", "tar ", "unzip ", "gzip ",
                "curl ", "wget ", "git ", "pip ", "pip3 ",
                "python ", "python3 ", "node ", "npm ",
                "apt ", "apt-get ", "yum ", "dnf ", "docker ",
                "kill ", "pkill ", "nohup ",
                "export ", "source ", "bash ", "sh ",
            )
            for step in root_fix.get("steps_vi", []):
                s = step.strip()
                # Bỏ prefix kiểu "1. ", "- ", "* " để check command
                for prefix in ("1. ", "2. ", "3. ", "4. ", "5. ", "- ", "* ", "+ "):
                    if s.startswith(prefix):
                        s = s[len(prefix):].strip()
                        break
                if s.startswith(bash_prefixes):
                    fix_commands.append(s)

            options.append({
                "priority": 2,
                "title": root_fix.get("description_vi", "Sửa lỗi gốc"),
                "description": "\n".join(root_fix.get("steps_vi", [])),
                "risk_level": "high" if root_fix.get("requires_restart") else "medium",
                "needs_approval": True,
                "action_type": "root_fix",
                "commands_json": fix_commands,
                "expected_effect": root_fix.get("description_vi", ""),
                "source": "llm",
            })

        # Option 3: Simple restart as fallback
        options.append({
            "priority": len(options) + 1,
            "title": f"Restart process {process_name}",
            "description": f"supervisorctl restart {process_name}",
            "risk_level": "low",
            "needs_approval": True,
            "action_type": "restart",
            "commands_json": [f"supervisorctl restart {process_name}"],
            "expected_effect": "Process sẽ được restart, nếu lỗi config vẫn sẽ crash",
            "source": "rule",
        })

        # Add extra guardrail commands if disk > 95%
        if rule_result.extra_commands:
            options.append({
                "priority": len(options) + 1,
                "title": "Dọn dẹp disk (>95% usage)",
                "description": "Xóa log cũ và file tạm để giải phóng disk",
                "risk_level": "low",
                "needs_approval": True,
                "action_type": "cleanup",
                "commands_json": rule_result.extra_commands,
                "source": "rule",
            })

        await repo.save_remediation_options(incident_id, options)
        logger.info(f"[SUP_PHASE4] ✅ Saved {len(options)} remediation options")
        logger.info(f"[SUP_PHASE4] ✅ Incident status → action_proposed")

    else:
        # LLM failed — use rule-based result
        logger.warning(f"[SUP_PHASE4] ⚠️ LLM failed, using rule-based fallback")

        if rule_result.matched:
            await repo.update_incident(incident_id,
                root_cause=rule_result.summary_vi,
                canonical_root_cause=f"supervisor_{rule_result.category.lower()}",
                issue_subtype=rule_result.category,
                root_cause_summary=rule_result.summary_vi,
                llm_confidence=rule_result.confidence,
                rca_level="probable_root_cause",
                knowledge_source="rule_fallback",
                summary=f"[{rule_result.category}] {rule_result.summary_vi}",
                status=IncidentStatus.ACTION_PROPOSED,
            )

            fallback_options = [{
                "priority": 1,
                "title": f"Restart process {process_name}",
                "description": f"supervisorctl restart {process_name}",
                "risk_level": "low",
                "needs_approval": True,
                "action_type": "restart",
                "commands_json": [f"supervisorctl restart {process_name}"],
                "source": "rule",
            }]
            await repo.save_remediation_options(incident_id, fallback_options)
        else:
            await repo.update_incident(incident_id,
                status=IncidentStatus.ANALYSIS_FAILED,
                knowledge_source="none",
                summary="LLM không phản hồi và rule engine không match. Cần kiểm tra thủ công.",
            )

    await repo.save_incident_event(incident_id, "supervisor_analysis_completed", {
        "llm_success": llm_response is not None,
        "rule_matched": rule_result.matched,
        "process_name": process_name,
        "category": llm_response.get("root_cause", {}).get("category") if llm_response else (rule_result.category if rule_result.matched else "UNKNOWN"),
    })
    await db.commit()

    final_status = IncidentStatus.ACTION_PROPOSED if (llm_response or rule_result.matched) else IncidentStatus.ANALYSIS_FAILED
    await redis_svc.publish_event("incident_analyzed", {
        "incident_id": incident_id, "status": final_status,
    })

    logger.info(f"[SUP_PIPELINE] ✅ DONE supervisor incident {incident_id} → status={final_status}")
    logger.info(f"{'=' * 60}")
