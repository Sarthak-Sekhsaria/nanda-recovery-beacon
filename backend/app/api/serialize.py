"""Model -> JSON helpers. Aliases are always applied so responses use `metadata`."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from app.schemas import (
    ArtifactOut,
    CheckpointOut,
    ClaimOut,
    ContextEvaluationOut,
    EventOut,
    WorkflowOut,
)


def dump(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json", by_alias=True)


def workflow_out(workflow: Any) -> WorkflowOut:
    return WorkflowOut.model_validate(workflow, from_attributes=True)


def checkpoint_out(checkpoint: Any) -> CheckpointOut:
    return CheckpointOut.model_validate(checkpoint, from_attributes=True)


def checkpoint_out_opt(checkpoint: Any) -> CheckpointOut | None:
    return checkpoint_out(checkpoint) if checkpoint is not None else None


def claim_out(claim: Any) -> ClaimOut:
    return ClaimOut.model_validate(claim, from_attributes=True)


def claim_out_opt(claim: Any) -> ClaimOut | None:
    return claim_out(claim) if claim is not None else None


def artifact_out(artifact: Any) -> ArtifactOut:
    return ArtifactOut.model_validate(artifact, from_attributes=True)


def event_out(event: Any) -> EventOut:
    return EventOut.model_validate(event, from_attributes=True)


def evaluation_out(evaluation: dict[str, Any]) -> ContextEvaluationOut:
    return ContextEvaluationOut.model_validate(evaluation)
