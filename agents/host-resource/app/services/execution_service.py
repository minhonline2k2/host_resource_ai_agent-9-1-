"""Execution service: run approved remediation with safety checks."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.redis_client import RedisService
from app.collectors.ssh_collector import SSHCollector
from app.repositories.incident_repo import IncidentRepository
from app.schemas.schemas import IncidentStatus

logger = get_logger(__name__)

# Commands that are NEVER allowed
BLOCKED_PATTERNS = [
    r"rm\s+-rf\s+/[^t]",  # rm -rf anything except /tmp
    r"mkfs",
    r"dd\s+if=",
    r"shutdown",
    r"reboot",
    r"init\s+[06]",
    r"wipefs",
    r"fdisk",
    r"parted",
]

# High-risk patterns that always need approval
HIGH_RISK_PATTERNS = [
    r"systemctl\s+(restart|stop)\s+.*(?:mysql|mariadb|postgres|redis|kafka|zookeeper)",
    r"kill\s+-9",
    r"truncate",
    r"docker\s+system\s+prune\s+-a",
]


def is_command_safe(command: str) -> tuple[bool, str]:
    """Check if a command is safe to execute. Returns (safe, reason)."""
    for pattern in BLOCKED_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE):
            return False, f"Blocked pattern: {pattern}"
    return True, ""


def is_high_risk(command: str) -> bool:
    """Check if a command is high-risk."""
    for pattern in HIGH_RISK_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE):
            return True
    return False


class ExecutionService:
    def __init__(self, db: AsyncSession, redis_svc: RedisService):
        self.repo = IncidentRepository(db)
        self.redis = redis_svc
        self.db = db

    async def execute_approved_action(
        self, incident_id: str, option_id: str, host: str
    ) -> dict:
        """Execute an approved remediation option."""

        # 1. Acquire execution lock
        if not await self.redis.acquire_exec_lock(incident_id):
            logger.warning(f"Execution lock already held for {incident_id}")
            return {"status": "locked", "message": "Incident đang được xử lý"}

        try:
            # 2. Get option details
            option = await self.repo.get_remediation_option(option_id)
            if not option:
                return {"status": "error", "message": "Không tìm thấy phương án"}

            commands = option.commands_json or []
            pre_checks = option.pre_checks_json or []
            post_checks = option.post_checks_json or []
            rollback_commands = option.rollback_commands_json or []

            # 3. Update status
            await self.repo.update_incident(incident_id, status=IncidentStatus.EXECUTING)
            await self.repo.save_incident_event(incident_id, "execution_started", {
                "option_id": option_id, "title": option.title,
            })

            collector = SSHCollector(host)
            results = []
            all_success = True

            # 4. Run pre-checks
            for i, check in enumerate(pre_checks):
                safe, reason = is_command_safe(check)
                if not safe:
                    logger.warning(f"Pre-check blocked: {reason}")
                    continue

                result = collector.run_command(check)
                await self.repo.save_execution_log(
                    incident_id=incident_id, action_proposal_id=option_id,
                    step_no=i + 1, step_name=f"pre_check_{i+1}",
                    status="success" if result["exit_code"] == 0 else "failed",
                    command=check, stdout=result["stdout"],
                    stderr=result["stderr"], exit_code=result["exit_code"],
                    started_at=datetime.now(timezone.utc),
                    finished_at=datetime.now(timezone.utc),
                )

                if result["exit_code"] != 0:
                    logger.error(f"Pre-check failed: {check}")
                    all_success = False
                    break

            if not all_success:
                await self.repo.update_incident(incident_id, status=IncidentStatus.EXECUTION_FAILED)
                await self.repo.update_option_status(option_id, "pre_check_failed")
                await self.db.commit()
                await self.redis.release_exec_lock(incident_id)
                return {"status": "pre_check_failed", "message": "Pre-check thất bại"}

            # 5. Execute main commands
            for i, cmd in enumerate(commands):
                safe, reason = is_command_safe(cmd)
                if not safe:
                    logger.error(f"Command blocked by safety: {cmd} - {reason}")
                    await self.repo.save_execution_log(
                        incident_id=incident_id, action_proposal_id=option_id,
                        step_no=len(pre_checks) + i + 1, step_name=f"execute_{i+1}",
                        status="blocked", command=cmd, stdout="",
                        stderr=f"BLOCKED: {reason}", exit_code=-1,
                        started_at=datetime.now(timezone.utc),
                        finished_at=datetime.now(timezone.utc),
                    )
                    all_success = False
                    break

                result = collector.run_command(cmd)
                await self.repo.save_execution_log(
                    incident_id=incident_id, action_proposal_id=option_id,
                    step_no=len(pre_checks) + i + 1, step_name=f"execute_{i+1}",
                    status="success" if result["exit_code"] == 0 else "failed",
                    command=cmd, stdout=result["stdout"][:10000],
                    stderr=result["stderr"][:5000], exit_code=result["exit_code"],
                    started_at=datetime.now(timezone.utc),
                    finished_at=datetime.now(timezone.utc),
                )
                results.append(result)

                if result["exit_code"] != 0:
                    logger.error(f"Execution step failed: {cmd}")
                    all_success = False
                    # Try rollback
                    if rollback_commands:
                        logger.info("Attempting rollback...")
                        for rb_cmd in rollback_commands:
                            rb_safe, _ = is_command_safe(rb_cmd)
                            if rb_safe:
                                collector.run_command(rb_cmd)
                    break

            # 6. Update status
            if all_success:
                await self.repo.update_incident(incident_id, status=IncidentStatus.EXECUTED)
                await self.repo.update_option_status(option_id, "executed")
            else:
                await self.repo.update_incident(incident_id, status=IncidentStatus.EXECUTION_FAILED)
                await self.repo.update_option_status(option_id, "execution_failed")

            await self.repo.save_incident_event(incident_id, "execution_completed", {
                "option_id": option_id, "success": all_success,
                "steps": len(results),
            })

            await self.db.commit()

            await self.redis.publish_event("execution_completed", {
                "incident_id": incident_id, "success": all_success,
            })

            return {
                "status": "success" if all_success else "failed",
                "steps_executed": len(results),
            }

        finally:
            await self.redis.release_exec_lock(incident_id)
