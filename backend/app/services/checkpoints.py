"""Immutable, versioned checkpoint writes.

Concurrency model: optimistic. The writer states the version it read
(``parent_version``); the service accepts the write only if that is still the
workflow's ``current_checkpoint_version``. Anything else is a lost update and is
rejected with 409 STALE_CHECKPOINT_VERSION.

Immutability is enforced by the ``trg_checkpoints_append_only`` database trigger,
not merely by the absence of an UPDATE endpoint.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import metrics
from app.config import settings
from app.db import utcnow
from app.errors import (
    CheckpointNotFound,
    StaleCheckpointVersion,
    UnsupportedSchemaVersion,
    WorkflowNotFound,
)
from app.models import Checkpoint, Claim, EventType, Workflow, WorkflowStatus
from app.schemas import CheckpointBody
from app.services.events import record_event
from app.state_machine import WRITABLE
from app.util import checksum


def _content_checksum(body: CheckpointBody, version: int) -> str:
    return checksum(
        {
            "version": version,
            "objective": body.objective,
            "completed_steps": body.completed_steps,
            "remaining_steps": body.remaining_steps,
            "decisions": [d.model_dump(mode="json") for d in body.decisions],
            "next_action": body.next_action,
            "context_summary": body.context_summary,
            "variables": body.variables,
            "schema_version": body.schema_version,
        }
    )


def latest_checkpoint(db: Session, workflow_id: uuid.UUID) -> Checkpoint | None:
    return db.execute(
        select(Checkpoint)
        .where(Checkpoint.workflow_id == workflow_id)
        .order_by(Checkpoint.version.desc())
        .limit(1)
    ).scalar_one_or_none()


def get_checkpoint(db: Session, workflow_id: uuid.UUID, version: int) -> Checkpoint:
    checkpoint = db.execute(
        select(Checkpoint).where(
            Checkpoint.workflow_id == workflow_id, Checkpoint.version == version
        )
    ).scalar_one_or_none()
    if checkpoint is None:
        raise CheckpointNotFound(details={"workflow_id": str(workflow_id), "version": version})
    return checkpoint


def create_checkpoint(
    db: Session,
    *,
    workflow: Workflow,
    body: CheckpointBody,
    parent_version: int,
    agent_id: str,
    claim: Claim | None,
    request_id: str | None = None,
) -> Checkpoint:
    """Append a new checkpoint version. ``workflow`` must already be row-locked."""
    if body.schema_version not in settings.supported_checkpoint_schema_versions:
        raise UnsupportedSchemaVersion(
            details={
                "found": body.schema_version,
                "supported": settings.supported_checkpoint_schema_versions,
            }
        )

    if workflow.status not in WRITABLE:
        from app.errors import InvalidStateTransition

        raise InvalidStateTransition(
            f"Checkpoints cannot be written while the workflow is '{workflow.status.value}'.",
            details={
                "current_status": workflow.status.value,
                "writable_statuses": sorted(s.value for s in WRITABLE),
            },
        )

    if parent_version != workflow.current_checkpoint_version:
        metrics.stale_updates_rejected_total.labels(reason="stale_version").inc()
        record_event(
            db,
            workflow_id=workflow.id,
            event_type=EventType.stale_update_rejected,
            actor_agent_id=agent_id,
            request_id=request_id,
            checkpoint_version=workflow.current_checkpoint_version,
            metadata={"submitted_parent_version": parent_version, "reason": "stale_version"},
        )
        db.commit()  # persist the audit record even though the write is rejected
        raise StaleCheckpointVersion(
            details={
                "your_parent_version": parent_version,
                "current_checkpoint_version": workflow.current_checkpoint_version,
                "hint": "GET the workflow, read current_checkpoint_version, retry.",
            }
        )

    version = workflow.current_checkpoint_version + 1
    checkpoint = Checkpoint(
        workflow_id=workflow.id,
        version=version,
        parent_version=parent_version or None,
        objective=body.objective,
        completed_steps=list(body.completed_steps),
        remaining_steps=list(body.remaining_steps),
        decisions=[d.model_dump(mode="json") for d in body.decisions],
        next_action=body.next_action,
        context_summary=body.context_summary,
        variables=dict(body.variables),
        producing_agent_id=agent_id,
        lease_generation=claim.lease_generation if claim else workflow.lease_generation,
        schema_version=body.schema_version,
        content_checksum=_content_checksum(body, version),
    )
    db.add(checkpoint)
    db.flush()

    workflow.current_checkpoint_version = version
    workflow.checkpoint_count += 1
    workflow.latest_checkpoint_id = checkpoint.id
    workflow.last_heartbeat_at = utcnow()
    if workflow.status == WorkflowStatus.suspected_failed:
        # A checkpoint is proof of life.
        workflow.status = WorkflowStatus.active

    record_event(
        db,
        workflow_id=workflow.id,
        event_type=EventType.checkpoint_created,
        actor_agent_id=agent_id,
        request_id=request_id,
        checkpoint_version=version,
        lease_generation=checkpoint.lease_generation,
        metadata={
            "completed_steps": len(checkpoint.completed_steps),
            "remaining_steps": len(checkpoint.remaining_steps),
            "content_checksum": checkpoint.content_checksum,
        },
    )
    metrics.checkpoints_created_total.inc()
    return checkpoint


def list_versions(db: Session, workflow_id: uuid.UUID) -> list[int]:
    return list(
        db.execute(
            select(Checkpoint.version)
            .where(Checkpoint.workflow_id == workflow_id)
            .order_by(Checkpoint.version.asc())
        )
        .scalars()
        .all()
    )


def diff(previous: Checkpoint | None, current: Checkpoint) -> dict:
    """Field-level difference between consecutive versions, for the dashboard."""
    if previous is None:
        return {
            "steps_completed_since_parent": list(current.completed_steps),
            "steps_removed_from_remaining": [],
            "decisions_added": list(current.decisions),
            "next_action_changed": True,
            "objective_changed": True,
        }
    prev_completed = {str(s) for s in previous.completed_steps}
    prev_remaining = {str(s) for s in previous.remaining_steps}
    curr_remaining = {str(s) for s in current.remaining_steps}
    prev_decisions = {checksum(d) for d in previous.decisions}
    return {
        "steps_completed_since_parent": [
            s for s in current.completed_steps if str(s) not in prev_completed
        ],
        "steps_removed_from_remaining": sorted(prev_remaining - curr_remaining),
        "decisions_added": [d for d in current.decisions if checksum(d) not in prev_decisions],
        "next_action_changed": previous.next_action != current.next_action,
        "objective_changed": previous.objective != current.objective,
    }


def require_workflow(db: Session, workflow_id: uuid.UUID) -> Workflow:
    workflow = db.get(Workflow, workflow_id)
    if workflow is None:
        raise WorkflowNotFound(details={"workflow_id": str(workflow_id)})
    return workflow
