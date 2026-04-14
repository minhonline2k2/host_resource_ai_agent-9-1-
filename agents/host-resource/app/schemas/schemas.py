"""Pydantic schemas for request/response validation."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional
from pydantic import BaseModel, Field


# === Alert Webhook ===
class AlertManagerAlert(BaseModel):
    status: str = "firing"
    labels: dict[str, str] = {}
    annotations: dict[str, str] = {}
    startsAt: Optional[str] = None
    endsAt: Optional[str] = None
    generatorURL: Optional[str] = None
    fingerprint: Optional[str] = None


class AlertManagerWebhook(BaseModel):
    version: str = "4"
    groupKey: Optional[str] = None
    truncatedAlerts: int = 0
    status: str = "firing"
    receiver: str = ""
    groupLabels: dict[str, str] = {}
    commonLabels: dict[str, str] = {}
    commonAnnotations: dict[str, str] = {}
    externalURL: Optional[str] = None
    alerts: list[AlertManagerAlert] = []


# === Alert Mapping ===
ALERT_RESOURCE_MAP: dict[str, str] = {
    "HostCPUHigh": "CPU", "HostLoadHigh": "CPU", "HostIOWaitHigh": "CPU", "HostStealHigh": "CPU",
    "HostMemoryHigh": "RAM", "HostAvailableMemoryLow": "RAM", "HostSwapHigh": "RAM", "HostOOMRisk": "RAM",
    "HostDiskUsageHigh": "DISK", "HostDiskUsageCritical": "DISK", "HostDiskInodeHigh": "DISK",
    "HostDiskIOHigh": "DISK", "HostDiskLatencyHigh": "DISK",
}


# === Incident Schemas ===
class IncidentStats(BaseModel):
    total: int = 0
    active: int = 0
    pending_approvals: int = 0


class IncidentListItem(BaseModel):
    id: str
    incident_number: str
    title: str
    status: str
    severity: str
    alert_type: str
    instance: str
    llm_confidence: Optional[float] = None
    created_at: datetime
    updated_at: Optional[datetime] = None


class EvidenceItem(BaseModel):
    id: int
    command_id: Optional[str] = None
    command_text: Optional[str] = None
    evidence_type: str = ""
    raw_text: Optional[str] = None
    parsed_json: Optional[dict] = None
    exit_code: Optional[int] = None
    duration_ms: Optional[int] = None
    is_key_evidence: bool = False
    collected_at: Optional[datetime] = None


class RootCauseItem(BaseModel):
    model_config = {"extra": "ignore"}
    name: str
    confidence: float
    why: str
    evidence_refs: list[str] = []


class LLMAnalysis(BaseModel):
    summary: str = ""
    root_causes: list[RootCauseItem] = []
    confidence: float = 0.0


class ActionProposal(BaseModel):
    id: str
    priority: int = 1
    title: str
    description: Optional[str] = None
    risk_level: str = "medium"
    commands: list[str] = []
    expected_effect: Optional[str] = None
    rollback_commands: list[str] = []
    warnings: list[str] = []
    status: str = "pending"
    created_at: Optional[datetime] = None


class ApprovalItem(BaseModel):
    id: int
    action_proposal_id: str
    decision: str
    decided_by: str = "operator"
    reason: Optional[str] = None
    decided_at: Optional[datetime] = None


class ExecutionItem(BaseModel):
    id: int
    step_no: int
    step_name: Optional[str] = None
    status: str
    command: Optional[str] = None
    stdout: Optional[str] = None
    stderr: Optional[str] = None
    exit_code: Optional[int] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None


class EventItem(BaseModel):
    event_type: str
    event_data: Optional[dict] = None
    created_at: Optional[datetime] = None


class IncidentDetail(BaseModel):
    incident: dict
    alerts: list[dict] = []
    evidence: list[EvidenceItem] = []
    llm_analysis: Optional[LLMAnalysis] = None
    action_proposals: list[ActionProposal] = []
    approvals: list[ApprovalItem] = []
    execution_results: list[ExecutionItem] = []
    events: list[EventItem] = []


# === Approval Request ===
class ApprovalRequest(BaseModel):
    action_proposal_id: str
    decision: str = Field(pattern="^(approved|canceled)$")
    decided_by: str = "operator"
    reason: Optional[str] = None
    selected_commands: Optional[list[int]] = None  # indices of commands to execute


# === Monitor Request ===
class MonitorRequest(BaseModel):
    duration_minutes: int = 15  # default 15 min watch


# === Audit ===
class AuditItem(BaseModel):
    id: int
    event_type: str
    entity_type: Optional[str] = None
    entity_id: Optional[str] = None
    actor: str = "system"
    action: Optional[str] = None
    details: Optional[dict] = None
    created_at: Optional[datetime] = None


# === LLM Response Schema ===
class LLMRemediationOption(BaseModel):
    model_config = {"extra": "ignore"}
    option_id: str = ""
    priority: int = 1
    title: str = ""
    description: str = ""
    risk_level: str = "medium"
    needs_approval: bool = True
    action_type: str = ""
    target: str = ""
    params: dict = {}
    commands: list[str] = []
    expected_effect: str = ""
    rollback_commands: list[str] = []
    pre_checks: list[str] = []
    post_checks: list[str] = []
    warnings: list[str] = []


class LLMRCAResponse(BaseModel):
    model_config = {"extra": "ignore"}
    symptom: str = ""
    immediate_cause: str = ""
    contributing_factors: list[str] = []
    root_cause_hypothesis: str = ""
    why_not_just_symptom: str = ""
    rca_level: str = "probable_root_cause"
    verification_status: str = "medium"
    confidence: float = 0.0
    impact: str = "medium"
    suspected_service: str = ""
    suspected_job: str = ""
    suspected_path: str = ""
    suspected_mount: str = ""
    canonical_root_cause: str = ""
    issue_subtype: str = ""
    evidence_refs: list[str] = []
    what_is_still_unknown: list[str] = []
    summary: str = ""
    root_causes: list[RootCauseItem] = []
    remediation_options: list[LLMRemediationOption] = []
    recommended_option: str = ""
    operator_message_vi: str = ""
    warnings: list[str] = []


# === Status Constants ===
class IncidentStatus:
    NEW = "new"
    DEDUPLICATED = "deduplicated"
    SUPPRESSED = "suppressed"
    EVIDENCE_COLLECTING = "evidence_collecting"
    EVIDENCE_COLLECTED = "evidence_collected"
    ANALYZING = "analyzing"
    ANALYSIS_FAILED = "analysis_failed"
    ACTION_PROPOSED = "action_proposed"
    APPROVED = "approved"
    CANCELED = "canceled"
    DISPATCHED = "dispatched"
    EXECUTING = "executing"
    EXECUTED = "executed"
    EXECUTION_FAILED = "execution_failed"
    RESOLVED = "resolved"
    CLOSED = "closed"
    MANUAL_REQUIRED = "manual_required"
    MONITORING = "monitoring"
    FAILED = "failed"

    ACTIVE_STATUSES = {
        NEW, EVIDENCE_COLLECTING, EVIDENCE_COLLECTED,
        ANALYZING, ACTION_PROPOSED, APPROVED, DISPATCHED, EXECUTING, MONITORING,
    }

    LABEL_VI = {
        "new": "Mới", "deduplicated": "Trùng lặp", "suppressed": "Không cảnh báo",
        "evidence_collecting": "Đang thu thập", "evidence_collected": "Đã thu thập",
        "analyzing": "Đang phân tích", "analysis_failed": "Phân tích lỗi",
        "action_proposed": "Chờ duyệt", "approved": "Đã duyệt", "canceled": "Đã hủy",
        "dispatched": "Đang gửi", "executing": "Đang xử lý", "executed": "Đã xử lý",
        "execution_failed": "Xử lý lỗi", "resolved": "Đã giải quyết",
        "closed": "Đã đóng", "manual_required": "Cần xử lý tay",
        "monitoring": "Đang theo dõi", "failed": "Thất bại",
    }
