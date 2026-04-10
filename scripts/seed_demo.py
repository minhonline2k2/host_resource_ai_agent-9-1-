"""Seed demo data for UI testing."""

import asyncio
import sys
import os
import uuid
from datetime import datetime, timezone, timedelta

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.database import get_engine, get_session_factory, Base
from app.models.models import (
    Incident, IncidentEvidence, RemediationOption,
    AuditEvent, RemediationKnowledge, IncidentPattern, IncidentEvent,
)


async def seed():
    engine = get_engine()

    # Create tables first
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = get_session_factory()
    async with factory() as db:
        now = datetime.now(timezone.utc)

        # Incident 1 - action_proposed (CPU)
        inc1 = str(uuid.uuid4())
        db.add(Incident(
            id=inc1, incident_number="INC-20260408-103022",
            alert_name="HostCPUHigh", title="HostCPUHigh on 10.0.1.50:9100",
            status="action_proposed", severity="warning", instance="10.0.1.50:9100",
            resource_type="CPU", domain_type="HOST", component_type="app",
            root_cause="CPU 92.3% do process java (PID 12345) chiếm 78% CPU — GC thrashing do heap gần đầy",
            canonical_root_cause="java_gc_thrashing_due_to_heap_pressure",
            issue_subtype="gc_thrashing",
            root_cause_signature_v2="HOST|HostCPUHigh|10.0.1.50|java|gc_thrashing|java_gc_thrashing_due_to_heap_pressure",
            root_cause_summary="CPU 92.3% do Java GC thrashing. Process java PID 12345 chiếm 78% CPU. GC pause time tăng dần: 1.2s → 1.8s → 2.1s.",
            llm_confidence=0.85, rca_level="probable_root_cause", knowledge_source="llm",
            summary="CPU 92.3% do Java GC thrashing — heap gần đầy, cần restart hoặc tăng heap.",
            ai_analysis_json={
                "summary": "CPU 92.3% do process java (PID 12345) chiếm 78% CPU. GC pause tăng dần: 1.2s → 1.8s → 2.1s. Root cause là Java heap pressure gây GC thrashing liên tục.",
                "root_causes": [
                    {"name": "Java GC thrashing do heap pressure", "confidence": 0.85,
                     "why": "Process java PID 12345 chiếm 78% CPU. Load average 4.21 cao. GC pause logs cho thấy GC chạy liên tục với pause time tăng dần. Pattern khớp với incidents trước.",
                     "evidence_refs": ["top_snap", "dmesg_grep", "vmstat"]},
                    {"name": "Memory leak trong Java application", "confidence": 0.10,
                     "why": "RES 2.1GB gần bằng Xmx2g limit, gợi ý heap gần đầy. Cần heap dump để xác nhận.",
                     "evidence_refs": ["top_snap"]},
                ],
                "confidence": 0.85,
            },
            created_at=now - timedelta(minutes=30),
        ))

        # Evidence for inc1
        for cmd_id, cmd, raw, etype, key in [
            ("top_snap", "top -bn1 -o %CPU | head -25",
             "top - 10:30:45 up 45 days, 3:22, 2 users, load average: 4.21, 3.85, 3.12\nTasks: 186 total, 3 running, 183 sleeping\n%Cpu(s): 92.3 us, 3.1 sy, 0.0 ni, 4.2 id, 0.0 wa, 0.0 hi, 0.4 si, 0.0 st\nMiB Mem:  7856.2 total,  512.3 free, 5234.1 used, 2109.8 buff/cache\n\n  PID USER      PR  NI    VIRT    RES    SHR S  %CPU  %MEM     TIME+ COMMAND\n12345 app       20   0 4589312 2.1g  12456 S  78.2  27.4  1234:56 java\n 5678 mysql     20   0 1256000 456m  34567 S   5.1   5.8   567:23 mysqld\n  891 root      20   0  234000  45m   8901 S   2.3   0.6    12:34 prometheus\n 2345 root      20   0  123000  23m   5678 S   1.1   0.3     5:45 filebeat",
             "process_cpu", True),
            ("loadavg", "cat /proc/loadavg", "4.21 3.85 3.12 3/186 23456", "baseline", False),
            ("vmstat", "vmstat 1 3",
             "procs -----------memory---------- ---swap-- -----io---- -system-- ------cpu-----\n r  b   swpd   free   buff  cache   si   so    bi    bo   in   cs us sy id wa st\n 3  0      0 524288 215040 1941504    0    0     5    12  456  890 89  4  6  1  0\n 4  0      0 518144 215040 1941504    0    0     0     8  478  912 91  3  5  1  0\n 3  0      0 520192 215040 1941504    0    0     0    16  445  878 90  4  5  1  0",
             "baseline", False),
            ("free", "free -m",
             "              total        used        free      shared  buff/cache   available\nMem:           7856        5234         512         128        2109        2394\nSwap:          2048           0        2048",
             "baseline", False),
            ("dmesg_grep", "dmesg -T | egrep -i 'gc|oom'",
             "[Tue Apr  8 10:25:12 2026] java[12345]: GC pause (G1 Evacuation Pause) 1.2s\n[Tue Apr  8 10:27:45 2026] java[12345]: GC pause (G1 Evacuation Pause) 1.5s\n[Tue Apr  8 10:28:33 2026] java[12345]: GC pause (G1 Evacuation Pause) 1.8s\n[Tue Apr  8 10:30:01 2026] java[12345]: GC pause (G1 Evacuation Pause) 2.1s",
             "kernel_journal", True),
            ("ss_summary", "ss -s",
             "Total: 234\nTCP:   156 (estab 89, closed 12, orphaned 3, timewait 8)\nUDP:   23\nRAW:   2",
             "socket_fd", False),
        ]:
            db.add(IncidentEvidence(
                incident_id=inc1, domain_type="HOST", source_type="ssh",
                evidence_type=etype, command_id=cmd_id, command_text=cmd,
                raw_text=raw, exit_code=0, duration_ms=200,
                source_host="10.0.1.50", collector_name="ssh_collector",
                is_key_evidence=key, observed_at=now - timedelta(minutes=29),
            ))

        # Remediation options for inc1
        opts = [
            ("Restart app-backend service",
             "Restart Java application để giải phóng CPU và reset heap. Downtime ~10s.",
             "medium", ["systemctl restart app-backend"],
             ["systemctl start app-backend"],
             "CPU giảm về dưới 20% trong 2 phút",
             ["Service sẽ bị downtime ~10 giây"]),
            ("Tăng JVM heap limit lên 3GB",
             "Tăng max heap từ 2G lên 3G để giảm tần suất GC. Cần restart service.",
             "medium",
             ["sed -i 's/-Xmx2g/-Xmx3g/' /etc/app-backend/jvm.opts", "systemctl restart app-backend"],
             ["sed -i 's/-Xmx3g/-Xmx2g/' /etc/app-backend/jvm.opts", "systemctl restart app-backend"],
             "Giảm tần suất GC thrashing, CPU ổn định hơn",
             ["Tăng RAM usage thêm ~1GB"]),
            ("Thread dump + heap analysis",
             "Thu thập thread dump và heap dump để phân tích sâu root cause. Không ảnh hưởng service.",
             "low",
             ["jstack 12345 > /tmp/thread_dump_$(date +%s).txt", "jcmd 12345 GC.heap_info > /tmp/heap_info_$(date +%s).txt"],
             [],
             "Có dữ liệu phân tích chính xác hơn, không giải quyết ngay",
             []),
        ]
        for i, (title, desc, risk, cmds, rollback, effect, warns) in enumerate(opts, 1):
            db.add(RemediationOption(
                id=str(uuid.uuid4()), incident_id=inc1, option_no=i, priority=i,
                title=title, description=desc, risk_level=risk, needs_approval=True,
                action_type="restart" if i==1 else "config" if i==2 else "diagnose",
                commands_json=cmds, rollback_commands_json=rollback,
                expected_effect=effect, warnings_json=warns,
                source="llm", status="pending",
            ))

        # Events for inc1
        for et, ed, mins in [
            ("incident_created", {"alert_name": "HostCPUHigh", "instance": "10.0.1.50:9100"}, 30),
            ("evidence_collection_started", {"resource_type": "CPU", "host_role": "app"}, 29),
            ("evidence_collected", {"ssh_commands": 45, "prometheus_metrics": 8, "key_evidence_count": 6}, 28),
            ("analysis_completed", {"knowledge_source": "llm", "confidence": 0.85, "options_count": 3}, 27),
        ]:
            db.add(IncidentEvent(incident_id=inc1, event_type=et, event_data_json=ed,
                                 created_at=now - timedelta(minutes=mins)))

        # Incident 2 - resolved (DISK)
        inc2 = str(uuid.uuid4())
        db.add(Incident(
            id=inc2, incident_number="INC-20260408-091500",
            alert_name="HostDiskUsageHigh", title="HostDiskUsageHigh on 10.0.1.51:9100",
            status="resolved", severity="warning", instance="10.0.1.51:9100",
            resource_type="DISK", domain_type="HOST", component_type="app",
            root_cause="Disk /var/log đầy 93% do log growth", llm_confidence=0.92,
            canonical_root_cause="disk_full_due_to_log_growth",
            knowledge_source="rule", summary="Disk đầy do /var/log tăng 12GB trong 24h. Đã cleanup.",
            final_status="resolved", verification_status="success",
            created_at=now - timedelta(hours=2),
        ))

        # Incident 3 - evidence_collecting (RAM)
        inc3 = str(uuid.uuid4())
        db.add(Incident(
            id=inc3, incident_number="INC-20260408-081200",
            alert_name="HostMemoryHigh", title="HostMemoryHigh on 10.0.1.52:9100",
            status="evidence_collecting", severity="critical", instance="10.0.1.52:9100",
            resource_type="RAM", domain_type="HOST",
            created_at=now - timedelta(hours=3),
        ))

        # Incident 4 - suppressed
        inc4 = str(uuid.uuid4())
        db.add(Incident(
            id=inc4, incident_number="INC-20260407-010000",
            alert_name="HostCPUHigh", title="HostCPUHigh on 10.0.1.53:9100",
            status="resolved", severity="warning", instance="10.0.1.53:9100",
            resource_type="CPU", summary="CPU tăng do backup job — đã xác nhận bình thường.",
            root_cause="CPU tăng do rsync backup", canonical_root_cause="cpu_spike_due_to_backup_job",
            knowledge_source="llm", llm_confidence=0.72, final_status="resolved",
            created_at=now - timedelta(days=1),
        ))

        # Incident 5 - executed
        inc5 = str(uuid.uuid4())
        db.add(Incident(
            id=inc5, incident_number="INC-20260407-180000",
            alert_name="HostDiskInodeHigh", title="HostDiskInodeHigh on 10.0.1.54:9100",
            status="executed", severity="warning", instance="10.0.1.54:9100",
            resource_type="DISK", domain_type="HOST",
            root_cause="Inode đầy do session files trong /tmp",
            llm_confidence=0.78, knowledge_source="knowledge_exact",
            summary="Inode 95% do /tmp chứa quá nhiều session files.",
            created_at=now - timedelta(hours=8),
        ))

        # Audit events
        for etype, eid, actor, action, details in [
            ("incident_created", inc1, "system", "create", {"alert_name": "HostCPUHigh", "instance": "10.0.1.50:9100"}),
            ("evidence_collected", inc1, "system", "collect", {"commands": 45, "key_evidence": 6}),
            ("analysis_completed", inc1, "system", "analyze", {"confidence": 0.85, "model": "gemini-2.0-flash", "source": "llm"}),
            ("incident_created", inc2, "system", "create", {"alert_name": "HostDiskUsageHigh"}),
            ("action_approved", inc2, "operator", "approve", {"title": "Cleanup old logs"}),
            ("execution_completed", inc2, "system", "execute", {"success": True}),
            ("verification_completed", inc2, "system", "verify", {"result": "success"}),
            ("incident_created", inc3, "system", "create", {"alert_name": "HostMemoryHigh", "severity": "critical"}),
            ("incident_created", inc4, "system", "create", {"alert_name": "HostCPUHigh", "note": "resolved after RCA"}),
        ]:
            db.add(AuditEvent(event_type=etype, entity_type="incident", entity_id=eid,
                              actor=actor, action=action, details_json=details))

        # Knowledge
        db.add(RemediationKnowledge(
            domain_type="HOST", alert_name="HostDiskUsageHigh", resource_type="DISK",
            canonical_root_cause="disk_full_due_to_log_growth", issue_subtype="log_growth",
            root_cause_signature_v2="HOST|HostDiskUsageHigh|*|*|log_growth|disk_full_due_to_log_growth",
            short_title="Disk đầy do log growth", confidence=0.8, success_count=3, usage_count=4,
            remediation_steps_json=[
                {"title": "Cleanup old logs", "commands": ["find /var/log -name '*.log.*' -mtime +7 -delete"],
                 "risk_level": "low", "expected_effect": "Giải phóng disk space"},
                {"title": "Fix logrotate", "commands": ["logrotate -f /etc/logrotate.conf"],
                 "risk_level": "low", "expected_effect": "Đảm bảo log rotate đúng"},
                {"title": "Check app log level", "commands": ["grep -r 'level' /etc/app/logging.yml"],
                 "risk_level": "low", "expected_effect": "Giảm log volume"},
            ],
        ))


        await db.commit()

    await engine.dispose()
    print("✅ Seed data created!")
    print(f"   Incident 1 (action_proposed / CPU): {inc1}")
    print(f"   Incident 2 (resolved / DISK):       {inc2}")
    print(f"   Incident 3 (evidence_collecting):    {inc3}")
    print(f"   Incident 4 (resolved):             {inc4}")
    print(f"   Incident 5 (executed / DISK):        {inc5}")
    print(f"   + 1 knowledge entry, 9 audit events")


if __name__ == "__main__":
    asyncio.run(seed())
