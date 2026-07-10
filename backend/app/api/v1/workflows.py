"""Workflow lifecycle endpoints."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Body, Query, Request, Response
from fastapi.responses import JSONResponse

from app.api.deps import CurrentAgent, DbSession, PageParams, RequestId
from app.api.serialize import dump, evaluation_out, workflow_out
from app.idempotency import IdempotencyGuard, key_from
from app.models import Priority, WorkflowStatus
from app.reaper import maybe_sweep
from app.schemas import (
    CompleteRequest,
    ContextEvaluationOut,
    EvaluateContextRequest,
    FailRequest,
    HeartbeatRequest,
    HeartbeatResponse,
    Page,
    WorkflowCreate,
    WorkflowOut,
)
from app.services import claims as claim_service
from app.services import recovery as recovery_service
from app.services import workflows as workflow_service

router = APIRouter(tags=["workflows"])


@router.post(
    "/workflows",
    status_code=201,
    response_model=WorkflowOut,
    summary="Create a workflow",
    description=(
        "Registers a unit of work and starts its heartbeat clock. Send an "
        "`Idempotency-Key` header to make retries safe. Optionally include "
        "`initial_checkpoint` to write version 1 in the same transaction."
    ),
)
def create_workflow(
    request: Request,
    payload: WorkflowCreate,
    agent: CurrentAgent,
    db: DbSession,
    request_id: RequestId,
) -> Response:
    guard = IdempotencyGuard(
        db,
        agent_id=agent.agent_id,
        endpoint=request.url.path,
        key=key_from(request),
        request_body=payload.model_dump(mode="json"),
    )
    replayed = guard.replay()
    if replayed is not None:
        return replayed

    workflow = workflow_service.create_workflow(
        db,
        payload=payload,
        agent_id=agent.agent_id,
        idempotency_key=guard.key,
        request_id=request_id,
    )
    return guard.commit(201, dump(workflow_out(workflow)))


@router.get(
    "/workflows",
    response_model=Page[WorkflowOut],
    summary="List workflows",
    description="Newest first. Cursor-paginated. Supports status, priority, tag, agent and text filters.",
)
def list_workflows(
    db: DbSession,
    agent: CurrentAgent,
    page: PageParams,
    status: Annotated[WorkflowStatus | None, Query()] = None,
    priority: Annotated[Priority | None, Query()] = None,
    tag: Annotated[str | None, Query(max_length=64)] = None,
    agent_id: Annotated[str | None, Query(max_length=128)] = None,
    search: Annotated[str | None, Query(max_length=200)] = None,
) -> Page[WorkflowOut]:
    rows, next_cursor, has_more = recovery_service.list_workflows(
        db,
        limit=page.limit,
        cursor=page.cursor,
        status=status,
        priority=priority,
        tag=tag,
        agent_id=agent_id,
        search=search,
    )
    return Page[WorkflowOut](
        items=[workflow_out(w) for w in rows], next_cursor=next_cursor, has_more=has_more
    )


@router.get("/workflows/{workflow_id}", response_model=WorkflowOut, summary="Get one workflow")
def get_workflow(workflow_id: uuid.UUID, db: DbSession, agent: CurrentAgent) -> WorkflowOut:
    return workflow_out(claim_service.get_workflow(db, workflow_id))


@router.post(
    "/workflows/{workflow_id}/heartbeats",
    response_model=HeartbeatResponse,
    summary="Report that the workflow is still being worked on",
    description=(
        "Resets the heartbeat clock. Call this at least twice per "
        "`heartbeat_timeout_seconds`. While an active claim exists you must send its "
        "`lease_token`."
    ),
)
def heartbeat(
    workflow_id: uuid.UUID,
    body: HeartbeatRequest,
    db: DbSession,
    agent: CurrentAgent,
    request_id: RequestId,
) -> HeartbeatResponse:
    workflow = workflow_service.heartbeat(
        db,
        workflow_id=workflow_id,
        agent_id=agent.agent_id,
        lease_token=body.lease_token,
        note=body.note,
        request_id=request_id,
    )
    db.commit()
    return HeartbeatResponse(
        workflow_id=workflow.id,
        status=workflow.status,
        last_heartbeat_at=workflow.last_heartbeat_at,
        next_heartbeat_due_at=workflow_service.next_heartbeat_due(workflow),
        heartbeat_timeout_seconds=workflow.heartbeat_timeout_seconds,
    )


@router.post(
    "/workflows/{workflow_id}/fail",
    response_model=WorkflowOut,
    summary="Report that this agent cannot continue",
    description=(
        "Marks the workflow failed immediately, without waiting for the heartbeat "
        "deadline. Depending on `failure_policy` and `max_recoveries` the workflow "
        "becomes `recoverable` or `dead_letter`. Any active claim is released."
    ),
)
def report_failure(
    request: Request,
    workflow_id: uuid.UUID,
    body: FailRequest,
    db: DbSession,
    agent: CurrentAgent,
    request_id: RequestId,
) -> Response:
    guard = IdempotencyGuard(
        db,
        agent_id=agent.agent_id,
        endpoint=request.url.path,
        key=key_from(request),
        request_body=body.model_dump(mode="json", exclude={"lease_token"}),
    )
    replayed = guard.replay()
    if replayed is not None:
        return replayed

    workflow = workflow_service.report_failure(
        db,
        workflow_id=workflow_id,
        agent_id=agent.agent_id,
        reason=body.reason,
        lease_token=body.lease_token,
        details=body.details,
        request_id=request_id,
    )
    return guard.commit(200, dump(workflow_out(workflow)))


@router.post(
    "/workflows/{workflow_id}/complete",
    response_model=WorkflowOut,
    summary="Complete the workflow",
    description=(
        "Succeeds only when every completion requirement is satisfied: at least one "
        "checkpoint exists, the latest checkpoint has no `remaining_steps`, no artifact "
        "has failed verification, and `final_checkpoint_version` matches the workflow's "
        "`current_checkpoint_version`. Send an `Idempotency-Key` so a retried completion "
        "replays instead of returning 409."
    ),
)
def complete_workflow(
    request: Request,
    workflow_id: uuid.UUID,
    payload: CompleteRequest,
    db: DbSession,
    agent: CurrentAgent,
    request_id: RequestId,
) -> Response:
    guard = IdempotencyGuard(
        db,
        agent_id=agent.agent_id,
        endpoint=request.url.path,
        key=key_from(request),
        request_body=payload.model_dump(mode="json", exclude={"lease_token"}),
    )
    replayed = guard.replay()
    if replayed is not None:
        return replayed

    workflow = workflow_service.complete_workflow(
        db,
        workflow_id=workflow_id,
        agent_id=agent.agent_id,
        lease_token=payload.lease_token,
        final_checkpoint_version=payload.final_checkpoint_version,
        summary=payload.summary,
        request_id=request_id,
    )
    return guard.commit(200, dump(workflow_out(workflow)))


@router.post(
    "/workflows/{workflow_id}/cancel",
    response_model=WorkflowOut,
    summary="Cancel a workflow (creator or admin only)",
)
def cancel_workflow(
    workflow_id: uuid.UUID,
    db: DbSession,
    agent: CurrentAgent,
    request_id: RequestId,
    reason: Annotated[str, Body(embed=True, max_length=500)] = "cancelled_by_agent",
) -> WorkflowOut:
    workflow = workflow_service.cancel_workflow(
        db,
        workflow_id=workflow_id,
        agent_id=agent.agent_id,
        reason=reason,
        is_admin=agent.is_admin,
        request_id=request_id,
    )
    db.commit()
    return workflow_out(workflow)


@router.post(
    "/workflows/{workflow_id}/evaluate-context",
    response_model=ContextEvaluationOut,
    summary="Check whether a checkpoint is complete enough to resume from",
    description=(
        "Deterministic. No LLM is called. With an empty body the workflow's latest "
        "checkpoint is evaluated; supply `checkpoint` to score a draft before writing it."
    ),
)
def evaluate_context(
    workflow_id: uuid.UUID,
    db: DbSession,
    agent: CurrentAgent,
    body: EvaluateContextRequest | None = None,
) -> ContextEvaluationOut:
    workflow = claim_service.get_workflow(db, workflow_id)
    draft = body.checkpoint if body else None
    evaluation = recovery_service.evaluate_workflow_context(db, workflow, draft=draft)
    return evaluation_out(evaluation.to_dict())


@router.get(
    "/workflows/{workflow_id}/recovery-package",
    summary="Everything a replacement agent needs to resume",
    description=(
        "One call returns the workflow, its latest checkpoint, the context evaluation, "
        "artifacts, the active claim (if any) and explicit resume instructions. "
        "Read this before claiming."
    ),
)
def recovery_package(workflow_id: uuid.UUID, db: DbSession, agent: CurrentAgent) -> JSONResponse:
    from app.api.serialize import artifact_out, checkpoint_out_opt, claim_out_opt, event_out

    workflow = claim_service.get_workflow(db, workflow_id)
    package = recovery_service.build_recovery_package(db, workflow)

    latest = checkpoint_out_opt(package["latest_checkpoint"])
    claim = claim_out_opt(package["active_claim"])
    body = {
        "workflow": dump(workflow_out(package["workflow"])),
        "latest_checkpoint": dump(latest) if latest else None,
        "context_evaluation": package["context_evaluation"],
        "artifacts": [dump(artifact_out(a)) for a in package["artifacts"]],
        "active_claim": dump(claim) if claim else None,
        "resume_instructions": package["resume_instructions"],
        "checkpoint_history": package["checkpoint_history"],
        "recent_events": [dump(event_out(e)) for e in package["recent_events"]],
    }
    return JSONResponse(body)


@router.get(
    "/recoverable-workflows",
    summary="Discover workflows waiting for a replacement agent",
    description=(
        "Ranked by priority, then by how long the work has been waiting. "
        "A failure-detection sweep runs before the query, so results are current even "
        "when no background worker is deployed."
    ),
)
def list_recoverable(
    db: DbSession,
    agent: CurrentAgent,
    page: PageParams,
    priority: Annotated[Priority | None, Query()] = None,
    tag: Annotated[str | None, Query(max_length=64)] = None,
    min_age_seconds: Annotated[int | None, Query(ge=0, le=31_536_000)] = None,
    resumable_only: Annotated[bool, Query(description="Hide workflows with blocking issues.")] = False,
) -> JSONResponse:
    maybe_sweep()

    items, next_cursor, has_more = recovery_service.list_recoverable(
        db,
        limit=page.limit,
        cursor=page.cursor,
        priority=priority,
        tag=tag,
        min_age_seconds=min_age_seconds,
        resumable_only=resumable_only,
    )
    return JSONResponse(
        {
            "items": [
                {
                    "workflow": dump(workflow_out(item["workflow"])),
                    "context_score": item["context_score"],
                    "resumable": item["resumable"],
                    "blocking_issue_codes": item["blocking_issue_codes"],
                    "seconds_since_recoverable": item["seconds_since_recoverable"],
                    "latest_checkpoint_version": item["latest_checkpoint_version"],
                }
                for item in items
            ],
            "next_cursor": next_cursor,
            "has_more": has_more,
        }
    )
