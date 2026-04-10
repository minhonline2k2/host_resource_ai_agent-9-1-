"""SQLAlchemy ORM models for all database tables."""

from __future__ import annotations

from datetime import datetime
from sqlalchemy import (
    Column, String, Integer, Float, Text, Boolean, DateTime, JSON, Enum, ForeignKey, Index, func
)
from sqlalchemy.orm import relationship

from app.core.database import Base


class AlertRaw(Base):
    __tablename__ = "alerts_raw"

    id = Column(Integer, primary_key=True, autoincrement=True)
    fingerprint = Column(String(255), nullable=False, index=True)
    source = Column(String(100), default="alertmanager")
    payload_json = Column(JSON, nullable=False)
    received_at = Column(DateTime, default=func.now(), nullable=False)


class AlertNormalized(Base):
    __tablename__ = "alerts_normalized"

    id = Column(Integer, primary_key=True, autoincrement=True)
    raw_alert_id = Column(Integer, ForeignKey("alerts_raw.id"), nullable=False)
    alert_name = Column(String(255), nullable=False, index=True)
    status = Column(String(50), default="firing")
    severity = Column(String(50), default="warning")
    instance = Column(String(255), nullable=False, index=True)
    job_name = Column(String(255), default="")
    resource_type = Column(String(50), nullable=False)  # CPU, RAM, DISK
    domain_type = Column(String(50), default="HOST")
    component_type = Column(String(100), default="")
    service_name = Column(String(255), default="")
    entity_name = Column(String(255), default="")
    cluster_name = Column(String(255), default="")
    alert_key = Column(String(512), nullable=False, index=True)
    labels_json = Column(JSON)
    annotations_json = Column(JSON)
    starts_at = Column(DateTime)
    ends_at = Column(DateTime)
    normalized_at = Column(DateTime, default=func.now())


class Incident(Base):
    __tablename__ = "incidents"

    id = Column(String(36), primary_key=True)
    incident_number = Column(String(50), unique=True, nullable=False)
    alert_name = Column(String(255), nullable=False, index=True)
    title = Column(String(512), nullable=False)
    status = Column(String(50), default="new", nullable=False, index=True)
    severity = Column(String(50), default="warning")
    instance = Column(String(255), nullable=False, index=True)
    resource_type = Column(String(50), nullable=False)
    domain_type = Column(String(50), default="HOST")
    component_type = Column(String(100), default="")
    service_name = Column(String(255), default="")
    entity_name = Column(String(255), default="")
    cluster_name = Column(String(255), default="")

    # RCA fields
    root_cause = Column(Text)
    immediate_cause = Column(Text)
    canonical_root_cause = Column(String(512))
    issue_subtype = Column(String(255))
    root_cause_signature_v2 = Column(String(1024), index=True)
    root_cause_summary = Column(Text)
    llm_confidence = Column(Float)
    rca_level = Column(String(50))
    verification_status = Column(String(50))

    # Knowledge reuse
    knowledge_source = Column(String(50))  # rule, knowledge_exact, knowledge_partial, llm
    knowledge_match_score = Column(Float)
    reused_from_incident_id = Column(String(36))
    reused_knowledge_id = Column(Integer)

    # Context
    summary = Column(Text)
    context_json = Column(JSON)
    ai_analysis_json = Column(JSON)
    llm_prompt_text = Column(Text)  # Full prompt sent to LLM for debugging
    llm_raw_response = Column(Text)  # Raw text returned by LLM for debugging
    selected_option_id = Column(String(36))
    final_status = Column(String(50))

    created_at = Column(DateTime, default=func.now(), nullable=False)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    # Relationships
    evidence = relationship("IncidentEvidence", back_populates="incident", lazy="selectin")
    remediation_options = relationship("RemediationOption", back_populates="incident", lazy="selectin")
    approvals = relationship("Approval", back_populates="incident", lazy="selectin")
    execution_logs = relationship("ExecutionLog", back_populates="incident", lazy="selectin")
    verifications = relationship("VerificationResult", back_populates="incident", lazy="selectin")
    events = relationship("IncidentEvent", back_populates="incident", lazy="selectin")

    __table_args__ = (
        Index("ix_incidents_status_severity", "status", "severity"),
    )


class IncidentEvidence(Base):
    __tablename__ = "incident_evidence"

    id = Column(Integer, primary_key=True, autoincrement=True)
    incident_id = Column(String(36), ForeignKey("incidents.id"), nullable=False, index=True)
    domain_type = Column(String(50), default="HOST")
    source_type = Column(String(50), nullable=False)  # ssh, prometheus, rule
    evidence_type = Column(String(100), nullable=False)  # baseline, process_cpu, disk, etc.
    command_id = Column(String(100))
    command_text = Column(Text)
    metric_name = Column(String(255))
    metric_value = Column(Float)
    metric_unit = Column(String(50))
    labels_json = Column(JSON)
    raw_text = Column(Text)
    parsed_json = Column(JSON)
    severity_weight = Column(Float, default=0.0)
    evidence_ref = Column(String(100))
    duration_ms = Column(Integer)
    exit_code = Column(Integer)
    source_host = Column(String(255))
    collector_name = Column(String(100))
    is_key_evidence = Column(Boolean, default=False)
    observed_at = Column(DateTime)
    created_at = Column(DateTime, default=func.now())

    incident = relationship("Incident", back_populates="evidence")


class RemediationOption(Base):
    __tablename__ = "remediation_options"

    id = Column(String(36), primary_key=True)
    incident_id = Column(String(36), ForeignKey("incidents.id"), nullable=False, index=True)
    option_no = Column(Integer, nullable=False)
    priority = Column(Integer, default=1)
    title = Column(String(512), nullable=False)
    description = Column(Text)
    risk_level = Column(String(50), default="medium")
    needs_approval = Column(Boolean, default=True)
    action_type = Column(String(100))
    target = Column(String(255))
    params_json = Column(JSON)
    commands_json = Column(JSON)
    expected_effect = Column(Text)
    rollback_commands_json = Column(JSON)
    pre_checks_json = Column(JSON)
    post_checks_json = Column(JSON)
    warnings_json = Column(JSON)
    source = Column(String(50), default="llm")
    status = Column(String(50), default="pending")
    created_at = Column(DateTime, default=func.now())

    incident = relationship("Incident", back_populates="remediation_options")


class Approval(Base):
    __tablename__ = "approvals"

    id = Column(Integer, primary_key=True, autoincrement=True)
    incident_id = Column(String(36), ForeignKey("incidents.id"), nullable=False, index=True)
    action_proposal_id = Column(String(36), nullable=False)
    decision = Column(String(50), nullable=False)  # approved, canceled
    decided_by = Column(String(100), default="operator")
    reason = Column(Text)
    decided_at = Column(DateTime, default=func.now())

    incident = relationship("Incident", back_populates="approvals")


class ExecutionLog(Base):
    __tablename__ = "execution_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    incident_id = Column(String(36), ForeignKey("incidents.id"), nullable=False, index=True)
    action_proposal_id = Column(String(36), nullable=False)
    step_no = Column(Integer, nullable=False)
    step_name = Column(String(255))
    status = Column(String(50), nullable=False)
    command = Column(Text)
    stdout = Column(Text)
    stderr = Column(Text)
    exit_code = Column(Integer)
    started_at = Column(DateTime)
    finished_at = Column(DateTime)

    incident = relationship("Incident", back_populates="execution_logs")


class VerificationResult(Base):
    __tablename__ = "verification_results"

    id = Column(Integer, primary_key=True, autoincrement=True)
    incident_id = Column(String(36), ForeignKey("incidents.id"), nullable=False, index=True)
    verification_type = Column(String(100), nullable=False)
    result = Column(String(50), nullable=False)  # success, partial, failed, unknown
    details_json = Column(JSON)
    verified_at = Column(DateTime, default=func.now())

    incident = relationship("Incident", back_populates="verifications")


class RemediationKnowledge(Base):
    __tablename__ = "remediation_knowledge"

    id = Column(Integer, primary_key=True, autoincrement=True)
    domain_type = Column(String(50), nullable=False, index=True)
    component_type = Column(String(100), default="")
    service_name = Column(String(255), default="")
    alert_name = Column(String(255), nullable=False, index=True)
    resource_type = Column(String(50), nullable=False)
    canonical_root_cause = Column(String(512), nullable=False, index=True)
    issue_subtype = Column(String(255), default="")
    root_cause_signature_v2 = Column(String(1024), index=True)
    short_title = Column(String(512))
    remediation_steps_json = Column(JSON)
    risk_notes = Column(Text)
    approval_policy = Column(String(100), default="required")
    source = Column(String(50), default="learned")
    confidence = Column(Float, default=0.5)
    success_count = Column(Integer, default=0)
    failure_count = Column(Integer, default=0)
    usage_count = Column(Integer, default=0)
    last_used_at = Column(DateTime)
    last_success_at = Column(DateTime)
    last_failure_at = Column(DateTime)
    incident_id_ref = Column(String(36))
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())


class IncidentPattern(Base):
    __tablename__ = "incident_patterns"

    id = Column(Integer, primary_key=True, autoincrement=True)
    pattern_type = Column(String(50), nullable=False)  # daily_activity, scheduled_job, known_noise, noise
    domain_type = Column(String(50), default="HOST")
    component_type = Column(String(100), default="")
    entity_pattern = Column(String(255))
    cluster_name_pattern = Column(String(255))
    root_cause_signature_v2 = Column(String(1024))
    description = Column(Text)
    created_by = Column(String(100), default="system")
    created_at = Column(DateTime, default=func.now())
    active = Column(Boolean, default=True)

    __table_args__ = (
        Index("ix_patterns_type_domain", "pattern_type", "domain_type"),
    )


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_type = Column(String(100), nullable=False, index=True)
    entity_type = Column(String(100))
    entity_id = Column(String(100))
    actor = Column(String(100), default="system")
    action = Column(String(100))
    details_json = Column(JSON)
    created_at = Column(DateTime, default=func.now(), index=True)


class IncidentEvent(Base):
    __tablename__ = "incident_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    incident_id = Column(String(36), ForeignKey("incidents.id"), nullable=False, index=True)
    event_type = Column(String(100), nullable=False)
    event_data_json = Column(JSON)
    created_at = Column(DateTime, default=func.now())

    incident = relationship("Incident", back_populates="events")
