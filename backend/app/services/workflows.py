"""Workflow lifecycle: create, heartbeat, explicit failure, completion, cancellation."""

from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import metrics
from app.db import utcnow
from app.errors import (
    CompletionRequirementsNotMet,
    Forbidden,
    InvalidStateTransition,
    StaleCheckpointVersion,
    WorkflowAlreadyCompleted,
)
from app.models import (
    Artifact,
    Claim,
    ClaimStatus,
    EventType,
    FailurePolicy,
    VerificationStatus,
    Workflow,
    WorkflowStatus,
)
from app.schemas import WorkflowCreate
from app.services import checkpoints as checkpoint_service
from app.services import claims as claim_service
from app.services.events import record_event
from app.state_machine import TERMINAL, WRITABLE, transition


def create_workflow(
    db: Session,
    *,
    payload: WorkflowCreate,
    agent_id: str,
    idempotency_key: str | None,
    request_id: str | None = None,
) -> Workflow:
    workflow = Workflow(
        title=payload.title,
        objective=payload.objective,
        status=WorkflowStatus.active,
        priority=payload.priority,
        failure_policy=payload.failure_policy,
        creator_agent_id=agent_id,
        current_agent_id=agent_id,
        heartbeat_timeout_seconds=payload.heartbeat_timeout_seconds,
        last_heartbeat_at=utcnow(),
        max_recoveries=payload.max_recoveries,
        tags=list(payload.tags),
        meta=dict(payload.metadata),
        idempotency_key=idempotency_key,
    )
    db.add(workflow)
    db.flush()

    record_event(
        db,
        workflow_id=workflow.id,
        event_type=EventType.workflow_created,
        actor_agent_id=agent_id,
        request_id=request_id,
        metadata={"priority": payload.priority.value, "tags": list(payload.tags)},
    )
    metrics.workflows_created_total.inc()

    if payload.initial_checkpoint is not None:
        checkpoint_service.create_checkpoint(
            db,
            workflow=workflow,
            body=payload.initial_checkpoint,
            parent_version=0,
            agent_id=agent_id,
            claim=None,
            request_id=request_id,
        )
    return workflow


def heartbeat(
    db: Session,
    *,
    workflow_id: uuid.UUID,
    agent_id: str,
    lease_token: str | None,
    note: str | None = None,
    request_id: str | None = None,
) -> Workflow:
    workflow = claim_service.get_workflow_for_update(db, workflow_id)
    claim_service.authorize_write(db, workflow, agent_id=agent_id, lease_token=lease_token)

    if workflow.status in TERMINAL or workflow.status == WorkflowStatus.dead_letter:
        raise InvalidStateTransition(
            f"Cannot heartbeat a '{workflow.status.value}' workflow.",
            details={"current_status": workflow.status.value},
        )
    if workflow.status == WorkflowStatus.recoverable:
        raise InvalidStateTransition(
            "This workflow was released for recovery. Claim it before sending heartbeats.",
            details={"current_status": workflow.status.value},
        )

    now = utcnow()
    workflow.last_heartbeat_at = now
    revived = False
    if workflow.status == WorkflowStatus.suspected_failed:
        transition(workflow, WorkflowStatus.active, reason="heartbeat_after_suspicion")
        revived = True

    record_event(
        db,
        workflow_id=workflow.id,
        event_type=EventType.heartbeat_received,
        actor_agent_id=agent_id,
        request_id=request_id,
        checkpoint_version=workflow.current_checkpoint_version,
        metadata={"revived_from_suspected_failure": revived, "note": note},
    )
    metrics.heartbeats_total.inc()
    return workflow


def next_heartbeat_due(workflow: Workflow):
    return workflow.last_heartbeat_at + timedelta(seconds=workflow.heartbeat_timeout_seconds)


def report_failure(
    db: Session,
    *,
    workflow_id: uuid.UUID,
    agent_id: str,
    reason: str,
    lease_token: str | None,
    details: dict | None = None,
    request_id: str | None = None,
) -> Workflow:
    """An agent explicitly reports that it cannot continue."""
    workflow = claim_service.get_workflow_for_update(db, workflow_id)
    claim_service.authorize_write(db, workflow, agent_id=agent_id, lease_token=lease_token)

    if workflow.status not in WRITABLE:
        raise InvalidStateTransition(
            f"Cannot report failure from status '{workflow.status.value}'.",
            details={"current_status": workflow.status.value},
        )

    now = utcnow()
    workflow.failed_at = now
    record_event(
        db,
        workflow_id=workflow.id,
        event_type=EventType.explicit_failure_reported,
        actor_agent_id=agent_id,
        request_id=request_id,
        checkpoint_version=workflow.current_checkpoint_version,
        metadata={"reason": reason, "details": details or {}},
    )
    metrics.failures_detected_total.labels(reason="explicit").inc()

    _release_claims(db, workflow, reason="agent_reported_failure", request_id=request_id)
    _to_recoverable_or_dead_letter(db, workflow, trigger="explicit_failure", request_id=request_id)
    return workflow


def _release_claims(db: Session, workflow: Workflow, *, reason: str, request_id: str | None) -> None:
    active = claim_service.get_active_claim(db, workflow.id)
    if active is None:
        return
    now = utcnow()
    active.status = ClaimStatus.released
    active.released_at = now
    active.release_reason = reason
    record_event(
        db,
        workflow_id=workflow.id,
        event_type=EventType.claim_released,
        actor_agent_id=active.agent_id,
        request_id=request_id,
        lease_generation=active.lease_generation,
        metadata={"reason": reason},
    )
    metrics.claims_released_total.inc()


def _to_recoverable_or_dead_letter(
    db: Session, workflow: Workflow, *, trigger: str, request_id: str | None
) -> None:
    exhausted = workflow.recovery_count >= workflow.max_recoveries
    dead = workflow.failure_policy == FailurePolicy.dead_letter or exhausted

    if dead:
        transition(workflow, WorkflowStatus.dead_letter, reason=trigger)
        workflow.current_agent_id = None
        record_event(
            db,
            workflow_id=workflow.id,
            event_type=EventType.workflow_dead_lettered,
            request_id=request_id,
            metadata={
                "trigger": trigger,
                "failure_policy": workflow.failure_policy.value,
                "recovery_count": workflow.recovery_count,
                "max_recoveries": workflow.max_recoveries,
                "reason": "max_recoveries_exhausted" if exhausted else "failure_policy",
            },
        )
        metrics.dead_lettered_total.inc()
        return

    transition(workflow, WorkflowStatus.recoverable, reason=trigger)
    workflow.current_agent_id = None
    record_event(
        db,
        workflow_id=workflow.id,
        event_type=EventType.workflow_made_recoverable,
        request_id=request_id,
        checkpoint_version=workflow.current_checkpoint_version,
        metadata={"trigger": trigger},
    )
    metrics.workflows_made_recoverable_total.inc()


def completion_requirements(db: Session, workflow: Workflow) -> list[dict]:
    """The exact conditions POST /complete checks, as machine-readable records."""
    latest = checkpoint_service.latest_checkpoint(db, workflow.id)
    failed_artifacts = (
        db.execute(
            select(func.count(Artifact.id)).where(
                Artifact.workflow_id == workflow.id,
                Artifact.verification_status == VerificationStatus.failed,
            )
        ).scalar_one()
        or 0
    )
    return [
        {
            "requirement": "AT_LEAST_ONE_CHECKPOINT",
            "description": "The workflow must have at least one checkpoint.",
            "satisfied": latest is not None,
        },
        {
            "requirement": "NO_REMAINING_STEPS",
            "description": "The latest checkpoint must have an empty remaining_steps list.",
            "satisfied": latest is not None and not latest.remaining_steps,
            "remaining_steps": list(latest.remaining_steps) if latest is not None else [],
        },
        {
            "requirement": "NO_FAILED_ARTIFACTS",
            "description": "No artifact may be in verification_status 'failed'.",
            "satisfied": failed_artifacts == 0,
            "failed_artifact_count": failed_artifacts,
        },
        {
            "requirement": "FINAL_VERSION_MATCHES",
            "description": (
                "final_checkpoint_version in the request must equal the workflow's "
                "current_checkpoint_version."
            ),
            "satisfied": True,  # checked against the request body at completion time
            "current_checkpoint_version": workflow.current_checkpoint_version,
        },
    ]


def complete_workflow(
    db: Session,
    *,
    workflow_id: uuid.UUID,
    agent_id: str,
    lease_token: str | None,
    final_checkpoint_version: int,
    summary: str | None,
    request_id: str | None = None,
) -> Workflow:
    workflow = claim_service.get_workflow_for_update(db, workflow_id)

    if workflow.status == WorkflowStatus.completed:
        # Replay protection. A genuine retry should carry the original
        # Idempotency-Key and be answered from the idempotency store instead.
        raise WorkflowAlreadyCompleted(
            details={"completed_at": workflow.completed_at.isoformat() if workflow.completed_at else None}
        )

    claim = claim_service.authorize_write(db, workflow, agent_id=agent_id, lease_token=lease_token)

    if workflow.status not in {WorkflowStatus.active, WorkflowStatus.claimed}:
        raise InvalidStateTransition(
            f"Cannot complete a workflow in status '{workflow.status.value}'.",
            details={"current_status": workflow.status.value},
        )

    if final_checkpoint_version != workflow.current_checkpoint_version:
        metrics.stale_updates_rejected_total.labels(reason="stale_version").inc()
        raise StaleCheckpointVersion(
            "final_checkpoint_version does not match the workflow's current checkpoint version.",
            details={
                "your_final_checkpoint_version": final_checkpoint_version,
                "current_checkpoint_version": workflow.current_checkpoint_version,
            },
        )

    unmet = [r for r in completion_requirements(db, workflow) if not r["satisfied"]]
    if unmet:
        raise CompletionRequirementsNotMet(details={"unmet_requirements": unmet})

    now = utcnow()
    transition(workflow, WorkflowStatus.completed, reason="completed")
    workflow.completed_at = now
    workflow.current_agent_id = agent_id
    claim_service.close_claim_on_completion(db, workflow, request_id=request_id)

    record_event(
        db,
        workflow_id=workflow.id,
        event_type=EventType.workflow_completed,
        actor_agent_id=agent_id,
        request_id=request_id,
        checkpoint_version=workflow.current_checkpoint_version,
        lease_generation=claim.lease_generation if claim else workflow.lease_generation,
        metadata={"summary": summary, "recovery_count": workflow.recovery_count},
    )
    metrics.workflows_completed_total.inc()
    return workflow


def cancel_workflow(
    db: Session,
    *,
    workflow_id: uuid.UUID,
    agent_id: str,
    reason: str,
    is_admin: bool = False,
    request_id: str | None = None,
) -> Workflow:
    workflow = claim_service.get_workflow_for_update(db, workflow_id)
    if not is_admin and workflow.creator_agent_id != agent_id:
        raise Forbidden("Only the creating agent (or an admin key) may cancel a workflow.")

    _release_claims(db, workflow, reason="workflow_cancelled", request_id=request_id)
    transition(workflow, WorkflowStatus.cancelled, reason=reason)
    workflow.current_agent_id = None
    record_event(
        db,
        workflow_id=workflow.id,
        event_type=EventType.workflow_cancelled,
        actor_agent_id=agent_id,
        request_id=request_id,
        metadata={"reason": reason},
    )
    return workflow


def find_by_idempotency(db: Session, agent_id: str, key: str) -> Workflow | None:
    return db.execute(
        select(Workflow).where(
            Workflow.creator_agent_id == agent_id, Workflow.idempotency_key == key
        )
    ).scalar_one_or_none()


def active_claim_for(db: Session, workflow_id: uuid.UUID) -> Claim | None:
    return claim_service.get_active_claim(db, workflow_id)
