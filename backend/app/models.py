"""SQLAlchemy models.

Invariants enforced by the database itself (not just application code):

* ``ux_claims_one_active_per_workflow`` - a partial unique index guaranteeing at
  most one ``active`` claim per workflow, so two racing agents can never both
  hold a lease even if application logic is bypassed.
* ``checkpoints`` and ``recovery_events`` carry BEFORE UPDATE/DELETE triggers
  that raise an exception, making them append-only.
* ``ux_workflows_idempotency`` - one workflow per (creator agent, idempotency key).
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


class WorkflowStatus(str, enum.Enum):
    active = "active"
    suspected_failed = "suspected_failed"
    recoverable = "recoverable"
    claimed = "claimed"
    completed = "completed"
    cancelled = "cancelled"
    dead_letter = "dead_letter"


class Priority(str, enum.Enum):
    low = "low"
    normal = "normal"
    high = "high"
    critical = "critical"


PRIORITY_RANK: dict[str, int] = {"critical": 4, "high": 3, "normal": 2, "low": 1}


class FailurePolicy(str, enum.Enum):
    # Failed work becomes claimable by another agent.
    recover = "recover"
    # Failed work goes straight to dead_letter and is never auto-offered.
    dead_letter = "dead_letter"


class ClaimStatus(str, enum.Enum):
    active = "active"
    released = "released"
    expired = "expired"
    completed = "completed"


class VerificationStatus(str, enum.Enum):
    unverified = "unverified"
    verified = "verified"
    failed = "failed"


class EventType(str, enum.Enum):
    workflow_created = "workflow_created"
    heartbeat_received = "heartbeat_received"
    checkpoint_created = "checkpoint_created"
    failure_suspected = "failure_suspected"
    workflow_made_recoverable = "workflow_made_recoverable"
    claim_acquired = "claim_acquired"
    claim_renewed = "claim_renewed"
    claim_expired = "claim_expired"
    claim_released = "claim_released"
    workflow_resumed = "workflow_resumed"
    stale_update_rejected = "stale_update_rejected"
    workflow_completed = "workflow_completed"
    workflow_cancelled = "workflow_cancelled"
    workflow_dead_lettered = "workflow_dead_lettered"
    artifact_registered = "artifact_registered"
    artifact_verification_failed = "artifact_verification_failed"
    explicit_failure_reported = "explicit_failure_reported"


def _enum(python_enum: type[enum.Enum], name: str) -> SAEnum:
    return SAEnum(
        python_enum,
        name=name,
        native_enum=True,
        values_callable=lambda e: [member.value for member in e],
        validate_strings=True,
    )


TS = DateTime(timezone=True)
NOW = text("now()")


class Workflow(Base):
    __tablename__ = "workflows"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=_uuid)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    objective: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[WorkflowStatus] = mapped_column(
        _enum(WorkflowStatus, "workflow_status"),
        nullable=False,
        default=WorkflowStatus.active,
        index=True,
    )
    priority: Mapped[Priority] = mapped_column(
        _enum(Priority, "workflow_priority"), nullable=False, default=Priority.normal, index=True
    )
    failure_policy: Mapped[FailurePolicy] = mapped_column(
        _enum(FailurePolicy, "failure_policy"), nullable=False, default=FailurePolicy.recover
    )

    creator_agent_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    current_agent_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)

    heartbeat_timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=120)
    last_heartbeat_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)

    checkpoint_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    current_checkpoint_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    latest_checkpoint_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )

    # Monotonic fencing counter. Incremented on every successful claim; a lease
    # is only valid while its generation equals the workflow's generation.
    lease_generation: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    recovery_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_recoveries: Mapped[int] = mapped_column(Integer, nullable=False, default=3)

    tags: Mapped[list[str]] = mapped_column(ARRAY(String(64)), nullable=False, server_default="{}")
    meta: Mapped[dict] = mapped_column("metadata", JSONB, nullable=False, server_default="{}")
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True)

    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)
    updated_at: Mapped[datetime] = mapped_column(
        TS, nullable=False, server_default=NOW, onupdate=text("now()")
    )
    failed_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    recovered_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)

    checkpoints: Mapped[list[Checkpoint]] = relationship(
        back_populates="workflow", cascade="all, delete-orphan", lazy="noload"
    )
    artifacts: Mapped[list[Artifact]] = relationship(
        back_populates="workflow", cascade="all, delete-orphan", lazy="noload"
    )

    __table_args__ = (
        UniqueConstraint("creator_agent_id", "idempotency_key", name="ux_workflows_idempotency"),
        CheckConstraint("heartbeat_timeout_seconds > 0", name="ck_workflows_heartbeat_positive"),
        CheckConstraint("current_checkpoint_version >= 0", name="ck_workflows_version_nonneg"),
        Index("ix_workflows_status_priority_created", "status", "priority", "created_at"),
        Index("ix_workflows_heartbeat_deadline", "status", "last_heartbeat_at"),
        Index("ix_workflows_tags", "tags", postgresql_using="gin"),
    )

    @property
    def heartbeat_age_seconds(self) -> float:
        from app.db import utcnow

        return max(0.0, (utcnow() - self.last_heartbeat_at).total_seconds())


class Checkpoint(Base):
    """Immutable, versioned snapshot. Enforced append-only by a DB trigger."""

    __tablename__ = "checkpoints"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=_uuid)
    workflow_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("workflows.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_version: Mapped[int | None] = mapped_column(Integer, nullable=True)

    objective: Mapped[str] = mapped_column(Text, nullable=False)
    completed_steps: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    remaining_steps: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    decisions: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    next_action: Mapped[str | None] = mapped_column(Text, nullable=True)
    context_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    variables: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")

    producing_agent_id: Mapped[str] = mapped_column(String(128), nullable=False)
    lease_generation: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False, default="1.0")
    content_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)

    workflow: Mapped[Workflow] = relationship(back_populates="checkpoints", lazy="noload")

    __table_args__ = (
        UniqueConstraint("workflow_id", "version", name="ux_checkpoints_workflow_version"),
        CheckConstraint("version > 0", name="ck_checkpoints_version_positive"),
        Index("ix_checkpoints_workflow_version_desc", "workflow_id", text("version DESC")),
    )


class Artifact(Base):
    __tablename__ = "artifacts"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=_uuid)
    workflow_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("workflows.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    storage_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    checkpoint_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    verification_status: Mapped[VerificationStatus] = mapped_column(
        _enum(VerificationStatus, "verification_status"),
        nullable=False,
        default=VerificationStatus.unverified,
    )
    verification_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    produced_by_agent_id: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)

    workflow: Mapped[Workflow] = relationship(back_populates="artifacts", lazy="noload")

    __table_args__ = (
        UniqueConstraint(
            "workflow_id", "name", "checkpoint_version", name="ux_artifacts_workflow_name_version"
        ),
        CheckConstraint(
            "uri IS NOT NULL OR storage_key IS NOT NULL", name="ck_artifacts_has_location"
        ),
        Index("ix_artifacts_workflow", "workflow_id"),
    )


class Claim(Base):
    """A time-boxed exclusive lease over a workflow."""

    __tablename__ = "claims"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=_uuid)
    workflow_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("workflows.id", ondelete="CASCADE"), nullable=False
    )
    agent_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)

    # Raw lease tokens are never stored. Only a SHA-256 hash plus a short,
    # non-secret prefix used for log correlation.
    lease_token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    lease_token_prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    lease_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)

    status: Mapped[ClaimStatus] = mapped_column(
        _enum(ClaimStatus, "claim_status"), nullable=False, default=ClaimStatus.active
    )
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)
    expires_at: Mapped[datetime] = mapped_column(TS, nullable=False, index=True)
    last_renewed_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    released_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    release_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    renewal_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        # THE guarantee: at most one active claim per workflow, at the DB level.
        Index(
            "ux_claims_one_active_per_workflow",
            "workflow_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
        Index("ix_claims_active_expiry", "status", "expires_at"),
        UniqueConstraint("workflow_id", "lease_generation", name="ux_claims_workflow_generation"),
    )


class RecoveryEvent(Base):
    """Append-only audit record. Enforced by a DB trigger."""

    __tablename__ = "recovery_events"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=_uuid)
    workflow_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("workflows.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[EventType] = mapped_column(
        _enum(EventType, "recovery_event_type"), nullable=False, index=True
    )
    actor_agent_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    checkpoint_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    lease_generation: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    meta: Mapped[dict] = mapped_column("metadata", JSONB, nullable=False, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)

    __table_args__ = (
        Index("ix_events_workflow_created", "workflow_id", "created_at"),
        Index("ix_events_created", "created_at"),
    )


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=_uuid)
    agent_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    key_prefix: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)
    last_used_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)


class IdempotencyRecord(Base):
    """Stores the first response for a given (agent, endpoint, key) triple."""

    __tablename__ = "idempotency_records"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=_uuid)
    agent_id: Mapped[str] = mapped_column(String(128), nullable=False)
    endpoint: Mapped[str] = mapped_column(String(255), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    response_status: Mapped[int] = mapped_column(Integer, nullable=False)
    response_body: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)

    __table_args__ = (
        UniqueConstraint(
            "agent_id", "endpoint", "idempotency_key", name="ux_idempotency_agent_endpoint_key"
        ),
    )


# --- Append-only enforcement -------------------------------------------------
# Applied by Alembic in production and by the test bootstrap for the test schema.

APPEND_ONLY_FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION beacon_reject_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'append_only_violation: % rows in % may not be % ',
        TG_OP, TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'restrict_violation';
END;
$$ LANGUAGE plpgsql;
"""

APPEND_ONLY_TRIGGERS_SQL = [
    """
    CREATE TRIGGER trg_checkpoints_append_only
    BEFORE UPDATE OR DELETE ON checkpoints
    FOR EACH ROW EXECUTE FUNCTION beacon_reject_mutation();
    """,
    """
    CREATE TRIGGER trg_recovery_events_append_only
    BEFORE UPDATE OR DELETE ON recovery_events
    FOR EACH ROW EXECUTE FUNCTION beacon_reject_mutation();
    """,
]
