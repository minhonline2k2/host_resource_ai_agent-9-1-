"""Supervisor incident worker: polls queues and processes supervisor alerts."""

from __future__ import annotations

import asyncio
import json as _json
from datetime import datetime, timezone

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.database import get_session_factory
from app.core.redis_client import get_redis, RedisService
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

    print("[WORKER] ✅ Polling: supervisor incidents + executions")
    logger.info("=" * 60)
    logger.info("SUPERVISOR WORKER STARTED — polling incident + execution queues")
    logger.info("=" * 60)

    # Run both loops concurrently
    await asyncio.gather(
        _incident_loop(redis_svc),
        _execution_loop(redis_svc),
    )


async def _execution_loop(redis_svc: RedisService):
    """Poll execution queue and run approved commands via SSH."""
    QUEUE = "agent:queue:supervisor:execute"
    logger.info(f"[EXEC] Execution loop started — polling {QUEUE}")

    while True:
        try:
            raw = await redis_svc.redis.brpop(QUEUE, timeout=2)
            if not raw:
                continue

            job = _json.loads(raw[1] if isinstance(raw, (list, tuple)) else raw)
            incident_id = job.get("incident_id", "")
            option_id = job.get("option_id", "")
            instance = job.get("instance", "")
            commands = job.get("commands", [])
            host = instance.split(":")[0] if ":" in instance else instance

            logger.info(
                f"[EXEC] 🔧 Executing {len(commands)} commands for "
                f"{incident_id} on {host}"
            )

            if not commands:
                logger.warning("[EXEC] No commands to execute")
                continue

            # Acquire execution lock
            lock_key = f"agent:exec_lock:{incident_id}"
            locked = await redis_svc.redis.set(
                lock_key, "1", nx=True, ex=settings.redis_exec_lock_ttl
            )
            if not locked:
                logger.warning(f"[EXEC] Lock exists for {incident_id}, skipping")
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
                results.append(
                    {
                        "step_no": i + 1,
                        "command": cmd,
                        "stdout": r.get("stdout", "")[:5000],
                        "stderr": r.get("stderr", "")[:2000],
                        "exit_code": r.get("exit_code", -1),
                        "success": success,
                    }
                )
                logger.info(
                    f"[EXEC]   → exit={r.get('exit_code')} "
                    f"{'✅' if success else '❌'}"
                )

            # Save results to DB
            async with get_session_factory()() as db:
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
                await repo.save_incident_event(
                    incident_id,
                    "execution_completed",
                    {
                        "success": overall_success,
                        "steps": len(commands),
                        "option_id": option_id,
                    },
                )
                await db.commit()

            # Push result to orchestrator
            try:
                from app.core.orchestrator import push_result_to_orchestrator

                await push_result_to_orchestrator(
                    {
                        "incident_id": incident_id,
                        "agent_id": settings.agent_id,
                        "status": new_status,
                        "execution_results": results,
                    }
                )
            except Exception:
                pass

            logger.info(
                f"[EXEC] {'✅' if overall_success else '❌'} "
                f"Execution done: {new_status}"
            )

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"[EXEC] Error: {e}", exc_info=True)
            await asyncio.sleep(3)


async def _incident_loop(redis_svc: RedisService):
    """Poll incident queue for new supervisor analysis jobs."""
    while True:
        try:
            raw = await redis_svc.pop_incident(timeout=settings.worker_poll_interval)
            if not raw:
                continue

            incident_id = raw if isinstance(raw, str) else raw.decode()
            logger.info(f"[WORKER] 📥 Got incident from queue: {incident_id}")

            # Process in a new DB session
            async with get_session_factory()() as db:
                try:
                    from app.workers.supervisor_worker import (
                        process_supervisor_incident,
                    )

                    await process_supervisor_incident(db, redis_svc, incident_id)
                except Exception as e:
                    logger.error(
                        f"[WORKER] ❌ Error processing {incident_id}: {e}",
                        exc_info=True,
                    )
                    try:
                        repo = IncidentRepository(db)
                        await repo.update_incident(
                            incident_id,
                            status=IncidentStatus.ANALYSIS_FAILED,
                            summary=f"Pipeline error: {e}",
                        )
                        await repo.save_incident_event(
                            incident_id,
                            "pipeline_error",
                            {"error": str(e)},
                        )
                        await db.commit()
                    except Exception:
                        pass

            # Push result to orchestrator
            try:
                from app.core.orchestrator import push_result_to_orchestrator

                async with get_session_factory()() as db2:
                    repo = IncidentRepository(db2)
                    inc = await repo.get_incident(incident_id)
                    if inc:
                        await push_result_to_orchestrator(
                            {
                                "incident_id": incident_id,
                                "agent_id": settings.agent_id,
                                "status": inc.status,
                                "root_cause": inc.root_cause_summary or "",
                                "confidence": inc.llm_confidence or 0,
                            }
                        )
            except Exception:
                pass

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"[WORKER] Queue error: {e}", exc_info=True)
            await asyncio.sleep(3)
