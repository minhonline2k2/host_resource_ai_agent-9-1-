"""Evidence builder: parse, score, summarize raw evidence into structured blocks for LLM."""

from __future__ import annotations

import re
from typing import Any


def parse_evidence(raw_results: list[dict]) -> list[dict]:
    """Parse raw SSH command results into structured evidence entries."""
    parsed = []
    for r in raw_results:
        entry = {
            "command_id": r.get("command_id", ""),
            "command_text": r.get("command_text", ""),
            "evidence_type": r.get("evidence_type", ""),
            "raw_text": r.get("raw_text", ""),
            "exit_code": r.get("exit_code", 0),
            "duration_ms": r.get("duration_ms", 0),
            "source_host": r.get("source_host", ""),
            "source_type": "ssh",
            "domain_type": "HOST",
            "parsed_json": {},
            "severity_weight": 0.0,
            "is_key_evidence": False,
        }

        text = r.get("raw_text", "")

        # Score and parse based on evidence type
        if r.get("evidence_type") == "process_cpu":
            entry["parsed_json"] = _parse_top_processes(text, sort_by="cpu")
            if _has_high_cpu_process(text):
                entry["severity_weight"] = 0.8
                entry["is_key_evidence"] = True
        elif r.get("evidence_type") == "process_mem":
            entry["parsed_json"] = _parse_top_processes(text, sort_by="mem")
            if _has_high_mem_process(text):
                entry["severity_weight"] = 0.7
                entry["is_key_evidence"] = True
        elif r.get("evidence_type") == "process_anomalies":
            anomalies = _detect_process_anomalies(text, r.get("command_id", ""))
            entry["parsed_json"] = anomalies
            if anomalies.get("count", 0) > 0:
                entry["severity_weight"] = 0.9
                entry["is_key_evidence"] = True
        elif r.get("evidence_type") == "kernel_journal":
            issues = _detect_kernel_issues(text)
            entry["parsed_json"] = issues
            if issues.get("oom") or issues.get("io_error") or issues.get("segfault"):
                entry["severity_weight"] = 1.0
                entry["is_key_evidence"] = True
        elif r.get("evidence_type") == "disk_detail":
            disk_info = _parse_disk_info(text, r.get("command_id", ""))
            entry["parsed_json"] = disk_info
            if disk_info.get("high_usage"):
                entry["severity_weight"] = 0.8
                entry["is_key_evidence"] = True
        elif r.get("evidence_type") == "socket_fd":
            fd_info = _parse_fd_info(text, r.get("command_id", ""))
            entry["parsed_json"] = fd_info
            if fd_info.get("deleted_files") or fd_info.get("high_fd_count"):
                entry["severity_weight"] = 0.6
                entry["is_key_evidence"] = True

        parsed.append(entry)

    return parsed


def build_evidence_pack(
    incident_info: dict,
    prometheus_snapshot: dict,
    prometheus_trends: dict,
    ssh_evidence: list[dict],
    known_history: list[dict] = None,
) -> str:
    """Build the full evidence pack string for LLM prompt."""
    blocks = []

    # [INCIDENT]
    blocks.append("[INCIDENT]")
    blocks.append(f"alert_name: {incident_info.get('alert_name', '')}")
    blocks.append(f"instance: {incident_info.get('instance', '')}")
    blocks.append(f"severity: {incident_info.get('severity', '')}")
    blocks.append(f"resource_type: {incident_info.get('resource_type', '')}")
    blocks.append(f"component_type: {incident_info.get('component_type', '')}")
    blocks.append(f"service_name: {incident_info.get('service_name', '')}")
    blocks.append("[/INCIDENT]")

    # [PROMETHEUS_SNAPSHOT]
    if prometheus_snapshot:
        blocks.append("\n[PROMETHEUS_SNAPSHOT]")
        for k, v in prometheus_snapshot.items():
            blocks.append(f"  {k}: {v}")
        blocks.append("[/PROMETHEUS_SNAPSHOT]")

    # [PROMETHEUS_TRENDS]
    if prometheus_trends:
        blocks.append("\n[PROMETHEUS_TRENDS]")
        for k, v in prometheus_trends.items():
            if isinstance(v, list) and len(v) > 0:
                vals = [str(round(float(p[1]), 2)) for p in v[-10:]]
                blocks.append(f"  {k} (last 10 samples): {', '.join(vals)}")
        blocks.append("[/PROMETHEUS_TRENDS]")

    # Group SSH evidence by type
    ev_by_type: dict[str, list] = {}
    for ev in ssh_evidence:
        et = ev.get("evidence_type", "other")
        ev_by_type.setdefault(et, []).append(ev)

    type_to_block = {
        "baseline": "HOST_BASELINE",
        "process_cpu": "PROCESS_CPU",
        "process_mem": "PROCESS_MEM",
        "process_aggregated": "PROCESS_AGGREGATED",
        "process_anomalies": "PROCESS_ANOMALIES",
        "service_state": "SERVICE_STATE",
        "kernel_journal": "KERNEL_JOURNAL",
        "socket_fd": "SOCKET_FD",
        "cpu_detail": "CPU_DETAIL",
        "memory_detail": "MEMORY_DETAIL",
        "disk_detail": "DISK_EVIDENCE",
        "cron_backup": "CRON_BACKUP_CORRELATION",
        "role_hints": "ROLE_HINTS",
    }

    for ev_type, block_name in type_to_block.items():
        items = ev_by_type.get(ev_type, [])
        if not items:
            continue
        blocks.append(f"\n[{block_name}]")
        for item in items:
            cmd_id = item.get("command_id", "")
            raw = item.get("raw_text", "").strip()
            if raw:
                # Truncate very long outputs
                if len(raw) > 3000:
                    raw = raw[:3000] + "\n... (truncated)"
                blocks.append(f"--- {cmd_id} ---")
                blocks.append(raw)
                # Add anomaly annotations
                parsed = item.get("parsed_json", {})
                if parsed and item.get("is_key_evidence"):
                    blocks.append(f"[ANOMALY: {_summarize_anomaly(parsed)}]")
        blocks.append(f"[/{block_name}]")

    # [KNOWN_HISTORY]
    if known_history:
        blocks.append("\n[KNOWN_HISTORY]")
        for h in known_history[:3]:
            blocks.append(f"  - [{h.get('created_at', '')}] root_cause={h.get('root_cause', 'unknown')}, "
                         f"status={h.get('final_status', '')}")
        blocks.append("[/KNOWN_HISTORY]")

    return "\n".join(blocks)


# === Internal parsers ===

def _parse_top_processes(text: str, sort_by: str = "cpu") -> dict:
    lines = text.strip().split("\n")
    return {"line_count": len(lines), "sort_by": sort_by}


def _has_high_cpu_process(text: str) -> bool:
    for line in text.split("\n"):
        parts = line.split()
        for p in parts:
            try:
                val = float(p)
                if val > 50.0:
                    return True
            except ValueError:
                continue
    return False


def _has_high_mem_process(text: str) -> bool:
    for line in text.split("\n"):
        if "rss" in line.lower():
            continue
        parts = line.split()
        for p in parts:
            try:
                val = int(p)
                if val > 2_000_000:  # > 2GB RSS in KB
                    return True
            except ValueError:
                continue
    return False


def _detect_process_anomalies(text: str, cmd_id: str) -> dict:
    result = {"count": 0, "type": cmd_id}
    if not text.strip():
        return result
    lines = [l for l in text.strip().split("\n") if l.strip()]
    result["count"] = len(lines)
    return result


def _detect_kernel_issues(text: str) -> dict:
    lower = text.lower()
    return {
        "oom": "out of memory" in lower or "oom" in lower,
        "io_error": "i/o error" in lower or "buffer i/o" in lower,
        "segfault": "segfault" in lower,
        "blocked": "blocked for more than" in lower,
        "ext4_error": "ext4" in lower and "error" in lower,
    }


def _parse_disk_info(text: str, cmd_id: str) -> dict:
    result = {"high_usage": False}
    for line in text.split("\n"):
        match = re.search(r'(\d+)%', line)
        if match and int(match.group(1)) > 85:
            result["high_usage"] = True
            break
    return result


def _parse_fd_info(text: str, cmd_id: str) -> dict:
    return {
        "deleted_files": "deleted" in text.lower() if "deleted" in cmd_id or "lsof" in text.lower() else False,
        "high_fd_count": bool(re.search(r'\b\d{4,}\s+fd\b', text)),
    }


def _summarize_anomaly(parsed: dict) -> str:
    parts = []
    if parsed.get("oom"):
        parts.append("OOM detected")
    if parsed.get("io_error"):
        parts.append("I/O errors")
    if parsed.get("segfault"):
        parts.append("segfault")
    if parsed.get("count", 0) > 0:
        parts.append(f"{parsed['count']} anomalous entries")
    if parsed.get("high_usage"):
        parts.append("high disk usage >85%")
    if parsed.get("deleted_files"):
        parts.append("deleted-open files")
    return "; ".join(parts) if parts else "anomaly detected"
