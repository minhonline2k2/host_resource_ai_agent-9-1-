"""initial schema - all tables

Revision ID: 001_initial
Revises: None
Create Date: 2026-04-08
"""
from alembic import op
import sqlalchemy as sa

revision = '001_initial'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('alerts_raw',
        sa.Column('id', sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column('fingerprint', sa.String(255), nullable=False, index=True),
        sa.Column('source', sa.String(100), server_default='alertmanager'),
        sa.Column('payload_json', sa.JSON(), nullable=False),
        sa.Column('received_at', sa.DateTime(), server_default=sa.func.now()),
    )

    op.create_table('alerts_normalized',
        sa.Column('id', sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column('raw_alert_id', sa.Integer(), sa.ForeignKey('alerts_raw.id'), nullable=False),
        sa.Column('alert_name', sa.String(255), nullable=False, index=True),
        sa.Column('status', sa.String(50), server_default='firing'),
        sa.Column('severity', sa.String(50), server_default='warning'),
        sa.Column('instance', sa.String(255), nullable=False, index=True),
        sa.Column('job_name', sa.String(255), server_default=''),
        sa.Column('resource_type', sa.String(50), nullable=False),
        sa.Column('domain_type', sa.String(50), server_default='HOST'),
        sa.Column('component_type', sa.String(100), server_default=''),
        sa.Column('service_name', sa.String(255), server_default=''),
        sa.Column('entity_name', sa.String(255), server_default=''),
        sa.Column('cluster_name', sa.String(255), server_default=''),
        sa.Column('alert_key', sa.String(255), nullable=False, index=True),
        sa.Column('labels_json', sa.JSON()),
        sa.Column('annotations_json', sa.JSON()),
        sa.Column('starts_at', sa.DateTime()),
        sa.Column('ends_at', sa.DateTime()),
        sa.Column('normalized_at', sa.DateTime(), server_default=sa.func.now()),
    )

    op.create_table('incidents',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('incident_number', sa.String(50), unique=True, nullable=False),
        sa.Column('alert_name', sa.String(255), nullable=False, index=True),
        sa.Column('title', sa.String(255), nullable=False),
        sa.Column('status', sa.String(50), nullable=False, server_default='new', index=True),
        sa.Column('severity', sa.String(50), server_default='warning'),
        sa.Column('instance', sa.String(255), nullable=False, index=True),
        sa.Column('resource_type', sa.String(50), nullable=False),
        sa.Column('domain_type', sa.String(50), server_default='HOST'),
        sa.Column('component_type', sa.String(100), server_default=''),
        sa.Column('service_name', sa.String(255), server_default=''),
        sa.Column('entity_name', sa.String(255), server_default=''),
        sa.Column('cluster_name', sa.String(255), server_default=''),
        sa.Column('root_cause', sa.Text()),
        sa.Column('immediate_cause', sa.Text()),
        sa.Column('canonical_root_cause', sa.String(255)),
        sa.Column('issue_subtype', sa.String(255)),
        sa.Column('root_cause_signature_v2', sa.String(255), index=True),
        sa.Column('root_cause_summary', sa.Text()),
        sa.Column('llm_confidence', sa.Float()),
        sa.Column('rca_level', sa.String(50)),
        sa.Column('verification_status', sa.String(50)),
        sa.Column('knowledge_source', sa.String(50)),
        sa.Column('knowledge_match_score', sa.Float()),
        sa.Column('reused_from_incident_id', sa.String(36)),
        sa.Column('reused_knowledge_id', sa.Integer()),
        sa.Column('summary', sa.Text()),
        sa.Column('context_json', sa.JSON()),
        sa.Column('ai_analysis_json', sa.JSON()),
        sa.Column('llm_prompt_text', sa.Text()),
        sa.Column('llm_raw_response', sa.Text()),
        sa.Column('selected_option_id', sa.String(36)),
        sa.Column('final_status', sa.String(50)),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_index('ix_incidents_status_severity', 'incidents', ['status', 'severity'])

    op.create_table('incident_evidence',
        sa.Column('id', sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column('incident_id', sa.String(36), sa.ForeignKey('incidents.id'), nullable=False, index=True),
        sa.Column('domain_type', sa.String(50), server_default='HOST'),
        sa.Column('source_type', sa.String(50), nullable=False),
        sa.Column('evidence_type', sa.String(100), nullable=False),
        sa.Column('command_id', sa.String(100)),
        sa.Column('command_text', sa.Text()),
        sa.Column('metric_name', sa.String(255)),
        sa.Column('metric_value', sa.Float()),
        sa.Column('metric_unit', sa.String(50)),
        sa.Column('labels_json', sa.JSON()),
        sa.Column('raw_text', sa.Text()),
        sa.Column('parsed_json', sa.JSON()),
        sa.Column('severity_weight', sa.Float(), server_default='0'),
        sa.Column('evidence_ref', sa.String(100)),
        sa.Column('duration_ms', sa.Integer()),
        sa.Column('exit_code', sa.Integer()),
        sa.Column('source_host', sa.String(255)),
        sa.Column('collector_name', sa.String(100)),
        sa.Column('is_key_evidence', sa.Boolean(), server_default='0'),
        sa.Column('observed_at', sa.DateTime()),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
    )

    op.create_table('remediation_options',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('incident_id', sa.String(36), sa.ForeignKey('incidents.id'), nullable=False, index=True),
        sa.Column('option_no', sa.Integer(), nullable=False),
        sa.Column('priority', sa.Integer(), server_default='1'),
        sa.Column('title', sa.String(255), nullable=False),
        sa.Column('description', sa.Text()),
        sa.Column('risk_level', sa.String(50), server_default='medium'),
        sa.Column('needs_approval', sa.Boolean(), server_default='1'),
        sa.Column('action_type', sa.String(100)),
        sa.Column('target', sa.String(255)),
        sa.Column('params_json', sa.JSON()),
        sa.Column('commands_json', sa.JSON()),
        sa.Column('expected_effect', sa.Text()),
        sa.Column('rollback_commands_json', sa.JSON()),
        sa.Column('pre_checks_json', sa.JSON()),
        sa.Column('post_checks_json', sa.JSON()),
        sa.Column('warnings_json', sa.JSON()),
        sa.Column('source', sa.String(50), server_default='llm'),
        sa.Column('status', sa.String(50), server_default='pending'),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
    )

    op.create_table('approvals',
        sa.Column('id', sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column('incident_id', sa.String(36), sa.ForeignKey('incidents.id'), nullable=False, index=True),
        sa.Column('action_proposal_id', sa.String(36), nullable=False),
        sa.Column('decision', sa.String(50), nullable=False),
        sa.Column('decided_by', sa.String(100), server_default='operator'),
        sa.Column('reason', sa.Text()),
        sa.Column('decided_at', sa.DateTime(), server_default=sa.func.now()),
    )

    op.create_table('execution_logs',
        sa.Column('id', sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column('incident_id', sa.String(36), sa.ForeignKey('incidents.id'), nullable=False, index=True),
        sa.Column('action_proposal_id', sa.String(36), nullable=False),
        sa.Column('step_no', sa.Integer(), nullable=False),
        sa.Column('step_name', sa.String(255)),
        sa.Column('status', sa.String(50), nullable=False),
        sa.Column('command', sa.Text()),
        sa.Column('stdout', sa.Text()),
        sa.Column('stderr', sa.Text()),
        sa.Column('exit_code', sa.Integer()),
        sa.Column('started_at', sa.DateTime()),
        sa.Column('finished_at', sa.DateTime()),
    )

    op.create_table('verification_results',
        sa.Column('id', sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column('incident_id', sa.String(36), sa.ForeignKey('incidents.id'), nullable=False, index=True),
        sa.Column('verification_type', sa.String(100), nullable=False),
        sa.Column('result', sa.String(50), nullable=False),
        sa.Column('details_json', sa.JSON()),
        sa.Column('verified_at', sa.DateTime(), server_default=sa.func.now()),
    )

    op.create_table('remediation_knowledge',
        sa.Column('id', sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column('domain_type', sa.String(50), nullable=False, index=True),
        sa.Column('component_type', sa.String(100), server_default=''),
        sa.Column('service_name', sa.String(255), server_default=''),
        sa.Column('alert_name', sa.String(255), nullable=False, index=True),
        sa.Column('resource_type', sa.String(50), nullable=False),
        sa.Column('canonical_root_cause', sa.String(255), nullable=False, index=True),
        sa.Column('issue_subtype', sa.String(255), server_default=''),
        sa.Column('root_cause_signature_v2', sa.String(255), index=True),
        sa.Column('short_title', sa.String(255)),
        sa.Column('remediation_steps_json', sa.JSON()),
        sa.Column('risk_notes', sa.Text()),
        sa.Column('approval_policy', sa.String(100), server_default='required'),
        sa.Column('source', sa.String(50), server_default='learned'),
        sa.Column('confidence', sa.Float(), server_default='0.5'),
        sa.Column('success_count', sa.Integer(), server_default='0'),
        sa.Column('failure_count', sa.Integer(), server_default='0'),
        sa.Column('usage_count', sa.Integer(), server_default='0'),
        sa.Column('last_used_at', sa.DateTime()),
        sa.Column('last_success_at', sa.DateTime()),
        sa.Column('last_failure_at', sa.DateTime()),
        sa.Column('incident_id_ref', sa.String(36)),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now()),
    )

    op.create_table('incident_patterns',
        sa.Column('id', sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column('pattern_type', sa.String(50), nullable=False),
        sa.Column('domain_type', sa.String(50), server_default='HOST'),
        sa.Column('component_type', sa.String(100), server_default=''),
        sa.Column('entity_pattern', sa.String(255)),
        sa.Column('cluster_name_pattern', sa.String(255)),
        sa.Column('root_cause_signature_v2', sa.String(255)),
        sa.Column('description', sa.Text()),
        sa.Column('created_by', sa.String(100), server_default='system'),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
        sa.Column('active', sa.Boolean(), server_default='1'),
    )
    op.create_index('ix_patterns_type_domain', 'incident_patterns', ['pattern_type', 'domain_type'])

    op.create_table('audit_events',
        sa.Column('id', sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column('event_type', sa.String(100), nullable=False, index=True),
        sa.Column('entity_type', sa.String(100)),
        sa.Column('entity_id', sa.String(100)),
        sa.Column('actor', sa.String(100), server_default='system'),
        sa.Column('action', sa.String(100)),
        sa.Column('details_json', sa.JSON()),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), index=True),
    )

    op.create_table('incident_events',
        sa.Column('id', sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column('incident_id', sa.String(36), sa.ForeignKey('incidents.id'), nullable=False, index=True),
        sa.Column('event_type', sa.String(100), nullable=False),
        sa.Column('event_data_json', sa.JSON()),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
    )


def downgrade() -> None:
    tables = [
        'incident_events', 'audit_events', 'incident_patterns',
        'remediation_knowledge', 'verification_results', 'execution_logs',
        'approvals', 'remediation_options', 'incident_evidence',
        'incidents', 'alerts_normalized', 'alerts_raw',
    ]
    for t in tables:
        op.drop_table(t)
