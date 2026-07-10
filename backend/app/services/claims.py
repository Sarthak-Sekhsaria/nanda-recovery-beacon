"""Claim leasing, lease validation and fencing.

Correctness argument for "exactly one agent may hold a workflow":

1. ``acquire`` takes ``SELECT ... FOR UPDATE`` on the workflow row. Two concurrent
   claim requests are therefore serialised by PostgreSQL: the loser blocks until
   the winner commits, then re-reads the row under READ COMMITTED and observes
   ``status = 'claimed'``.
2. Independently of application logic, the partial unique index
   ``ux_claims_one_active_per_workflow`` makes a second ``active`` claim row
   impossible. An ``IntegrityError`` there is translated to 409 CLAIM_ALREADY_HELD.
3. Every write carries a lease token whose ``lease_generation`` must equal the
   workflow's current generation. A holder whose lease expired and whose workflow
   was re-claimed is rejected with 409 FENCING_TOKEN_STALE even if it presents a
   syntactically valid token. This is the fencing token from Kleppmann's
   "How to do distributed locking".
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import metrics
from app.db import utcnow
from app.errors import (
    ClaimAlreadyHeld,
    ClaimNotFound,
    DomainValidationError,
    FencingTokenStale,
    Forbidden,
    LeaseExpired,
    NotLeaseHolder,
    WorkflowNotFound,
    WorkflowNotRecoverable,
)
from app.models import Claim, ClaimStatus, EventType, Workflow, WorkflowStatus
from app.security import PREFIX_LENGTH, generate_lease_token, verify_secret
from app.services.events import record_event
from app.state_machine import transition


def get_workflow_for_update(db: Session, workflow_id: uuid.UUID) -> Workflow:
    workflow = db.execute(
        select(Workflow).where(Workflow.id == workflow_id).with_for_update()
    ).scalar_one_or_none()
    if workflow is None:
        raise WorkflowNotFound(details={"workflow_id": str(workflow_id)})
    return workflow


def get_workflow(db: Session, workflow_id: uuid.UUID) -> Workflow:
    workflow = db.get(Workflow, workflow_id)
    if workflow is None:
        raise WorkflowNotFound(details={"workflow_id": str(workflow_id)})
    return workflow


def get_active_claim(db: Session, workflow_id: uuid.UUID) -> Claim | None:
    return db.execute(
        select(Claim).where(Claim.workflow_id == workflow_id, Claim.status == ClaimStatus.active)
    ).scalar_one_or_none()


def find_claim_by_token(db: Session, workflow_id: uuid.UUID, lease_token: str) -> Claim | None:
    """Locate a claim from its raw token using a constant-time hash comparison."""
    if not lease_token:
        return None
    candidates = (
        db.execute(
            select(Claim).where(
                Claim.workflow_id == workflow_id,
                Claim.lease_token_prefix == lease_token[:PREFIX_LENGTH],
            )
        )
        .scalars()
        .all()
    )
    for candidate in candidates:
        if verify_secret(lease_token, candidate.lease_token_hash):
            return candidate
    return None


def expire_stale_claims_for_workflow(
    db: Session, workflow: Workflow, *, request_id: str | None = None
) -> int:
    """Expire any lease on ``workflow`` whose deadline has passed.

    Called before every claim attempt so an abandoned lease never blocks recovery,
    even if the background reaper is not running.
    """
    now = utcnow()
    stale = (
        db.execute(
            select(Claim).where(
                Claim.workflow_id == workflow.id,
                Claim.status == ClaimStatus.active,
                Claim.expires_at <= now,
            )
        )
        .scalars()
        .all()
    )
    for claim in stale:
        claim.status = ClaimStatus.expired
        claim.released_at = now
        claim.release_reason = "lease_expired"
        metrics.claims_expired_total.inc()
        record_event(
            db,
            workflow_id=workflow.id,
            event_type=EventType.claim_expired,
            actor_agent_id=claim.agent_id,
            request_id=request_id,
            lease_generation=claim.lease_generation,
            metadata={"expired_at": now.isoformat(), "claim_id": str(claim.id)},
        )

    if stale and workflow.status == WorkflowStatus.claimed:
        transition(workflow, WorkflowStatus.recoverable, reason="lease_expired")
        workflow.current_agent_id = None
        record_event(
            db,
            workflow_id=workflow.id,
            event_type=EventType.workflow_made_recoverable,
            request_id=request_id,
            metadata={"trigger": "lease_expired"},
        )
        metrics.workflows_made_recoverable_total.inc()
    return len(stale)


def acquire(
    db: Session,
    *,
    workflow_id: uuid.UUID,
    agent_id: str,
    lease_seconds: int,
    acknowledge_blocking_issues: bool,
    request_id: str | None = None,
    note: str | None = None,
) -> tuple[Claim, str, Workflow]:
    """Atomically take the lease. Returns ``(claim, raw_lease_token, workflow)``."""
    from app.services.recovery import evaluate_workflow_context  # local import: cycle

    workflow = get_workflow_for_update(db, workflow_id)
    expire_stale_claims_for_workflow(db, workflow, request_id=request_id)

    if workflow.status != WorkflowStatus.recoverable:
        existing = get_active_claim(db, workflow_id)
        if existing is not None:
            metrics.claim_conflicts_total.labels(reason="already_held").inc()
            raise ClaimAlreadyHeld(
                details={
                    "held_by_agent_id": existing.agent_id,
                    "expires_at": existing.expires_at.isoformat(),
                    "lease_generation": existing.lease_generation,
                },
                retry_after_seconds=max(1, int((existing.expires_at - utcnow()).total_seconds())),
            )
        metrics.claim_conflicts_total.labels(reason="not_recoverable").inc()
        raise WorkflowNotRecoverable(
            f"Workflow status is '{workflow.status.value}'. Only 'recoverable' workflows "
            "can be claimed.",
            details={"current_status": workflow.status.value},
        )

    evaluation = evaluate_workflow_context(db, workflow)
    if evaluation.blocking_issues and not acknowledge_blocking_issues:
        raise DomainValidationError(
            "This workflow has blocking context issues. Re-send with "
            "'acknowledge_blocking_issues': true if you still intend to resume it.",
            details={
                "code": "BLOCKING_CONTEXT_ISSUES",
                "score": evaluation.score,
                "blocking_issues": [i.to_dict() for i in evaluation.blocking_issues],
                "recommended_repairs": evaluation.recommended_repairs,
            },
        )

    raw_token, token_hash, token_prefix = generate_lease_token()
    now = utcnow()
    workflow.lease_generation += 1

    claim = Claim(
        workflow_id=workflow.id,
        agent_id=agent_id,
        lease_token_hash=token_hash,
        lease_token_prefix=token_prefix,
        lease_generation=workflow.lease_generation,
        status=ClaimStatus.active,
        expires_at=now + timedelta(seconds=lease_seconds),
    )
    db.add(claim)

    try:
        db.flush()
    except IntegrityError as exc:  # pragma: no cover - defence in depth
        db.rollback()
        metrics.claim_conflicts_total.labels(reason="unique_index").inc()
        raise ClaimAlreadyHeld() from exc

    transition(workflow, WorkflowStatus.claimed, reason="claim_acquired")
    workflow.current_agent_id = agent_id

    record_event(
        db,
        workflow_id=workflow.id,
        event_type=EventType.claim_acquired,
        actor_agent_id=agent_id,
        request_id=request_id,
        lease_generation=claim.lease_generation,
        checkpoint_version=workflow.current_checkpoint_version,
        metadata={
            "lease_seconds": lease_seconds,
            "expires_at": claim.expires_at.isoformat(),
            "context_score": evaluation.score,
            "acknowledged_blocking_issues": acknowledge_blocking_issues,
            "note": note,
        },
    )
    metrics.claims_acquired_total.inc()
    return claim, raw_token, workflow


def validate_lease(
    db: Session, workflow: Workflow, lease_token: str, *, agent_id: str | None = None
) -> Claim:
    """Validate a presented lease token against the workflow's fencing generation.

    Order matters: a superseded token yields 409 FENCING_TOKEN_STALE (someone else
    owns the work now) rather than the less specific 410 LEASE_EXPIRED.
    """
    claim = find_claim_by_token(db, workflow.id, lease_token)
    if claim is None:
        metrics.stale_updates_rejected_total.labels(reason="unknown_token").inc()
        raise NotLeaseHolder("The lease token is not valid for this workflow.")

    if claim.lease_generation != workflow.lease_generation:
        metrics.stale_updates_rejected_total.labels(reason="stale_fencing_token").inc()
        raise FencingTokenStale(
            details={
                "your_lease_generation": claim.lease_generation,
                "current_lease_generation": workflow.lease_generation,
                "current_agent_id": workflow.current_agent_id,
            }
        )

    if claim.status != ClaimStatus.active or claim.expires_at <= utcnow():
        metrics.stale_updates_rejected_total.labels(reason="expired_lease").inc()
        raise LeaseExpired(
            details={
                "claim_status": claim.status.value,
                "expired_at": claim.expires_at.isoformat(),
                "lease_generation": claim.lease_generation,
            }
        )

    if agent_id is not None and claim.agent_id != agent_id:
        raise NotLeaseHolder(
            "The lease token belongs to a different agent.",
            details={"lease_holder": claim.agent_id, "you": agent_id},
        )
    return claim


def authorize_write(
    db: Session, workflow: Workflow, *, agent_id: str, lease_token: str | None
) -> Claim | None:
    """Decide whether ``agent_id`` may write to ``workflow``.

    * No active claim -> the workflow's current agent may write.
    * Active claim    -> only the lease holder may write, and only with a valid,
      current-generation, unexpired token.
    """
    active = get_active_claim(db, workflow.id)

    if active is None:
        if lease_token:
            # A token was supplied but no claim is active: it is expired or superseded.
            return validate_lease(db, workflow, lease_token, agent_id=agent_id)
        if workflow.current_agent_id and workflow.current_agent_id != agent_id:
            raise Forbidden(
                "You are not the agent currently responsible for this workflow.",
                details={"current_agent_id": workflow.current_agent_id},
            )
        return None

    if not lease_token:
        raise NotLeaseHolder(
            details={
                "held_by_agent_id": active.agent_id,
                "lease_generation": active.lease_generation,
            }
        )
    return validate_lease(db, workflow, lease_token, agent_id=agent_id)


def renew(
    db: Session,
    *,
    workflow_id: uuid.UUID,
    agent_id: str,
    lease_token: str,
    lease_seconds: int,
    request_id: str | None = None,
) -> tuple[Claim, Workflow]:
    workflow = get_workflow_for_update(db, workflow_id)
    claim = validate_lease(db, workflow, lease_token, agent_id=agent_id)

    now = utcnow()
    claim.expires_at = now + timedelta(seconds=lease_seconds)
    claim.last_renewed_at = now
    claim.renewal_count += 1

    record_event(
        db,
        workflow_id=workflow.id,
        event_type=EventType.claim_renewed,
        actor_agent_id=agent_id,
        request_id=request_id,
        lease_generation=claim.lease_generation,
        metadata={"expires_at": claim.expires_at.isoformat(), "lease_seconds": lease_seconds},
    )
    metrics.claims_renewed_total.inc()
    return claim, workflow


def release(
    db: Session,
    *,
    workflow_id: uuid.UUID,
    agent_id: str,
    lease_token: str,
    reason: str,
    request_id: str | None = None,
) -> tuple[Claim, Workflow]:
    """Voluntarily give up the lease. The workflow returns to ``recoverable``."""
    workflow = get_workflow_for_update(db, workflow_id)
    claim = validate_lease(db, workflow, lease_token, agent_id=agent_id)

    now = utcnow()
    claim.status = ClaimStatus.released
    claim.released_at = now
    claim.release_reason = reason[:255]

    if workflow.status in {WorkflowStatus.claimed, WorkflowStatus.active}:
        transition(workflow, WorkflowStatus.recoverable, reason="claim_released")
        workflow.current_agent_id = None
        workflow.failed_at = workflow.failed_at or now
        record_event(
            db,
            workflow_id=workflow.id,
            event_type=EventType.workflow_made_recoverable,
            request_id=request_id,
            actor_agent_id=agent_id,
            metadata={"trigger": "claim_released"},
        )
        metrics.workflows_made_recoverable_total.inc()

    record_event(
        db,
        workflow_id=workflow.id,
        event_type=EventType.claim_released,
        actor_agent_id=agent_id,
        request_id=request_id,
        lease_generation=claim.lease_generation,
        metadata={"reason": reason},
    )
    metrics.claims_released_total.inc()
    return claim, workflow


def resume(
    db: Session,
    *,
    workflow_id: uuid.UUID,
    agent_id: str,
    lease_token: str,
    request_id: str | None = None,
    note: str | None = None,
) -> Workflow:
    """Move a claimed workflow back into ``active`` under the new agent."""
    workflow = get_workflow_for_update(db, workflow_id)
    claim = validate_lease(db, workflow, lease_token, agent_id=agent_id)

    if workflow.status != WorkflowStatus.claimed:
        raise WorkflowNotRecoverable(
            f"Only a 'claimed' workflow can be resumed; this one is '{workflow.status.value}'.",
            details={"current_status": workflow.status.value},
        )

    now = utcnow()
    if workflow.failed_at:
        metrics.recovery_seconds.observe((now - workflow.failed_at).total_seconds())

    transition(workflow, WorkflowStatus.active, reason="resumed")
    workflow.current_agent_id = agent_id
    workflow.last_heartbeat_at = now
    workflow.recovered_at = now
    workflow.recovery_count += 1

    record_event(
        db,
        workflow_id=workflow.id,
        event_type=EventType.workflow_resumed,
        actor_agent_id=agent_id,
        request_id=request_id,
        lease_generation=claim.lease_generation,
        checkpoint_version=workflow.current_checkpoint_version,
        metadata={"recovery_count": workflow.recovery_count, "note": note},
    )
    metrics.recoveries_total.inc()
    return workflow


def close_claim_on_completion(
    db: Session, workflow: Workflow, *, request_id: str | None = None
) -> None:
    now = utcnow()
    db.execute(
        update(Claim)
        .where(Claim.workflow_id == workflow.id, Claim.status == ClaimStatus.active)
        .values(status=ClaimStatus.completed, released_at=now, release_reason="workflow_completed")
    )


def require_active_claim(db: Session, workflow_id: uuid.UUID) -> Claim:
    claim = get_active_claim(db, workflow_id)
    if claim is None:
        raise ClaimNotFound(details={"workflow_id": str(workflow_id)})
    return claim


def seconds_remaining(claim: Claim, *, now: datetime | None = None) -> int:
    return max(0, int((claim.expires_at - (now or utcnow())).total_seconds()))
