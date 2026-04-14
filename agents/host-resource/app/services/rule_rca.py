"""Rule-based RCA engine: detect common patterns before calling LLM."""

from __future__ import annotations

import re
from typing import Optional
from app.core.logging import get_logger

logger = get_logger(__name__)


class RuleRCAResult:
    def __init__(
        self,
        matched: bool = False,
        root_cause: str = "",
        canonical_root_cause: str = "",
        issue_subtype: str = "",
        confidence: float = 0.0,
        evidence_refs: list[str] = None,
        explanation: str = "",
    ):
        self.matched = matched
        self.root_cause = root_cause
        self.canonical_root_cause = canonical_root_cause
        self.issue_subtype = issue_subtype
        self.confidence = confidence
        self.evidence_refs = evidence_refs or []
        self.explanation = explanation


def run_rule_rca(
    resource_type: str,
    alert_name: str,
    evidence: list[dict],
    prometheus_snapshot: dict,
) -> RuleRCAResult:
    """Run rule-based RCA on collected evidence. Returns RuleRCAResult."""

    ev_by_type = {}
    for e in evidence:
        et = e.get("evidence_type", "other")
        ev_by_type.setdefault(et, []).append(e)

    rt = resource_type.upper()

    if rt == "CPU":
        return _rca_cpu(alert_name, ev_by_type, prometheus_snapshot)
    elif rt == "RAM":
        return _rca_ram(alert_name, ev_by_type, prometheus_snapshot)
    elif rt == "DISK":
        return _rca_disk(alert_name, ev_by_type, prometheus_snapshot)

    return RuleRCAResult()


# ========== CPU Rules ==========

def _rca_cpu(alert_name: str, ev: dict, prom: dict) -> RuleRCAResult:
    # Check iowait first
    iowait = prom.get("cpu_iowait", 0)
    if iowait and iowait > 20:
        return RuleRCAResult(
            matched=True,
            root_cause="CPU high do I/O wait cao",
            canonical_root_cause="cpu_high_due_to_iowait",
            issue_subtype="iowait",
            confidence=0.7,
            evidence_refs=["vmstat", "iostat"],
            explanation=f"iowait={iowait:.1f}% > 20%, gốc vấn đề nằm ở disk I/O không phải CPU tính toán",
        )

    # Check steal time
    steal = prom.get("cpu_steal", 0)
    if steal and steal > 10:
        return RuleRCAResult(
            matched=True,
            root_cause="CPU high do hypervisor steal time",
            canonical_root_cause="cpu_high_due_to_steal_time",
            issue_subtype="steal_time",
            confidence=0.75,
            evidence_refs=["top_snap"],
            explanation=f"steal={steal:.1f}% > 10%, VM bị hypervisor contention",
        )

    # Check single runaway process from top_cpu evidence
    cpu_ev = ev.get("process_cpu", [])
    for e in cpu_ev:
        text = e.get("raw_text", "")
        top_proc = _find_top_cpu_process(text)
        if top_proc and top_proc["cpu"] > 60:
            return RuleRCAResult(
                matched=True,
                root_cause=f"CPU high do process {top_proc['comm']} (PID {top_proc['pid']}) chiếm {top_proc['cpu']}%",
                canonical_root_cause=f"cpu_hog_{top_proc['comm']}",
                issue_subtype="runaway_process",
                confidence=0.65,
                evidence_refs=["top_cpu", "ps_cpu"],
                explanation=f"Process {top_proc['comm']} chiếm >{top_proc['cpu']}% CPU",
            )

    # Check GC thrashing from kernel/journal
    kernel_ev = ev.get("kernel_journal", [])
    for e in kernel_ev:
        text = e.get("raw_text", "")
        if re.search(r'gc\s+pause|gc\s+evacuation|full\s+gc', text, re.IGNORECASE):
            return RuleRCAResult(
                matched=True,
                root_cause="CPU high do Java GC thrashing",
                canonical_root_cause="java_gc_thrashing_due_to_heap_pressure",
                issue_subtype="gc_thrashing",
                confidence=0.7,
                evidence_refs=["dmesg", "dmesg_grep", "top_cpu"],
                explanation="GC pause logs phát hiện trong kernel/journal",
            )

    # Check zombie/D-state
    anomaly_ev = ev.get("process_anomalies", [])
    for e in anomaly_ev:
        parsed = e.get("parsed_json", {})
        if e.get("command_id") == "d_state" and parsed.get("count", 0) > 5:
            return RuleRCAResult(
                matched=True,
                root_cause="Load cao do nhiều process trong D-state (blocked I/O)",
                canonical_root_cause="load_high_due_to_d_state_processes",
                issue_subtype="d_state_processes",
                confidence=0.65,
                evidence_refs=["d_state", "loadavg"],
                explanation=f"{parsed['count']} process trong D-state",
            )

    # Check backup/cron correlation
    cron_ev = ev.get("cron_backup", [])
    for e in cron_ev:
        text = e.get("raw_text", "")
        if re.search(r'rsync|tar|gzip|mysqldump|pg_dump|backup', text, re.IGNORECASE):
            return RuleRCAResult(
                matched=True,
                root_cause="CPU tăng do backup/cron job đang chạy",
                canonical_root_cause="cpu_spike_due_to_backup_job",
                issue_subtype="backup_correlation",
                confidence=0.5,
                evidence_refs=["crontab", "timers", "recent_modified"],
                explanation="Phát hiện backup/rsync/tar trong cron hoặc recent processes",
            )

    return RuleRCAResult()


# ========== RAM Rules ==========

def _rca_ram(alert_name: str, ev: dict, prom: dict) -> RuleRCAResult:
    # Check OOM
    kernel_ev = ev.get("kernel_journal", [])
    for e in kernel_ev:
        parsed = e.get("parsed_json", {})
        if parsed.get("oom"):
            return RuleRCAResult(
                matched=True,
                root_cause="OOM killer đã can thiệp",
                canonical_root_cause="oom_killer_invoked",
                issue_subtype="oom_kill",
                confidence=0.9,
                evidence_refs=["dmesg_grep", "oom_logs"],
                explanation="Kernel OOM killer log phát hiện trong dmesg/journal",
            )

    # Check swap thrashing
    swap_used = prom.get("swap_used", 0)
    if swap_used and swap_used > 500_000_000:  # > 500MB swap
        return RuleRCAResult(
            matched=True,
            root_cause="Memory pressure gây swap thrashing",
            canonical_root_cause="memory_pressure_swap_thrashing",
            issue_subtype="swap_thrashing",
            confidence=0.65,
            evidence_refs=["free", "vmstat", "swapon"],
            explanation=f"Swap used={swap_used / 1e9:.1f}GB, hệ thống đang swap thrashing",
        )

    # Check top RSS process
    mem_ev = ev.get("process_mem", [])
    for e in mem_ev:
        top_proc = _find_top_mem_process(e.get("raw_text", ""))
        if top_proc and top_proc["rss_gb"] > 2:
            return RuleRCAResult(
                matched=True,
                root_cause=f"Memory high do process {top_proc['comm']} chiếm {top_proc['rss_gb']:.1f}GB RSS",
                canonical_root_cause=f"memory_hog_{top_proc['comm']}",
                issue_subtype="high_rss_process",
                confidence=0.6,
                evidence_refs=["top_mem"],
                explanation=f"{top_proc['comm']} RSS={top_proc['rss_gb']:.1f}GB",
            )

    return RuleRCAResult()


# ========== DISK Rules ==========

def _rca_disk(alert_name: str, ev: dict, prom: dict) -> RuleRCAResult:
    # Check deleted-open files
    fd_ev = ev.get("socket_fd", []) + ev.get("disk_detail", [])
    for e in fd_ev:
        cmd_id = e.get("command_id", "")
        if "deleted" in cmd_id or "lsof" in cmd_id:
            text = e.get("raw_text", "")
            if text.strip() and len(text.strip().split("\n")) > 1:
                return RuleRCAResult(
                    matched=True,
                    root_cause="Disk đầy do file đã xóa nhưng process vẫn giữ handle (deleted-open-file)",
                    canonical_root_cause="disk_full_due_to_deleted_open_files",
                    issue_subtype="deleted_open_file",
                    confidence=0.75,
                    evidence_refs=["deleted_open", "lsof"],
                    explanation="lsof +L1 cho thấy file đã xóa vẫn chiếm disk",
                )

    # Check log growth
    disk_ev = ev.get("disk_detail", [])
    for e in disk_ev:
        if e.get("command_id") == "du_top":
            text = e.get("raw_text", "")
            if re.search(r'(\d+)G\s+/var/log', text):
                match = re.search(r'(\d+)G\s+/var/log', text)
                if match and int(match.group(1)) > 5:
                    return RuleRCAResult(
                        matched=True,
                        root_cause="Disk đầy do /var/log tăng nhanh",
                        canonical_root_cause="disk_full_due_to_log_growth",
                        issue_subtype="log_growth",
                        confidence=0.7,
                        evidence_refs=["du_top", "recent_files"],
                        explanation=f"/var/log={match.group(1)}GB",
                    )

    # Check inode
    if "Inode" in alert_name:
        return RuleRCAResult(
            matched=True,
            root_cause="Inode đầy do quá nhiều file nhỏ",
            canonical_root_cause="inode_full_too_many_small_files",
            issue_subtype="inode_pressure",
            confidence=0.6,
            evidence_refs=["df_ih", "inode_detail"],
            explanation="Alert inode high, cần tìm thư mục chứa nhiều file nhỏ",
        )

    # Check backup growth
    cron_ev = ev.get("cron_backup", [])
    for e in cron_ev:
        if e.get("command_id") == "backup_files":
            text = e.get("raw_text", "")
            if text.strip():
                return RuleRCAResult(
                    matched=True,
                    root_cause="Disk tăng do backup file tích lũy",
                    canonical_root_cause="disk_growth_due_to_backup_retention",
                    issue_subtype="backup_growth",
                    confidence=0.55,
                    evidence_refs=["backup_files", "du_top"],
                    explanation="Backup files phát hiện trong /backup hoặc /var/backup",
                )

    # Check docker
    for e in disk_ev:
        if e.get("command_id") == "docker_df":
            text = e.get("raw_text", "")
            if text.strip() and "GB" in text:
                return RuleRCAResult(
                    matched=True,
                    root_cause="Disk đầy do Docker images/layers/volumes",
                    canonical_root_cause="disk_full_due_to_docker",
                    issue_subtype="docker_disk",
                    confidence=0.6,
                    evidence_refs=["docker_df"],
                    explanation="docker system df cho thấy disk usage đáng kể",
                )

    return RuleRCAResult()


# ========== Helpers ==========

def _find_top_cpu_process(text: str) -> Optional[dict]:
    for line in text.split("\n"):
        parts = line.split()
        if len(parts) < 11:
            continue
        try:
            pid = int(parts[0])
            cpu = float(parts[5])
            comm = parts[10]
            if cpu > 30:
                return {"pid": pid, "cpu": cpu, "comm": comm}
        except (ValueError, IndexError):
            continue
    return None


def _find_top_mem_process(text: str) -> Optional[dict]:
    for line in text.split("\n"):
        parts = line.split()
        if len(parts) < 11:
            continue
        try:
            pid = int(parts[0])
            rss = int(parts[7])  # RSS in KB
            comm = parts[10]
            rss_gb = rss / 1_048_576
            if rss_gb > 1:
                return {"pid": pid, "rss_gb": rss_gb, "comm": comm}
        except (ValueError, IndexError):
            continue
    return None
