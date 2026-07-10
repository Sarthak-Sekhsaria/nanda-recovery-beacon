"""Checkpoint endpoints. Checkpoints are append-only and versioned."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Request, Response

from app.api.deps import CurrentAgent, DbSession, PageParams, RequestId
from app.api.serialize import checkpoint_out, dump
from app.idempotency import IdempotencyGuard, key_from
from app.schemas import CheckpointCreate, CheckpointOut, Page
from app.services import checkpoints as checkpoint_service
from app.services import claims as claim_service
from app.services import recovery as recovery_service

router = APIRouter(tags=["checkpoints"])


@router.post(
    "/workflows/{workflow_id}/checkpoints",
    status_code=201,
    response_model=CheckpointOut,
    summary="Append a new checkpoint version",
    description=(
        "Creates the next immutable version. `parent_version` must equal the workflow's "
        "`current_checkpoint_version`, otherwise the write is rejected with 409 "
        "STALE_CHECKPOINT_VERSION and the rejection is written to the audit log. "
        "While an active claim exists, `lease_token` is required."
    ),
)
def create_checkpoint(
    request: Request,
    workflow_id: uuid.UUID,
    payload: CheckpointCreate,
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

    workflow = claim_service.get_workflow_for_update(db, workflow_id)
    claim = claim_service.authorize_write(
        db, workflow, agent_id=agent.agent_id, lease_token=payload.lease_token
    )
    checkpoint = checkpoint_service.create_checkpoint(
        db,
        workflow=workflow,
        body=payload,
        parent_version=payload.parent_version,
        agent_id=agent.agent_id,
        claim=claim,
        request_id=request_id,
    )
    return guard.commit(201, dump(checkpoint_out(checkpoint)))


@router.get(
    "/workflows/{workflow_id}/checkpoints",
    response_model=Page[CheckpointOut],
    summary="List checkpoint versions, newest first",
)
def list_checkpoints(
    workflow_id: uuid.UUID, db: DbSession, agent: CurrentAgent, page: PageParams
) -> Page[CheckpointOut]:
    checkpoint_service.require_workflow(db, workflow_id)
    rows, next_cursor, has_more = recovery_service.list_checkpoints(
        db, workflow_id=workflow_id, limit=page.limit, cursor=page.cursor
    )
    return Page[CheckpointOut](
        items=[checkpoint_out(c) for c in rows], next_cursor=next_cursor, has_more=has_more
    )


@router.get(
    "/workflows/{workflow_id}/checkpoints/{version}",
    response_model=CheckpointOut,
    summary="Read one immutable checkpoint version",
)
def get_checkpoint(
    workflow_id: uuid.UUID, version: int, db: DbSession, agent: CurrentAgent
) -> CheckpointOut:
    checkpoint_service.require_workflow(db, workflow_id)
    return checkpoint_out(checkpoint_service.get_checkpoint(db, workflow_id, version))


@router.get(
    "/workflows/{workflow_id}/checkpoints/{version}/diff",
    summary="What changed between this version and its parent",
)
def checkpoint_diff(workflow_id: uuid.UUID, version: int, db: DbSession, agent: CurrentAgent) -> dict:
    checkpoint_service.require_workflow(db, workflow_id)
    current = checkpoint_service.get_checkpoint(db, workflow_id, version)
    previous = (
        checkpoint_service.get_checkpoint(db, workflow_id, version - 1) if version > 1 else None
    )
    return {
        "workflow_id": str(workflow_id),
        "version": version,
        "parent_version": current.parent_version,
        "diff": checkpoint_service.diff(previous, current),
    }
