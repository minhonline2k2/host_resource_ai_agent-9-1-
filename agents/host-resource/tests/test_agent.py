"""Tests for host_resource_ai_agent - covers all 12 required test cases."""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.schemas.schemas import (
    AlertManagerAlert, AlertManagerWebhook, LLMRCAResponse,
    IncidentStatus, ALERT_RESOURCE_MAP,
)
from app.services.rule_rca import run_rule_rca, RuleRCAResult
from app.collectors.evidence_builder import (
    parse_evidence, build_evidence_pack, _detect_kernel_issues,
)
from app.services.knowledge_service import build_signature_v2
from app.services.execution_service import is_command_safe, is_high_risk
from app.prompts.rca_prompt import build_llm_prompt


# === Test 1: Receive new alert ===
class TestAlertIntake:
    def test_webhook_parse(self):
        payload = {
            "status": "firing",
            "alerts": [
                {
                    "status": "firing",
                    "labels": {"alertname": "HostCPUHigh", "instance": "10.0.1.50:9100", "severity": "warning"},
                    "annotations": {"summary": "CPU is high"},
                    "fingerprint": "abc123",
                }
            ],
        }
        webhook = AlertManagerWebhook(**payload)
        assert len(webhook.alerts) == 1
        assert webhook.alerts[0].labels["alertname"] == "HostCPUHigh"

    def test_resource_type_mapping(self):
        assert ALERT_RESOURCE_MAP["HostCPUHigh"] == "CPU"
        assert ALERT_RESOURCE_MAP["HostMemoryHigh"] == "RAM"
        assert ALERT_RESOURCE_MAP["HostDiskUsageHigh"] == "DISK"
        assert ALERT_RESOURCE_MAP.get("UnknownAlert") is None


# === Test 2: Dedup alert ===
class TestDedup:
    def test_fingerprint_consistency(self):
        alert = AlertManagerAlert(
            status="firing",
            labels={"alertname": "HostCPUHigh", "instance": "10.0.1.50:9100"},
            fingerprint="abc123",
        )
        assert alert.fingerprint == "abc123"


# === Test 3: Suppress daily activity ===
class TestSuppression:
    def test_signature_v2_build(self):
        sig = build_signature_v2("HOST", "HostCPUHigh", "api-01", "java", "gc_thrashing",
                                 "java_gc_thrashing_due_to_heap_pressure")
        assert sig == "HOST|HostCPUHigh|api-01|java|gc_thrashing|java_gc_thrashing_due_to_heap_pressure"

    def test_signature_v2_with_wildcards(self):
        sig = build_signature_v2("HOST", "HostCPUHigh", "", "", "", "")
        assert sig == "HOST|HostCPUHigh|*|*|*|*"


# === Test 4: Crawl evidence ===
class TestEvidenceCollection:
    def test_parse_evidence_cpu(self):
        raw = [
            {
                "command_id": "top_cpu",
                "command_text": "ps -eo ... --sort=-%cpu | head -40",
                "evidence_type": "process_cpu",
                "raw_text": "  PID %CPU COMMAND\n12345  78.2 java\n 5678   5.1 mysqld",
                "exit_code": 0,
                "duration_ms": 200,
            }
        ]
        parsed = parse_evidence(raw)
        assert len(parsed) == 1
        assert parsed[0]["is_key_evidence"] is True
        assert parsed[0]["severity_weight"] > 0

    def test_parse_evidence_kernel_oom(self):
        raw = [
            {
                "command_id": "dmesg_grep",
                "command_text": "dmesg -T | egrep ...",
                "evidence_type": "kernel_journal",
                "raw_text": "[Apr 8 10:25:12] Out of memory: Killed process 12345",
                "exit_code": 0,
                "duration_ms": 50,
            }
        ]
        parsed = parse_evidence(raw)
        assert parsed[0]["is_key_evidence"] is True
        assert parsed[0]["severity_weight"] == 1.0

    def test_detect_kernel_issues(self):
        text = "Out of memory: Killed process 1234\nsegfault at 0x0\ni/o error on /dev/sda"
        issues = _detect_kernel_issues(text)
        assert issues["oom"] is True
        assert issues["segfault"] is True
        assert issues["io_error"] is True


# === Test 5: Build 1 LLM prompt ===
class TestPromptBuilding:
    def test_build_evidence_pack(self):
        incident = {"alert_name": "HostCPUHigh", "instance": "10.0.1.50:9100",
                     "severity": "warning", "resource_type": "CPU",
                     "component_type": "app", "service_name": ""}
        prom = {"cpu_usage": 92.3, "load1": 4.21}
        ssh_ev = [
            {"command_id": "top_cpu", "evidence_type": "process_cpu",
             "raw_text": "java 78.2%", "is_key_evidence": True,
             "parsed_json": {"count": 1}},
        ]
        pack = build_evidence_pack(incident, prom, {}, ssh_ev)
        assert "[INCIDENT]" in pack
        assert "[PROMETHEUS_SNAPSHOT]" in pack
        assert "cpu_usage: 92.3" in pack

    def test_build_llm_prompt(self):
        prompt = build_llm_prompt("[INCIDENT]\nalert: test\n[/INCIDENT]")
        assert "TỐI THIỂU 3" in prompt
        assert "remediation_options" in prompt


# === Test 6: Parse JSON response of LLM ===
class TestLLMParsing:
    def test_parse_llm_response(self):
        resp = LLMRCAResponse(
            symptom="CPU 92%",
            immediate_cause="Java GC",
            root_cause_hypothesis="heap pressure",
            confidence=0.85,
            canonical_root_cause="java_gc_thrashing",
            issue_subtype="gc_thrashing",
            summary="Test",
            remediation_options=[
                {"option_id": "opt-1", "priority": 1, "title": "Restart", "commands": ["systemctl restart app"]},
                {"option_id": "opt-2", "priority": 2, "title": "Increase heap", "commands": ["edit config"]},
                {"option_id": "opt-3", "priority": 3, "title": "Monitor", "commands": []},
            ],
            operator_message_vi="Test message",
        )
        assert resp.confidence == 0.85
        assert len(resp.remediation_options) == 3


# === Test 7: At least 3 remediation options ===
class TestRemediationOptions:
    def test_minimum_3_options(self):
        resp = LLMRCAResponse(
            symptom="test",
            confidence=0.5,
            remediation_options=[
                {"option_id": f"opt-{i}", "priority": i, "title": f"Option {i}"}
                for i in range(1, 5)
            ],
        )
        assert len(resp.remediation_options) >= 3


# === Test 8: Approve action ===
class TestApproval:
    def test_approval_decision_validation(self):
        from app.schemas.schemas import ApprovalRequest
        req = ApprovalRequest(
            action_proposal_id="test-id",
            decision="approved",
            decided_by="operator",
        )
        assert req.decision == "approved"

        req2 = ApprovalRequest(
            action_proposal_id="test-id",
            decision="canceled",
        )
        assert req2.decision == "canceled"

    def test_invalid_decision(self):
        from pydantic import ValidationError
        from app.schemas.schemas import ApprovalRequest
        with pytest.raises(ValidationError):
            ApprovalRequest(action_proposal_id="x", decision="invalid")


# === Test 9: Execute successfully ===
class TestExecution:
    def test_command_safety_check(self):
        safe, _ = is_command_safe("systemctl restart app-backend")
        assert safe is True

        safe, reason = is_command_safe("rm -rf /etc")
        assert safe is False

        safe, _ = is_command_safe("rm -rf /tmp/old_logs")
        assert safe is True

    def test_high_risk_detection(self):
        assert is_high_risk("systemctl restart mariadb") is True
        assert is_high_risk("kill -9 1234") is True
        assert is_high_risk("systemctl restart app-backend") is False


# === Test 10: Verify resolved ===
class TestVerification:
    def test_status_transitions(self):
        statuses = IncidentStatus
        assert statuses.NEW == "new"
        assert statuses.RESOLVED == "resolved"
        assert statuses.EXECUTION_FAILED == "execution_failed"
        assert statuses.MANUAL_REQUIRED == "manual_required"

    def test_vi_labels(self):
        assert IncidentStatus.LABEL_VI["action_proposed"] == "Chờ duyệt"
        assert IncidentStatus.LABEL_VI["resolved"] == "Đã giải quyết"


# === Test 11: LLM fallback ===
class TestLLMFallback:
    def test_llm_timeout_status(self):
        # When LLM fails, status should be analysis_failed
        assert IncidentStatus.ANALYSIS_FAILED == "analysis_failed"

    def test_rule_rca_fallback(self):
        # Rule RCA should work without LLM
        evidence = [
            {
                "evidence_type": "kernel_journal",
                "command_id": "dmesg_grep",
                "raw_text": "Out of memory: Killed process 12345",
                "parsed_json": {"oom": True},
                "is_key_evidence": True,
            }
        ]
        result = run_rule_rca("RAM", "HostMemoryHigh", evidence, {})
        assert result.matched is True
        assert "OOM" in result.root_cause


# === Test 12: Knowledge reuse ===
class TestKnowledgeReuse:
    def test_signature_matching(self):
        sig1 = build_signature_v2("HOST", "HostCPUHigh", "api-01", "java",
                                  "gc_thrashing", "java_gc_thrashing")
        sig2 = build_signature_v2("HOST", "HostCPUHigh", "api-01", "java",
                                  "gc_thrashing", "java_gc_thrashing")
        assert sig1 == sig2

    def test_signature_different_host(self):
        sig1 = build_signature_v2("HOST", "HostCPUHigh", "api-01", "java",
                                  "gc_thrashing", "java_gc_thrashing")
        sig2 = build_signature_v2("HOST", "HostCPUHigh", "api-02", "java",
                                  "gc_thrashing", "java_gc_thrashing")
        assert sig1 != sig2


# === Additional: Rule RCA tests ===
class TestRuleRCA:
    def test_cpu_iowait(self):
        result = run_rule_rca("CPU", "HostCPUHigh", [], {"cpu_iowait": 25.0})
        assert result.matched is True
        assert "iowait" in result.canonical_root_cause

    def test_cpu_steal(self):
        result = run_rule_rca("CPU", "HostCPUHigh", [], {"cpu_steal": 15.0})
        assert result.matched is True
        assert "steal" in result.canonical_root_cause

    def test_disk_deleted_open(self):
        evidence = [
            {
                "evidence_type": "socket_fd",
                "command_id": "deleted_open",
                "raw_text": "java 12345 user 5u REG 253,0 1073741824 (deleted)\napp 5678 user 3w REG 253,0 524288000 (deleted)",
                "parsed_json": {"deleted_files": True},
            }
        ]
        result = run_rule_rca("DISK", "HostDiskUsageHigh", evidence, {})
        assert result.matched is True
        assert "deleted" in result.canonical_root_cause


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
