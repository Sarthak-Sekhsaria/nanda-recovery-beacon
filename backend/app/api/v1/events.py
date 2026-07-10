"""Audit trail and dashboard statistics."""

from __future__ import annotations

import uuid

from fastapi import APIRouter

from app.api.deps import AdminAgent, CurrentAgent, DbSession, PageParams
from app.api.serialize import event_out
from app.reaper import run_sweep
from app.schemas import EventOut, Page, StatsOut, StatusCounts
from app.services import claims as claim_service
from app.services import recovery as recovery_service

router = APIRouter(tags=["audit"])


@router.get(
    "/workflows/{workflow_id}/events",
    response_model=Page[EventOut],
    summary="Append-only audit trail for one workflow",
    description="Newest first. Every claim, checkpoint, failure and rejection is recorded here.",
)
def workflow_events(
    workflow_id: uuid.UUID, db: DbSession, agent: CurrentAgent, page: PageParams
) -> Page[EventOut]:
    claim_service.get_workflow(db, workflow_id)
    rows, next_cursor, has_more = recovery_service.list_events(
        db, workflow_id=workflow_id, limit=page.limit, cursor=page.cursor
    )
    return Page[EventOut](
        items=[event_out(e) for e in rows], next_cursor=next_cursor, has_more=has_more
    )


@router.get(
    "/events",
    response_model=Page[EventOut],
    summary="Recent events across every workflow",
)
def all_events(db: DbSession, agent: CurrentAgent, page: PageParams) -> Page[EventOut]:
    rows, next_cursor, has_more = recovery_service.list_events(
        db, workflow_id=None, limit=page.limit, cursor=page.cursor
    )
    return Page[EventOut](
        items=[event_out(e) for e in rows], next_cursor=next_cursor, has_more=has_more
    )


@router.get(
    "/stats",
    response_model=StatsOut,
    summary="Aggregate counters that power the operations dashboard",
)
def stats(db: DbSession, agent: CurrentAgent) -> StatsOut:
    raw = recovery_service.compute_stats(db)
    return StatsOut(
        status_counts=StatusCounts(**raw["status_counts"]),
        total_workflows=raw["total_workflows"],
        expired_claims=raw["expired_claims"],
        active_claims=raw["active_claims"],
        average_recovery_seconds=raw["average_recovery_seconds"],
        context_score_distribution=raw["context_score_distribution"],
        checkpoints_total=raw["checkpoints_total"],
        events_total=raw["events_total"],
        recent_events=[event_out(e) for e in raw["recent_events"]],
        generated_at=raw["generated_at"],
    )


@router.post(
    "/admin/reap",
    summary="Run one failure-detection sweep now (admin key required)",
    description=(
        "The same sweep the background worker runs. Exposed so operators can force a "
        "pass without redeploying. Guarded by a PostgreSQL advisory lock, so calling it "
        "while the worker is running is safe."
    ),
)
def force_sweep(agent: AdminAgent) -> dict:
    return run_sweep().as_dict()
