"""Artifact registration and checksum verification."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Request, Response

from app import metrics
from app.api.deps import CurrentAgent, DbSession, PageParams, RequestId
from app.api.serialize import artifact_out, dump
from app.artifact_verify import verify_checksum
from app.db import utcnow
from app.errors import ArtifactNotFound, ArtifactVerificationFailed
from app.idempotency import IdempotencyGuard, key_from
from app.models import Artifact, EventType, VerificationStatus
from app.schemas import ArtifactCreate, ArtifactOut, Page, UploadUrlRequest
from app.services import claims as claim_service
from app.services import recovery as recovery_service
from app.services.events import record_event
from app.storage import get_storage

router = APIRouter(tags=["artifacts"])


def _get_artifact(db, workflow_id: uuid.UUID, artifact_id: uuid.UUID) -> Artifact:
    artifact = db.get(Artifact, artifact_id)
    if artifact is None or artifact.workflow_id != workflow_id:
        raise ArtifactNotFound(details={"artifact_id": str(artifact_id)})
    return artifact


@router.post(
    "/workflows/{workflow_id}/artifacts",
    status_code=201,
    response_model=ArtifactOut,
    summary="Register an artifact needed to resume the work",
    description=(
        "Register an externally hosted file by `uri` plus its `sha256`. Set `verify: true` "
        "to have the Beacon fetch the URL and confirm the checksum now; a mismatch returns "
        "422 ARTIFACT_VERIFICATION_FAILED and records the artifact as `failed`.\n\n"
        "URLs must be http(s) and must not resolve to a private network."
    ),
)
def create_artifact(
    request: Request,
    workflow_id: uuid.UUID,
    payload: ArtifactCreate,
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
    claim_service.authorize_write(
        db, workflow, agent_id=agent.agent_id, lease_token=payload.lease_token
    )

    artifact = Artifact(
        workflow_id=workflow.id,
        name=payload.name,
        uri=payload.uri,
        storage_key=payload.storage_key,
        sha256=payload.sha256.lower() if payload.sha256 else None,
        content_type=payload.content_type,
        size_bytes=payload.size_bytes,
        description=payload.description,
        checkpoint_version=payload.checkpoint_version or workflow.current_checkpoint_version or None,
        produced_by_agent_id=agent.agent_id,
        verification_status=VerificationStatus.unverified,
    )

    if payload.verify:
        # ArtifactCreate._location_present guarantees both are present when verify is set.
        assert payload.uri is not None and payload.sha256 is not None
        result = verify_checksum(payload.uri, payload.sha256)
        if not result.ok:
            artifact.verification_status = VerificationStatus.failed
            artifact.verification_error = result.error
            db.add(artifact)
            db.flush()
            record_event(
                db,
                workflow_id=workflow.id,
                event_type=EventType.artifact_verification_failed,
                actor_agent_id=agent.agent_id,
                request_id=request_id,
                metadata={"artifact": payload.name, "error": result.error},
            )
            metrics.artifact_verifications_total.labels(result="failed").inc()
            db.commit()  # keep the audit record and the failed artifact
            raise ArtifactVerificationFailed(
                details={"artifact_id": str(artifact.id), "error": result.error}
            )
        artifact.verification_status = VerificationStatus.verified
        artifact.verified_at = utcnow()
        artifact.size_bytes = result.size_bytes
        artifact.content_type = artifact.content_type or result.content_type
        metrics.artifact_verifications_total.labels(result="verified").inc()

    db.add(artifact)
    db.flush()
    record_event(
        db,
        workflow_id=workflow.id,
        event_type=EventType.artifact_registered,
        actor_agent_id=agent.agent_id,
        request_id=request_id,
        checkpoint_version=artifact.checkpoint_version,
        metadata={
            "artifact": artifact.name,
            "verification_status": artifact.verification_status.value,
            "sha256": artifact.sha256,
        },
    )
    return guard.commit(201, dump(artifact_out(artifact)))


@router.get(
    "/workflows/{workflow_id}/artifacts",
    response_model=Page[ArtifactOut],
    summary="List artifacts with their verification state",
)
def list_artifacts(
    workflow_id: uuid.UUID, db: DbSession, agent: CurrentAgent, page: PageParams
) -> Page[ArtifactOut]:
    claim_service.get_workflow(db, workflow_id)
    rows = recovery_service.workflow_artifacts(db, workflow_id)
    return Page[ArtifactOut](items=[artifact_out(a) for a in rows], next_cursor=None, has_more=False)


@router.post(
    "/workflows/{workflow_id}/artifacts/{artifact_id}/verify",
    response_model=ArtifactOut,
    summary="Re-fetch an artifact and re-check its SHA-256",
)
def verify_artifact(
    workflow_id: uuid.UUID,
    artifact_id: uuid.UUID,
    db: DbSession,
    agent: CurrentAgent,
    request_id: RequestId,
) -> ArtifactOut:
    claim_service.get_workflow(db, workflow_id)
    artifact = _get_artifact(db, workflow_id, artifact_id)

    if not artifact.uri or not artifact.sha256:
        raise ArtifactVerificationFailed(
            "Verification needs both a uri and a sha256.",
            details={"artifact_id": str(artifact_id)},
        )

    result = verify_checksum(artifact.uri, artifact.sha256)
    if result.ok:
        artifact.verification_status = VerificationStatus.verified
        artifact.verification_error = None
        artifact.verified_at = utcnow()
        artifact.size_bytes = result.size_bytes
        metrics.artifact_verifications_total.labels(result="verified").inc()
        db.commit()
        return artifact_out(artifact)

    artifact.verification_status = VerificationStatus.failed
    artifact.verification_error = result.error
    record_event(
        db,
        workflow_id=workflow_id,
        event_type=EventType.artifact_verification_failed,
        actor_agent_id=agent.agent_id,
        request_id=request_id,
        metadata={"artifact": artifact.name, "error": result.error},
    )
    metrics.artifact_verifications_total.labels(result="failed").inc()
    db.commit()
    raise ArtifactVerificationFailed(details={"artifact_id": str(artifact_id), "error": result.error})


@router.post(
    "/workflows/{workflow_id}/artifacts/upload-url",
    summary="Get a pre-signed upload URL (requires STORAGE_BACKEND=s3)",
    description=(
        "Returns 501 STORAGE_BACKEND_DISABLED unless an S3-compatible object store is "
        "configured. Artifact bytes never pass through the Beacon."
    ),
)
def upload_url(
    workflow_id: uuid.UUID,
    payload: UploadUrlRequest,
    db: DbSession,
    agent: CurrentAgent,
) -> dict:
    claim_service.get_workflow(db, workflow_id)
    key = f"workflows/{workflow_id}/{uuid.uuid4().hex}/{payload.name}"
    return get_storage().presigned_put(key, payload.content_type)
