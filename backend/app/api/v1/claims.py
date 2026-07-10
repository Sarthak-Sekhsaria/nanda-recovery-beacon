"""Claim (lease) endpoints. Exactly one agent may hold a workflow at a time."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Request, Response

from app.api.deps import CurrentAgent, DbSession, RequestId
from app.api.serialize import claim_out, dump, workflow_out
from app.idempotency import IdempotencyGuard, key_from
from app.reaper import maybe_sweep
from app.schemas import (
    ClaimAcquiredOut,
    ClaimOut,
    ClaimRequest,
    ReleaseRequest,
    RenewRequest,
    ResumeRequest,
    WorkflowOut,
)
from app.services import claims as claim_service

router = APIRouter(tags=["claims"])


@router.post(
    "/workflows/{workflow_id}/claims",
    status_code=201,
    response_model=ClaimAcquiredOut,
    summary="Claim a recoverable workflow",
    description=(
        "Takes an exclusive, time-boxed lease. The workflow must be `recoverable`. "
        "Two agents racing here produce exactly one 201 and one 409 CLAIM_ALREADY_HELD.\n\n"
        "The response contains `lease_token` **once**. Store it securely; every "
        "subsequent write to this workflow must present it. If the checkpoint has "
        "blocking context issues you must re-send with `acknowledge_blocking_issues: true`."
    ),
    responses={
        409: {"description": "CLAIM_ALREADY_HELD or WORKFLOW_NOT_RECOVERABLE"},
        422: {"description": "BLOCKING_CONTEXT_ISSUES (not acknowledged)"},
    },
)
def acquire_claim(
    request: Request,
    workflow_id: uuid.UUID,
    payload: ClaimRequest,
    db: DbSession,
    agent: CurrentAgent,
    request_id: RequestId,
) -> Response:
    maybe_sweep()

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

    claim, lease_token, workflow = claim_service.acquire(
        db,
        workflow_id=workflow_id,
        agent_id=agent.agent_id,
        lease_seconds=payload.lease_seconds,
        acknowledge_blocking_issues=payload.acknowledge_blocking_issues,
        request_id=request_id,
        note=payload.note,
    )
    body = dump(
        ClaimAcquiredOut(
            claim=claim_out(claim),
            lease_token=lease_token,
            lease_expires_at=claim.expires_at,
            lease_seconds=payload.lease_seconds,
            fencing_token=claim.lease_generation,
            workflow=workflow_out(workflow),
        )
    )
    return guard.commit(201, body)


@router.post(
    "/workflows/{workflow_id}/claims/renew",
    response_model=ClaimOut,
    summary="Extend the lease before it expires",
    description=(
        "Call this at roughly half the lease duration. A lease that has already expired "
        "cannot be renewed: you get 410 LEASE_EXPIRED and must claim again. If another "
        "agent claimed the workflow after your lease lapsed you get 409 FENCING_TOKEN_STALE."
    ),
)
def renew_claim(
    workflow_id: uuid.UUID,
    payload: RenewRequest,
    db: DbSession,
    agent: CurrentAgent,
    request_id: RequestId,
) -> ClaimOut:
    claim, _ = claim_service.renew(
        db,
        workflow_id=workflow_id,
        agent_id=agent.agent_id,
        lease_token=payload.lease_token,
        lease_seconds=payload.lease_seconds,
        request_id=request_id,
    )
    db.commit()
    return claim_out(claim)


@router.post(
    "/workflows/{workflow_id}/claims/release",
    response_model=ClaimOut,
    summary="Give the workflow back",
    description=(
        "Call this when you cannot continue but the work is still valid. The workflow "
        "returns to `recoverable` immediately instead of waiting for the lease to lapse."
    ),
)
def release_claim(
    workflow_id: uuid.UUID,
    payload: ReleaseRequest,
    db: DbSession,
    agent: CurrentAgent,
    request_id: RequestId,
) -> ClaimOut:
    claim, _ = claim_service.release(
        db,
        workflow_id=workflow_id,
        agent_id=agent.agent_id,
        lease_token=payload.lease_token,
        reason=payload.reason,
        request_id=request_id,
    )
    db.commit()
    return claim_out(claim)


@router.post(
    "/workflows/{workflow_id}/resume",
    response_model=WorkflowOut,
    summary="Begin working on a claimed workflow",
    description=(
        "Moves the workflow from `claimed` to `active` with you as the current agent, and "
        "restarts the heartbeat clock. Keep the same lease token; keep renewing it."
    ),
)
def resume_workflow(
    workflow_id: uuid.UUID,
    payload: ResumeRequest,
    db: DbSession,
    agent: CurrentAgent,
    request_id: RequestId,
) -> WorkflowOut:
    workflow = claim_service.resume(
        db,
        workflow_id=workflow_id,
        agent_id=agent.agent_id,
        lease_token=payload.lease_token,
        request_id=request_id,
        note=payload.note,
    )
    db.commit()
    return workflow_out(workflow)


@router.get(
    "/workflows/{workflow_id}/claims/active",
    response_model=ClaimOut,
    summary="Inspect the current lease (no token is returned)",
)
def get_active_claim(workflow_id: uuid.UUID, db: DbSession, agent: CurrentAgent) -> ClaimOut:
    claim_service.get_workflow(db, workflow_id)
    return claim_out(claim_service.require_active_claim(db, workflow_id))
