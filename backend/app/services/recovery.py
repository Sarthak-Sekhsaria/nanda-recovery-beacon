"""Recovery package assembly, recoverable-work discovery and dashboard statistics."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import Select, case, desc, func, literal, select, tuple_
from sqlalchemy.orm import Session

from app import metrics
from app.context_eval import ContextEvaluation, evaluate_context
from app.db import utcnow
from app.models import (
    PRIORITY_RANK,
    Artifact,
    Checkpoint,
    Claim,
    ClaimStatus,
    Priority,
    RecoveryEvent,
    Workflow,
    WorkflowStatus,
)
from app.schemas import CheckpointBody
from app.services import checkpoints as checkpoint_service
from app.services import claims as claim_service
from app.services import workflows as workflow_service
from app.util import decode_cursor, encode_cursor, parse_datetime, parse_uuid

PRIORITY_CASE = case(
    (Workflow.priority == Priority.critical, 4),
    (Workflow.priority == Priority.high, 3),
    (Workflow.priority == Priority.normal, 2),
    else_=1,
)


def workflow_artifacts(db: Session, workflow_id: uuid.UUID) -> list[Artifact]:
    return list(
        db.execute(
            select(Artifact)
            .where(Artifact.workflow_id == workflow_id)
            .order_by(Artifact.created_at.asc())
        )
        .scalars()
        .all()
    )


def evaluate_workflow_context(
    db: Session, workflow: Workflow, *, draft: CheckpointBody | None = None
) -> ContextEvaluation:
    """Evaluate the latest stored checkpoint, or a draft the agent has not written yet."""
    artifacts = workflow_artifacts(db, workflow.id)

    if draft is not None:
        pseudo = Checkpoint(
            workflow_id=workflow.id,
            version=workflow.current_checkpoint_version + 1,
            parent_version=workflow.current_checkpoint_version or None,
            objective=draft.objective,
            completed_steps=list(draft.completed_steps),
            remaining_steps=list(draft.remaining_steps),
            decisions=[d.model_dump(mode="json") for d in draft.decisions],
            next_action=draft.next_action,
            context_summary=draft.context_summary,
            variables=dict(draft.variables),
            producing_agent_id="draft",
            schema_version=draft.schema_version,
            content_checksum="",
        )
        evaluation = evaluate_context(workflow, pseudo, artifacts)
    else:
        latest = checkpoint_service.latest_checkpoint(db, workflow.id)
        evaluation = evaluate_context(workflow, latest, artifacts)

    metrics.context_score.observe(evaluation.score)
    return evaluation


def build_recovery_package(db: Session, workflow: Workflow) -> dict[str, Any]:
    """Everything a replacement agent needs, in one response."""
    latest = checkpoint_service.latest_checkpoint(db, workflow.id)
    artifacts = workflow_artifacts(db, workflow.id)
    evaluation = evaluate_context(workflow, latest, artifacts)
    active_claim = claim_service.get_active_claim(db, workflow.id)
    requirements = workflow_service.completion_requirements(db, workflow)

    must_preserve: list[str] = []
    if latest:
        for decision in latest.decisions:
            if isinstance(decision, dict) and decision.get("decision"):
                reason = decision.get("reason")
                must_preserve.append(
                    f"Decision: {decision['decision']}" + (f" (reason: {reason})" if reason else "")
                )
    for artifact in artifacts:
        location = artifact.uri or f"storage_key:{artifact.storage_key}"
        must_preserve.append(f"Artifact '{artifact.name}' at {location}")

    recent_events = list(
        db.execute(
            select(RecoveryEvent)
            .where(RecoveryEvent.workflow_id == workflow.id)
            .order_by(RecoveryEvent.created_at.desc())
            .limit(20)
        )
        .scalars()
        .all()
    )

    return {
        "workflow": workflow,
        "latest_checkpoint": latest,
        "context_evaluation": evaluation.to_dict(),
        "artifacts": artifacts,
        "active_claim": active_claim,
        "resume_instructions": {
            "next_action": latest.next_action if latest else None,
            "must_preserve": must_preserve,
            "must_not_repeat": list(latest.completed_steps) if latest else [],
            "completion_requirements": [
                f"{r['requirement']}: {r['description']}" for r in requirements
            ],
            "claim_first": workflow.status == WorkflowStatus.recoverable,
            "expected_parent_version": workflow.current_checkpoint_version,
        },
        "checkpoint_history": checkpoint_service.list_versions(db, workflow.id),
        "recent_events": recent_events,
    }


# --- Discovery ---------------------------------------------------------------
def list_recoverable(
    db: Session,
    *,
    limit: int,
    cursor: str | None = None,
    priority: Priority | None = None,
    tag: str | None = None,
    min_age_seconds: int | None = None,
    resumable_only: bool = False,
) -> tuple[list[dict[str, Any]], str | None, bool]:
    """Recoverable workflows ranked by priority, then by how long they have waited.

    Keyset pagination over ``(-priority_rank, failed_at, id)`` -- a stable total
    order, so a workflow can never be skipped or shown twice across pages.
    """
    now = utcnow()
    stmt: Select = select(Workflow).where(Workflow.status == WorkflowStatus.recoverable)

    if priority is not None:
        stmt = stmt.where(Workflow.priority == priority)
    if tag:
        stmt = stmt.where(Workflow.tags.contains([tag]))
    if min_age_seconds:
        stmt = stmt.where(
            func.coalesce(Workflow.failed_at, Workflow.updated_at)
            <= func.now() - func.make_interval(0, 0, 0, 0, 0, 0, min_age_seconds)
        )

    order_rank = (-PRIORITY_CASE).label("neg_rank")
    order_time = func.coalesce(Workflow.failed_at, Workflow.created_at).label("wait_since")

    if cursor:
        rank, wait_since, last_id = decode_cursor(cursor)
        stmt = stmt.where(
            tuple_(order_rank, order_time, Workflow.id)
            > tuple_(literal(int(rank)), literal(parse_datetime(wait_since)), literal(parse_uuid(last_id)))
        )

    stmt = stmt.order_by(order_rank.asc(), order_time.asc(), Workflow.id.asc()).limit(limit + 1)
    rows = list(db.execute(stmt).scalars().all())

    has_more = len(rows) > limit
    rows = rows[:limit]

    items: list[dict[str, Any]] = []
    for workflow in rows:
        latest = checkpoint_service.latest_checkpoint(db, workflow.id)
        evaluation = evaluate_context(workflow, latest, workflow_artifacts(db, workflow.id))
        if resumable_only and not evaluation.resumable:
            continue
        waited_since = workflow.failed_at or workflow.created_at
        items.append(
            {
                "workflow": workflow,
                "context_score": evaluation.score,
                "resumable": evaluation.resumable,
                "blocking_issue_codes": [i.code for i in evaluation.blocking_issues],
                "seconds_since_recoverable": max(0.0, (now - waited_since).total_seconds()),
                "latest_checkpoint_version": latest.version if latest else 0,
            }
        )

    next_cursor = None
    if has_more and rows:
        last = rows[-1]
        next_cursor = encode_cursor(
            [
                -PRIORITY_RANK[last.priority.value],
                (last.failed_at or last.created_at).isoformat(),
                str(last.id),
            ]
        )
    return items, next_cursor, has_more


def list_workflows(
    db: Session,
    *,
    limit: int,
    cursor: str | None = None,
    status: WorkflowStatus | None = None,
    priority: Priority | None = None,
    tag: str | None = None,
    agent_id: str | None = None,
    search: str | None = None,
) -> tuple[list[Workflow], str | None, bool]:
    """Newest first, keyset paginated on ``(created_at, id)``."""
    stmt: Select = select(Workflow)
    if status is not None:
        stmt = stmt.where(Workflow.status == status)
    if priority is not None:
        stmt = stmt.where(Workflow.priority == priority)
    if tag:
        stmt = stmt.where(Workflow.tags.contains([tag]))
    if agent_id:
        stmt = stmt.where(
            (Workflow.creator_agent_id == agent_id) | (Workflow.current_agent_id == agent_id)
        )
    if search:
        pattern = f"%{search.lower()}%"
        stmt = stmt.where(
            func.lower(Workflow.title).like(pattern) | func.lower(Workflow.objective).like(pattern)
        )
    if cursor:
        created_at, last_id = decode_cursor(cursor)
        stmt = stmt.where(
            tuple_(Workflow.created_at, Workflow.id)
            < tuple_(literal(parse_datetime(created_at)), literal(parse_uuid(last_id)))
        )

    stmt = stmt.order_by(desc(Workflow.created_at), desc(Workflow.id)).limit(limit + 1)
    rows = list(db.execute(stmt).scalars().all())
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = (
        encode_cursor([rows[-1].created_at.isoformat(), str(rows[-1].id)])
        if has_more and rows
        else None
    )
    return rows, next_cursor, has_more


def list_events(
    db: Session, *, workflow_id: uuid.UUID | None, limit: int, cursor: str | None = None
) -> tuple[list[RecoveryEvent], str | None, bool]:
    stmt: Select = select(RecoveryEvent)
    if workflow_id is not None:
        stmt = stmt.where(RecoveryEvent.workflow_id == workflow_id)
    if cursor:
        created_at, last_id = decode_cursor(cursor)
        stmt = stmt.where(
            tuple_(RecoveryEvent.created_at, RecoveryEvent.id)
            < tuple_(literal(parse_datetime(created_at)), literal(parse_uuid(last_id)))
        )
    stmt = stmt.order_by(desc(RecoveryEvent.created_at), desc(RecoveryEvent.id)).limit(limit + 1)
    rows = list(db.execute(stmt).scalars().all())
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = (
        encode_cursor([rows[-1].created_at.isoformat(), str(rows[-1].id)])
        if has_more and rows
        else None
    )
    return rows, next_cursor, has_more


def list_checkpoints(
    db: Session, *, workflow_id: uuid.UUID, limit: int, cursor: str | None = None
) -> tuple[list[Checkpoint], str | None, bool]:
    stmt: Select = select(Checkpoint).where(Checkpoint.workflow_id == workflow_id)
    if cursor:
        (last_version,) = decode_cursor(cursor)
        stmt = stmt.where(Checkpoint.version < int(last_version))
    stmt = stmt.order_by(desc(Checkpoint.version)).limit(limit + 1)
    rows = list(db.execute(stmt).scalars().all())
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = encode_cursor([rows[-1].version]) if has_more and rows else None
    return rows, next_cursor, has_more


# --- Dashboard stats ---------------------------------------------------------
SCORE_BUCKETS = ["0-19", "20-39", "40-59", "60-79", "80-100"]


def _bucket(score: int) -> str:
    if score < 20:
        return "0-19"
    if score < 40:
        return "20-39"
    if score < 60:
        return "40-59"
    if score < 80:
        return "60-79"
    return "80-100"


def compute_stats(db: Session, *, sample_size: int = 200) -> dict[str, Any]:
    counts_rows = db.execute(
        select(Workflow.status, func.count(Workflow.id)).group_by(Workflow.status)
    ).all()
    status_counts = {status.value: 0 for status in WorkflowStatus}
    for status, count in counts_rows:
        status_counts[status.value] = count

    expired_claims = (
        db.execute(
            select(func.count(Claim.id)).where(Claim.status == ClaimStatus.expired)
        ).scalar_one()
        or 0
    )
    active_claims = (
        db.execute(
            select(func.count(Claim.id)).where(Claim.status == ClaimStatus.active)
        ).scalar_one()
        or 0
    )

    avg_recovery = db.execute(
        select(
            func.avg(
                func.extract("epoch", Workflow.recovered_at)
                - func.extract("epoch", Workflow.failed_at)
            )
        ).where(Workflow.recovered_at.is_not(None), Workflow.failed_at.is_not(None))
    ).scalar_one()

    sample = list(
        db.execute(
            select(Workflow)
            .where(Workflow.status.not_in([WorkflowStatus.cancelled]))
            .order_by(desc(Workflow.updated_at))
            .limit(sample_size)
        )
        .scalars()
        .all()
    )
    distribution = dict.fromkeys(SCORE_BUCKETS, 0)
    for workflow in sample:
        latest = checkpoint_service.latest_checkpoint(db, workflow.id)
        evaluation = evaluate_context(workflow, latest, workflow_artifacts(db, workflow.id))
        distribution[_bucket(evaluation.score)] += 1

    recent_events = list(
        db.execute(select(RecoveryEvent).order_by(desc(RecoveryEvent.created_at)).limit(25))
        .scalars()
        .all()
    )

    return {
        "status_counts": status_counts,
        "total_workflows": sum(status_counts.values()),
        "expired_claims": expired_claims,
        "active_claims": active_claims,
        "average_recovery_seconds": float(avg_recovery) if avg_recovery is not None else None,
        "context_score_distribution": distribution,
        "checkpoints_total": db.execute(select(func.count(Checkpoint.id))).scalar_one() or 0,
        "events_total": db.execute(select(func.count(RecoveryEvent.id))).scalar_one() or 0,
        "recent_events": recent_events,
        "generated_at": utcnow(),
    }
