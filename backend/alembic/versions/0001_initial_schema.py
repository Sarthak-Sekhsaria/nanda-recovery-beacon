"""Initial schema: workflows, checkpoints, artifacts, claims, events, api keys, idempotency.

Revision ID: 0001
Revises:
Create Date: 2026-07-10

Notable database-level guarantees created here:

* ``ux_claims_one_active_per_workflow`` - partial unique index. At most one active
  claim per workflow, enforced by PostgreSQL rather than by application code.
* ``trg_checkpoints_append_only`` / ``trg_recovery_events_append_only`` - BEFORE
  UPDATE OR DELETE triggers that raise, making those tables append-only.
* ``ux_workflows_idempotency`` - one workflow per (creator agent, idempotency key).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels = None
depends_on = None

WORKFLOW_STATUS = (
    "active",
    "suspected_failed",
    "recoverable",
    "claimed",
    "completed",
    "cancelled",
    "dead_letter",
)
PRIORITY = ("low", "normal", "high", "critical")
FAILURE_POLICY = ("recover", "dead_letter")
CLAIM_STATUS = ("active", "released", "expired", "completed")
VERIFICATION_STATUS = ("unverified", "verified", "failed")
EVENT_TYPE = (
    "workflow_created",
    "heartbeat_received",
    "checkpoint_created",
    "failure_suspected",
    "workflow_made_recoverable",
    "claim_acquired",
    "claim_renewed",
    "claim_expired",
    "claim_released",
    "workflow_resumed",
    "stale_update_rejected",
    "workflow_completed",
    "workflow_cancelled",
    "workflow_dead_lettered",
    "artifact_registered",
    "artifact_verification_failed",
    "explicit_failure_reported",
)

APPEND_ONLY_FUNCTION = """
CREATE OR REPLACE FUNCTION beacon_reject_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'append_only_violation: rows in % may not be updated or deleted', TG_TABLE_NAME
        USING ERRCODE = 'restrict_violation';
END;
$$ LANGUAGE plpgsql;
"""


def _enum(name: str, values: tuple[str, ...]) -> postgresql.ENUM:
    return postgresql.ENUM(*values, name=name, create_type=False)


def upgrade() -> None:
    bind = op.get_bind()
    for name, values in (
        ("workflow_status", WORKFLOW_STATUS),
        ("workflow_priority", PRIORITY),
        ("failure_policy", FAILURE_POLICY),
        ("claim_status", CLAIM_STATUS),
        ("verification_status", VERIFICATION_STATUS),
        ("recovery_event_type", EVENT_TYPE),
    ):
        postgresql.ENUM(*values, name=name).create(bind, checkfirst=True)

    op.create_table(
        "workflows",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("objective", sa.Text(), nullable=False),
        sa.Column("status", _enum("workflow_status", WORKFLOW_STATUS), nullable=False),
        sa.Column("priority", _enum("workflow_priority", PRIORITY), nullable=False),
        sa.Column("failure_policy", _enum("failure_policy", FAILURE_POLICY), nullable=False),
        sa.Column("creator_agent_id", sa.String(128), nullable=False),
        sa.Column("current_agent_id", sa.String(128), nullable=True),
        sa.Column("heartbeat_timeout_seconds", sa.Integer(), nullable=False),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("checkpoint_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("current_checkpoint_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("latest_checkpoint_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("lease_generation", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("recovery_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_recoveries", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("tags", postgresql.ARRAY(sa.String(64)), nullable=False, server_default="{}"),
        sa.Column("metadata", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("idempotency_key", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("recovered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("heartbeat_timeout_seconds > 0", name="ck_workflows_heartbeat_positive"),
        sa.CheckConstraint("current_checkpoint_version >= 0", name="ck_workflows_version_nonneg"),
        sa.UniqueConstraint("creator_agent_id", "idempotency_key", name="ux_workflows_idempotency"),
    )
    op.create_index("ix_workflows_status", "workflows", ["status"])
    op.create_index("ix_workflows_priority", "workflows", ["priority"])
    op.create_index("ix_workflows_creator_agent_id", "workflows", ["creator_agent_id"])
    op.create_index("ix_workflows_current_agent_id", "workflows", ["current_agent_id"])
    op.create_index(
        "ix_workflows_status_priority_created", "workflows", ["status", "priority", "created_at"]
    )
    op.create_index("ix_workflows_heartbeat_deadline", "workflows", ["status", "last_heartbeat_at"])
    op.create_index("ix_workflows_tags", "workflows", ["tags"], postgresql_using="gin")

    op.create_table(
        "checkpoints",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "workflow_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workflows.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("parent_version", sa.Integer(), nullable=True),
        sa.Column("objective", sa.Text(), nullable=False),
        sa.Column("completed_steps", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("remaining_steps", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("decisions", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("next_action", sa.Text(), nullable=True),
        sa.Column("context_summary", sa.Text(), nullable=True),
        sa.Column("variables", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("producing_agent_id", sa.String(128), nullable=False),
        sa.Column("lease_generation", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("schema_version", sa.String(16), nullable=False, server_default="1.0"),
        sa.Column("content_checksum", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("version > 0", name="ck_checkpoints_version_positive"),
        sa.UniqueConstraint("workflow_id", "version", name="ux_checkpoints_workflow_version"),
    )
    op.execute(
        "CREATE INDEX ix_checkpoints_workflow_version_desc ON checkpoints (workflow_id, version DESC)"
    )

    op.create_table(
        "artifacts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "workflow_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workflows.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("content_type", sa.String(128), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("uri", sa.Text(), nullable=True),
        sa.Column("storage_key", sa.Text(), nullable=True),
        sa.Column("sha256", sa.String(64), nullable=True),
        sa.Column("checkpoint_version", sa.Integer(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "verification_status",
            _enum("verification_status", VERIFICATION_STATUS),
            nullable=False,
            server_default="unverified",
        ),
        sa.Column("verification_error", sa.Text(), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("produced_by_agent_id", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("uri IS NOT NULL OR storage_key IS NOT NULL", name="ck_artifacts_has_location"),
        sa.UniqueConstraint(
            "workflow_id", "name", "checkpoint_version", name="ux_artifacts_workflow_name_version"
        ),
    )
    op.create_index("ix_artifacts_workflow", "artifacts", ["workflow_id"])

    op.create_table(
        "claims",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "workflow_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workflows.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("agent_id", sa.String(128), nullable=False),
        sa.Column("lease_token_hash", sa.String(64), nullable=False),
        sa.Column("lease_token_prefix", sa.String(16), nullable=False),
        sa.Column("lease_generation", sa.BigInteger(), nullable=False),
        sa.Column("status", _enum("claim_status", CLAIM_STATUS), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_renewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("release_reason", sa.String(255), nullable=True),
        sa.Column("renewal_count", sa.Integer(), nullable=False, server_default="0"),
        sa.UniqueConstraint("workflow_id", "lease_generation", name="ux_claims_workflow_generation"),
    )
    op.create_index("ix_claims_agent_id", "claims", ["agent_id"])
    op.create_index("ix_claims_expires_at", "claims", ["expires_at"])
    op.create_index("ix_claims_active_expiry", "claims", ["status", "expires_at"])
    # The core mutual-exclusion guarantee.
    op.create_index(
        "ux_claims_one_active_per_workflow",
        "claims",
        ["workflow_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )

    op.create_table(
        "recovery_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "workflow_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workflows.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_type", _enum("recovery_event_type", EVENT_TYPE), nullable=False),
        sa.Column("actor_agent_id", sa.String(128), nullable=True),
        sa.Column("request_id", sa.String(64), nullable=True),
        sa.Column("checkpoint_version", sa.Integer(), nullable=True),
        sa.Column("lease_generation", sa.BigInteger(), nullable=True),
        sa.Column("metadata", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_recovery_events_event_type", "recovery_events", ["event_type"])
    op.create_index("ix_events_workflow_created", "recovery_events", ["workflow_id", "created_at"])
    op.create_index("ix_events_created", "recovery_events", ["created_at"])

    op.create_table(
        "api_keys",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("agent_id", sa.String(128), nullable=False, unique=True),
        sa.Column("label", sa.String(255), nullable=True),
        sa.Column("key_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("key_prefix", sa.String(16), nullable=False),
        sa.Column("is_admin", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_api_keys_key_prefix", "api_keys", ["key_prefix"])

    op.create_table(
        "idempotency_records",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("agent_id", sa.String(128), nullable=False),
        sa.Column("endpoint", sa.String(255), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=False),
        sa.Column("response_body", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint(
            "agent_id", "endpoint", "idempotency_key", name="ux_idempotency_agent_endpoint_key"
        ),
    )

    # Append-only enforcement.
    op.execute(APPEND_ONLY_FUNCTION)
    op.execute(
        "CREATE TRIGGER trg_checkpoints_append_only BEFORE UPDATE OR DELETE ON checkpoints "
        "FOR EACH ROW EXECUTE FUNCTION beacon_reject_mutation()"
    )
    op.execute(
        "CREATE TRIGGER trg_recovery_events_append_only BEFORE UPDATE OR DELETE ON recovery_events "
        "FOR EACH ROW EXECUTE FUNCTION beacon_reject_mutation()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_recovery_events_append_only ON recovery_events")
    op.execute("DROP TRIGGER IF EXISTS trg_checkpoints_append_only ON checkpoints")
    op.execute("DROP FUNCTION IF EXISTS beacon_reject_mutation()")

    op.drop_table("idempotency_records")
    op.drop_table("api_keys")
    op.drop_table("recovery_events")
    op.drop_table("claims")
    op.drop_table("artifacts")
    op.drop_table("checkpoints")
    op.drop_table("workflows")

    bind = op.get_bind()
    for name in (
        "recovery_event_type",
        "verification_status",
        "claim_status",
        "failure_policy",
        "workflow_priority",
        "workflow_status",
    ):
        postgresql.ENUM(name=name).drop(bind, checkfirst=True)
