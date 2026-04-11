"""Rule-based RCA engine for supervisor incidents — implements guardrails."""

from __future__ import annotations

from typing import Optional
from app.core.logging import get_logger

logger = get_logger(__name__)


class SupervisorRuleResult:
    def __init__(
        self,
        matched: bool = False,
        category: str = "UNKNOWN",
        summary_vi: str = "",
        confidence: float = 0.0,
        severity: str = "MEDIUM",
        escalate: bool = False,
        escalate_reason: str = "",
        extra_commands: list[str] = None,
    ):
        self.matched = matched
        self.category = category
        self.summary_vi = summary_vi
        self.confidence = confidence
        self.severity = severity
        self.escalate = escalate
        self.escalate_reason = escalate_reason
        self.extra_commands = extra_commands or []


def run_supervisor_rule_rca(
    process_name: str,
    exit_code: int,
    signal: str,
    uptime_sec: int,
    restart_count: int,
    oom_flag: bool,
    signal_flag: bool,
    disk_pct: str,
    stderr_content: str,
    stdout_content: str,
) -> SupervisorRuleResult:
    """Apply guardrails rules to supervisor incident data.
    
    Rules from spec:
    - exit_code = 137              → category = [OOM]
    - exit_code = 1 + uptime < 3s  → [CONFIG_ERR] or [DEP_VERSION]
    - exit_code = 0                → severity = LOW
    - signal = SIGKILL             → [OOM] or [RESOURCE]
    - restart_count > 3            → escalate = true
    - severity = CRITICAL          → escalate = true
    - oom_flag = true              → [OOM], escalate = true
    - stderr empty + stdout clean  → [UNKNOWN]
    - confidence < 0.5             → ask_for_more_info required
    - disk_pct > 95                → add disk cleanup commands
    """
    result = SupervisorRuleResult()

    # Parse disk percentage
    disk_pct_num = 0
    try:
        disk_pct_num = int(disk_pct.replace("%", "").strip())
    except (ValueError, AttributeError):
        pass

    # === GUARDRAIL: OOM flag in syslog ===
    if oom_flag:
        result.matched = True
        result.category = "OOM"
        result.summary_vi = f"Process {process_name} bị OOM killer hạ — hết RAM"
        result.confidence = 0.9
        result.severity = "CRITICAL"
        result.escalate = True
        result.escalate_reason = "OOM killer detected in syslog"
        logger.info(f"[SUP_RULE] Matched: OOM flag in syslog")
        return result

    # === GUARDRAIL: exit_code = 137 → OOM ===
    if exit_code == 137:
        result.matched = True
        result.category = "OOM"
        result.summary_vi = f"Process {process_name} bị kill với exit 137 (SIGKILL) — nghi OOM"
        result.confidence = 0.85
        result.severity = "CRITICAL"
        result.escalate = True
        result.escalate_reason = "Exit code 137 = SIGKILL, likely OOM"
        logger.info(f"[SUP_RULE] Matched: exit_code=137 → OOM")
        return result

    # === GUARDRAIL: signal = SIGKILL ===
    if signal and "SIGKILL" in signal.upper():
        result.matched = True
        result.category = "OOM"
        result.summary_vi = f"Process {process_name} bị SIGKILL — nghi OOM hoặc hết tài nguyên"
        result.confidence = 0.8
        result.severity = "HIGH"
        result.escalate = True
        result.escalate_reason = "SIGKILL detected"
        logger.info(f"[SUP_RULE] Matched: SIGKILL")
        return result

    # === GUARDRAIL: exit_code = 1 + uptime < 3s → CONFIG_ERR ===
    if exit_code == 1 and uptime_sec < 3:
        # Check stderr for hints
        stderr_lower = stderr_content.lower()
        if any(kw in stderr_lower for kw in ("import", "module", "version", "pip", "package")):
            result.category = "DEP_VERSION"
            result.summary_vi = f"Process {process_name} crash ngay khi start — xung đột thư viện"
        else:
            result.category = "CONFIG_ERR"
            result.summary_vi = f"Process {process_name} crash ngay khi start — lỗi config/env"
        
        result.matched = True
        result.confidence = 0.7
        result.severity = "HIGH"
        logger.info(f"[SUP_RULE] Matched: exit=1 + uptime<3s → {result.category}")
        return result

    # === GUARDRAIL: exit_code = 0 → LOW severity ===
    if exit_code == 0:
        result.matched = True
        result.category = "UNKNOWN"
        result.summary_vi = f"Process {process_name} thoát bình thường (exit 0)"
        result.confidence = 0.5
        result.severity = "LOW"
        logger.info(f"[SUP_RULE] Matched: exit=0 → LOW severity")
        return result

    # === GUARDRAIL: stderr rỗng + stdout sạch → UNKNOWN ===
    if not stderr_content.strip() and not stdout_content.strip():
        result.matched = True
        result.category = "UNKNOWN"
        result.summary_vi = f"Process {process_name} crash — log rỗng, không đủ thông tin"
        result.confidence = 0.3
        result.severity = "MEDIUM"
        logger.info(f"[SUP_RULE] Matched: empty logs → UNKNOWN")
        return result

    # === Pattern matching on stderr ===
    stderr_lower = stderr_content.lower()

    # Permission errors
    if any(kw in stderr_lower for kw in ("permission denied", "errno 13", "access denied")):
        result.matched = True
        result.category = "PERM_ERR"
        result.summary_vi = f"Process {process_name} lỗi quyền truy cập file/thư mục"
        result.confidence = 0.75
        result.severity = "HIGH"
        logger.info(f"[SUP_RULE] Matched: permission error in stderr")
        return result

    # Connection errors (DEP_FAIL)
    if any(kw in stderr_lower for kw in ("connection refused", "connection reset", "timeout", "econnrefused", "cannot connect")):
        result.matched = True
        result.category = "DEP_FAIL"
        result.summary_vi = f"Process {process_name} không kết nối được dependency (DB/Redis/API)"
        result.confidence = 0.7
        result.severity = "HIGH"
        logger.info(f"[SUP_RULE] Matched: connection error → DEP_FAIL")
        return result

    # === GUARDRAIL: disk_pct > 95 ===
    if disk_pct_num > 95:
        result.extra_commands = [
            "find /var/log -name '*.log.*' -mtime +7 -delete",
            "find /tmp -type f -mtime +3 -delete",
        ]
        logger.info(f"[SUP_RULE] disk={disk_pct_num}% > 95%, adding cleanup commands")

    # === GUARDRAIL: restart_count > 3 → escalate ===
    if restart_count > 3:
        result.escalate = True
        result.escalate_reason = f"Restart count = {restart_count} > 3"
        logger.info(f"[SUP_RULE] restart_count={restart_count} > 3 → escalate")

    return result
