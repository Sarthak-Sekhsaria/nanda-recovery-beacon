"""Pydantic request/response models. These drive the generated OpenAPI schema."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.config import settings
from app.models import (
    ClaimStatus,
    EventType,
    FailurePolicy,
    Priority,
    VerificationStatus,
    WorkflowStatus,
)

T = TypeVar("T")

SHA256_PATTERN = r"^[a-fA-F0-9]{64}$"


class Decision(BaseModel):
    """A judgement call already made. Recording it stops it being re-litigated."""

    decision: str = Field(
        min_length=1,
        max_length=2000,
        examples=["Only include programs open to international students"],
    )
    reason: str | None = Field(
        default=None,
        max_length=4000,
        description="Why the decision was made. Shorter than 12 characters counts as missing.",
        examples=["Required by the original request"],
    )
    made_at: datetime | None = None


# --- Workflows ---------------------------------------------------------------
class CheckpointBody(BaseModel):
    """The progress snapshot itself, shared by create-workflow and create-checkpoint."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "objective": "Compare five scholarship programs",
                "completed_steps": ["Found five programs", "Collected eligibility requirements"],
                "remaining_steps": ["Compare deadlines", "Produce recommendation"],
                "decisions": [
                    {
                        "decision": "Only include programs open to international students",
                        "reason": "Required by the original request",
                    }
                ],
                "next_action": "Compare application deadlines",
                "context_summary": (
                    "Five programs identified from the official directory. Eligibility "
                    "gathered for all five. Deadlines not yet compared."
                ),
                "variables": {"source_directory": "https://example.com/programs"},
                "schema_version": "1.0",
            }
        }
    )

    objective: str = Field(min_length=1, max_length=10_000)
    completed_steps: list[str] = Field(default_factory=list, max_length=500)
    remaining_steps: list[str] = Field(default_factory=list, max_length=500)
    decisions: list[Decision] = Field(default_factory=list, max_length=200)
    next_action: str | None = Field(default=None, max_length=4000)
    context_summary: str | None = Field(default=None, max_length=20_000)
    variables: dict[str, Any] = Field(default_factory=dict)
    schema_version: str = Field(default="1.0")

    @field_validator("schema_version")
    @classmethod
    def _known_schema(cls, value: str) -> str:
        # Accepted here, scored by the evaluator. Rejection happens in the route so
        # the agent gets a 422 with UNSUPPORTED_SCHEMA_VERSION rather than a 400.
        return value


class CheckpointCreate(CheckpointBody):
    parent_version: int = Field(
        ge=0,
        description=(
            "Required. The checkpoint version you last read. Must equal the workflow's "
            "current_checkpoint_version or the write is rejected with 409 "
            "STALE_CHECKPOINT_VERSION. Use 0 for the first checkpoint."
        ),
    )
    lease_token: str | None = Field(
        default=None, description="Required while an active claim exists on the workflow."
    )


class WorkflowCreate(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "title": "Scholarship comparison",
                "objective": "Compare five scholarship programs and recommend one",
                "priority": "high",
                "heartbeat_timeout_seconds": 120,
                "failure_policy": "recover",
                "tags": ["research", "scholarships"],
                "metadata": {"requested_by": "user-42"},
            }
        }
    )

    title: str = Field(min_length=1, max_length=200)
    objective: str = Field(min_length=1, max_length=10_000)
    priority: Priority = Priority.normal
    failure_policy: FailurePolicy = FailurePolicy.recover
    heartbeat_timeout_seconds: int = Field(
        default_factory=lambda: settings.default_heartbeat_timeout_seconds,
        ge=settings.min_heartbeat_timeout_seconds,
        le=settings.max_heartbeat_timeout_seconds,
    )
    max_recoveries: int = Field(default_factory=lambda: settings.default_max_recoveries, ge=0, le=50)
    tags: list[str] = Field(default_factory=list, max_length=20)
    metadata: dict[str, Any] = Field(default_factory=dict)
    initial_checkpoint: CheckpointBody | None = Field(
        default=None, description="Optional. Creates checkpoint version 1 in the same transaction."
    )


class WorkflowOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    objective: str
    status: WorkflowStatus
    priority: Priority
    failure_policy: FailurePolicy
    creator_agent_id: str
    current_agent_id: str | None
    heartbeat_timeout_seconds: int
    last_heartbeat_at: datetime
    heartbeat_age_seconds: float
    checkpoint_count: int
    current_checkpoint_version: int
    latest_checkpoint_id: uuid.UUID | None
    lease_generation: int
    recovery_count: int
    max_recoveries: int
    tags: list[str]
    metadata: dict[str, Any] = Field(validation_alias="meta", serialization_alias="metadata")
    created_at: datetime
    updated_at: datetime
    failed_at: datetime | None
    recovered_at: datetime | None
    completed_at: datetime | None


class CheckpointOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    workflow_id: uuid.UUID
    version: int
    parent_version: int | None
    objective: str
    completed_steps: list[str]
    remaining_steps: list[str]
    decisions: list[dict[str, Any]]
    next_action: str | None
    context_summary: str | None
    variables: dict[str, Any]
    producing_agent_id: str
    lease_generation: int
    schema_version: str
    content_checksum: str
    created_at: datetime


# --- Heartbeats / failure ----------------------------------------------------
class HeartbeatRequest(BaseModel):
    lease_token: str | None = Field(
        default=None, description="Required only while an active claim exists on the workflow."
    )
    note: str | None = Field(default=None, max_length=500)


class HeartbeatResponse(BaseModel):
    workflow_id: uuid.UUID
    status: WorkflowStatus
    last_heartbeat_at: datetime
    next_heartbeat_due_at: datetime
    heartbeat_timeout_seconds: int


class FailRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=2000, examples=["Upstream API returned 500 five times"])
    lease_token: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


# --- Claims ------------------------------------------------------------------
class ClaimRequest(BaseModel):
    lease_seconds: int = Field(
        default_factory=lambda: settings.default_lease_seconds,
        ge=settings.min_lease_seconds,
        le=settings.max_lease_seconds,
    )
    note: str | None = Field(default=None, max_length=500)
    acknowledge_blocking_issues: bool = Field(
        default=False,
        description=(
            "Must be true to claim a workflow whose context evaluation reports blocking "
            "issues. Forces the replacement agent to acknowledge it is resuming with "
            "incomplete context."
        ),
    )


class ClaimOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    workflow_id: uuid.UUID
    agent_id: str
    status: ClaimStatus
    lease_generation: int
    created_at: datetime
    expires_at: datetime
    last_renewed_at: datetime | None
    released_at: datetime | None
    release_reason: str | None
    renewal_count: int
    lease_token_prefix: str


class ClaimAcquiredOut(BaseModel):
    """Returned once, at claim time. ``lease_token`` is never shown again."""

    claim: ClaimOut
    lease_token: str = Field(
        description="Store securely. Required by every subsequent write to this workflow."
    )
    lease_expires_at: datetime
    lease_seconds: int
    fencing_token: int = Field(description="Same value as claim.lease_generation.")
    workflow: WorkflowOut


class RenewRequest(BaseModel):
    lease_token: str
    lease_seconds: int = Field(
        default_factory=lambda: settings.default_lease_seconds,
        ge=settings.min_lease_seconds,
        le=settings.max_lease_seconds,
    )


class ReleaseRequest(BaseModel):
    lease_token: str
    reason: str = Field(default="voluntary_release", max_length=255)


class ResumeRequest(BaseModel):
    lease_token: str
    note: str | None = Field(default=None, max_length=500)


class CompleteRequest(BaseModel):
    lease_token: str | None = Field(
        default=None, description="Required while an active claim exists."
    )
    final_checkpoint_version: int = Field(
        ge=1,
        description="Must equal the workflow's current_checkpoint_version. Guards against replays.",
    )
    summary: str | None = Field(default=None, max_length=10_000)


# --- Artifacts ---------------------------------------------------------------
class ArtifactCreate(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "name": "programs.json",
                "uri": "https://example.com/programs.json",
                "sha256": "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
                "content_type": "application/json",
                "description": "Five candidate programs with eligibility data",
                "verify": True,
            }
        }
    )

    name: str = Field(min_length=1, max_length=255)
    uri: str | None = Field(default=None, max_length=2000)
    storage_key: str | None = Field(default=None, max_length=1000)
    sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    content_type: str | None = Field(default=None, max_length=128)
    size_bytes: int | None = Field(default=None, ge=0)
    description: str | None = Field(default=None, max_length=2000)
    checkpoint_version: int | None = Field(default=None, ge=1)
    lease_token: str | None = None
    verify: bool = Field(
        default=False,
        description="Fetch the URI and check its SHA-256 now. Requires 'sha256' and 'uri'.",
    )

    @model_validator(mode="after")
    def _location_present(self) -> ArtifactCreate:
        if not self.uri and not self.storage_key:
            raise ValueError("An artifact needs either 'uri' or 'storage_key'.")
        if self.verify and not (self.uri and self.sha256):
            raise ValueError("'verify' requires both 'uri' and 'sha256'.")
        return self


class ArtifactOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    workflow_id: uuid.UUID
    name: str
    uri: str | None
    storage_key: str | None
    sha256: str | None
    content_type: str | None
    size_bytes: int | None
    description: str | None
    checkpoint_version: int | None
    verification_status: VerificationStatus
    verification_error: str | None
    verified_at: datetime | None
    produced_by_agent_id: str
    created_at: datetime


class UploadUrlRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    content_type: str | None = None


# --- Events ------------------------------------------------------------------
class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    workflow_id: uuid.UUID
    event_type: EventType
    actor_agent_id: str | None
    request_id: str | None
    checkpoint_version: int | None
    lease_generation: int | None
    metadata: dict[str, Any] = Field(validation_alias="meta", serialization_alias="metadata")
    created_at: datetime


# --- Context evaluation ------------------------------------------------------
class IssueOut(BaseModel):
    code: str
    severity: str
    message: str
    weight: int
    field: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class ContextEvaluationOut(BaseModel):
    resumable: bool
    score: int = Field(ge=0, le=100)
    blocking_issues: list[IssueOut]
    warnings: list[IssueOut]
    recommended_repairs: list[str]
    evaluated_checkpoint_version: int | None
    min_score_for_resume: int


class EvaluateContextRequest(BaseModel):
    checkpoint: CheckpointBody | None = Field(
        default=None,
        description=(
            "Optional draft checkpoint to evaluate before writing it. "
            "When omitted, the workflow's latest stored checkpoint is evaluated."
        ),
    )


# --- Recovery package --------------------------------------------------------
class ResumeInstructions(BaseModel):
    next_action: str | None
    must_preserve: list[str] = Field(description="Decisions and artifacts to carry forward.")
    must_not_repeat: list[str] = Field(description="Steps already completed. Do not redo these.")
    completion_requirements: list[str] = Field(
        description="Everything that must be true before POST /complete will succeed."
    )
    claim_first: bool = True
    expected_parent_version: int


class RecoveryPackage(BaseModel):
    workflow: WorkflowOut
    latest_checkpoint: CheckpointOut | None
    context_evaluation: ContextEvaluationOut
    artifacts: list[ArtifactOut]
    active_claim: ClaimOut | None
    resume_instructions: ResumeInstructions
    checkpoint_history: list[int] = Field(description="All stored checkpoint versions, ascending.")
    recent_events: list[EventOut]


# --- Pagination --------------------------------------------------------------
class Page(BaseModel, Generic[T]):
    items: list[T]
    next_cursor: str | None = Field(
        default=None, description="Pass back as ?cursor= to fetch the next page."
    )
    has_more: bool = False


class RecoverableWorkflowOut(BaseModel):
    workflow: WorkflowOut
    context_score: int
    resumable: bool
    blocking_issue_codes: list[str]
    seconds_since_recoverable: float
    latest_checkpoint_version: int


# --- Stats (dashboard) -------------------------------------------------------
class StatusCounts(BaseModel):
    active: int = 0
    suspected_failed: int = 0
    recoverable: int = 0
    claimed: int = 0
    completed: int = 0
    cancelled: int = 0
    dead_letter: int = 0


class StatsOut(BaseModel):
    status_counts: StatusCounts
    total_workflows: int
    expired_claims: int
    active_claims: int
    average_recovery_seconds: float | None
    context_score_distribution: dict[str, int]
    checkpoints_total: int
    events_total: int
    recent_events: list[EventOut]
    generated_at: datetime


# --- System ------------------------------------------------------------------
class HealthOut(BaseModel):
    status: str = "ok"
    service: str
    version: str
    environment: str
    time: datetime


class ReadyOut(BaseModel):
    status: str
    database: str
    migrations_applied: bool
    reaper_last_success: datetime | None
    time: datetime
