"""Deterministic checkpoint completeness evaluator.

No LLM is involved. The same checkpoint always produces the same score.

Scoring
-------
1. Start at ``100``.
2. Every rule that fires subtracts its ``weight``.
3. ``score = max(0, 100 - sum(weights of fired rules))``.
4. ``resumable = (no blocking issue fired) and (score >= RESUMABLE_MIN_SCORE)``.

``RESUMABLE_MIN_SCORE`` defaults to 50 and is configurable.

A blocking issue means a replacement agent cannot safely continue: the objective,
the next action, or the artifacts needed to resume are missing or untrustworthy.
A warning means work can continue but something is degraded.
"""

from __future__ import annotations

import dataclasses
from dataclasses import asdict, dataclass
from typing import Any

from app.config import settings
from app.models import Artifact, Checkpoint, VerificationStatus, Workflow, WorkflowStatus

BLOCKING = "blocking"
WARNING = "warning"


@dataclass(frozen=True)
class Rule:
    code: str
    severity: str
    weight: int
    message: str
    repair: str


# Single source of truth for the rule catalogue. references/checkpoint-schema.md
# is generated from this table, so the docs cannot drift from the code.
RULES: dict[str, Rule] = {
    r.code: r
    for r in [
        Rule(
            "NO_CHECKPOINT",
            BLOCKING,
            100,
            "The workflow has no checkpoint, so there is no progress to resume from.",
            "Create a checkpoint with POST /api/v1/workflows/{workflow_id}/checkpoints.",
        ),
        Rule(
            "UNSUPPORTED_SCHEMA_VERSION",
            BLOCKING,
            30,
            "The checkpoint uses a schema_version this service cannot interpret.",
            "Re-submit the checkpoint using a supported schema_version.",
        ),
        Rule(
            "MISSING_OBJECTIVE",
            BLOCKING,
            25,
            "The checkpoint has no objective, so the goal of the work is unknown.",
            "Set 'objective' to a one-sentence statement of the overall goal.",
        ),
        Rule(
            "MISSING_NEXT_ACTION",
            BLOCKING,
            20,
            "The checkpoint has no next_action, so a replacement agent has no entry point.",
            "Set 'next_action' to the single concrete step to perform first.",
        ),
        Rule(
            "NO_REMAINING_STEPS_BUT_INCOMPLETE",
            BLOCKING,
            15,
            "The workflow is not complete but remaining_steps is empty.",
            "List the outstanding steps in 'remaining_steps', or complete the workflow.",
        ),
        Rule(
            "COMPLETE_WITH_REMAINING_STEPS",
            BLOCKING,
            20,
            "The workflow is marked complete while remaining_steps is not empty.",
            "Clear 'remaining_steps' in a new checkpoint before completing.",
        ),
        Rule(
            "CONTRADICTORY_STEPS",
            BLOCKING,
            15,
            "One or more steps appear in both completed_steps and remaining_steps.",
            "Remove the duplicated steps from one of the two lists.",
        ),
        Rule(
            "ARTIFACT_MISSING_LOCATION",
            BLOCKING,
            15,
            "An artifact has neither a uri nor a storage_key, so it cannot be retrieved.",
            "Provide 'uri' or 'storage_key' for every artifact.",
        ),
        Rule(
            "ARTIFACT_VERIFICATION_FAILED",
            BLOCKING,
            20,
            "An artifact failed checksum verification and must not be trusted.",
            "Re-upload the artifact and register it with a correct sha256.",
        ),
        Rule(
            "ARTIFACT_MISSING_CHECKSUM",
            WARNING,
            8,
            "An artifact has no sha256, so its integrity cannot be verified.",
            "Register the artifact's sha256 so a replacement agent can verify it.",
        ),
        Rule(
            "ARTIFACT_UNVERIFIED",
            WARNING,
            5,
            "An artifact has a checksum that has never been verified against its content.",
            "Call POST /api/v1/workflows/{workflow_id}/artifacts/{artifact_id}/verify.",
        ),
        Rule(
            "CHECKPOINT_VERSION_GAP",
            WARNING,
            10,
            "parent_version is not exactly one less than version; history has a gap.",
            "Always set 'parent_version' to the version you read before writing.",
        ),
        Rule(
            "MISSING_PARENT_VERSION",
            WARNING,
            5,
            "A checkpoint after version 1 does not record its parent_version.",
            "Set 'parent_version' on every checkpoint after the first.",
        ),
        Rule(
            "MISSING_CONTEXT_SUMMARY",
            WARNING,
            8,
            "The checkpoint has no context_summary explaining the situation so far.",
            "Write two to five sentences of 'context_summary' describing what happened.",
        ),
        Rule(
            "DECISION_WITHOUT_REASON",
            WARNING,
            6,
            "A decision was recorded without an explanation of why it was made.",
            "Give every decision a 'reason' of at least 12 characters.",
        ),
        Rule(
            "NO_DECISIONS_RECORDED",
            WARNING,
            4,
            "No decisions were recorded, so prior judgement calls may be repeated.",
            "Record the choices already made so they are not re-litigated.",
        ),
    ]
}

MIN_REASON_LENGTH = 12


@dataclass
class Issue:
    code: str
    severity: str
    message: str
    weight: int
    # `field` is the JSON path the issue refers to. It shadows dataclasses.field
    # inside this class body, hence the qualified call below.
    field: str | None = None
    details: dict[str, Any] = dataclasses.field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ContextEvaluation:
    resumable: bool
    score: int
    blocking_issues: list[Issue]
    warnings: list[Issue]
    recommended_repairs: list[str]
    evaluated_checkpoint_version: int | None
    min_score_for_resume: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "resumable": self.resumable,
            "score": self.score,
            "blocking_issues": [i.to_dict() for i in self.blocking_issues],
            "warnings": [i.to_dict() for i in self.warnings],
            "recommended_repairs": self.recommended_repairs,
            "evaluated_checkpoint_version": self.evaluated_checkpoint_version,
            "min_score_for_resume": self.min_score_for_resume,
        }


def _normalise(step: Any) -> str:
    return " ".join(str(step).strip().lower().split())


def _issue(code: str, *, field: str | None = None, **details: Any) -> Issue:
    rule = RULES[code]
    return Issue(
        code=rule.code,
        severity=rule.severity,
        message=rule.message,
        weight=rule.weight,
        field=field,
        details=details,
    )


def evaluate_context(
    workflow: Workflow,
    checkpoint: Checkpoint | None,
    artifacts: list[Artifact] | None = None,
) -> ContextEvaluation:
    """Evaluate whether ``checkpoint`` is sufficient for another agent to resume."""
    artifacts = artifacts or []
    issues: list[Issue] = []

    if checkpoint is None:
        issues.append(_issue("NO_CHECKPOINT"))
        return _finalise(issues, None)

    if checkpoint.schema_version not in settings.supported_checkpoint_schema_versions:
        issues.append(
            _issue(
                "UNSUPPORTED_SCHEMA_VERSION",
                field="schema_version",
                found=checkpoint.schema_version,
                supported=settings.supported_checkpoint_schema_versions,
            )
        )

    if not (checkpoint.objective or "").strip():
        issues.append(_issue("MISSING_OBJECTIVE", field="objective"))

    if not (checkpoint.next_action or "").strip():
        issues.append(_issue("MISSING_NEXT_ACTION", field="next_action"))

    completed = list(checkpoint.completed_steps or [])
    remaining = list(checkpoint.remaining_steps or [])
    workflow_is_finished = workflow.status in {WorkflowStatus.completed, WorkflowStatus.cancelled}

    if not remaining and not workflow_is_finished:
        issues.append(_issue("NO_REMAINING_STEPS_BUT_INCOMPLETE", field="remaining_steps"))
    if remaining and workflow.status == WorkflowStatus.completed:
        issues.append(
            _issue(
                "COMPLETE_WITH_REMAINING_STEPS",
                field="remaining_steps",
                remaining_count=len(remaining),
            )
        )

    overlap = sorted({_normalise(s) for s in completed} & {_normalise(s) for s in remaining})
    if overlap:
        issues.append(_issue("CONTRADICTORY_STEPS", field="remaining_steps", overlapping=overlap))

    if not (checkpoint.context_summary or "").strip():
        issues.append(_issue("MISSING_CONTEXT_SUMMARY", field="context_summary"))

    decisions = list(checkpoint.decisions or [])
    if not decisions and completed:
        issues.append(_issue("NO_DECISIONS_RECORDED", field="decisions"))
    for index, decision in enumerate(decisions):
        reason = ""
        if isinstance(decision, dict):
            reason = str(decision.get("reason") or "").strip()
        if len(reason) < MIN_REASON_LENGTH:
            issues.append(
                _issue(
                    "DECISION_WITHOUT_REASON",
                    field=f"decisions[{index}]",
                    min_reason_length=MIN_REASON_LENGTH,
                )
            )
            break  # one issue is enough; the repair applies to all of them

    if checkpoint.version > 1 and checkpoint.parent_version is None:
        issues.append(_issue("MISSING_PARENT_VERSION", field="parent_version"))
    elif checkpoint.parent_version is not None and checkpoint.parent_version != checkpoint.version - 1:
        issues.append(
            _issue(
                "CHECKPOINT_VERSION_GAP",
                field="parent_version",
                version=checkpoint.version,
                parent_version=checkpoint.parent_version,
            )
        )

    seen_missing_location = seen_failed = seen_missing_checksum = seen_unverified = False
    for artifact in artifacts:
        if not artifact.uri and not artifact.storage_key and not seen_missing_location:
            issues.append(_issue("ARTIFACT_MISSING_LOCATION", field=f"artifacts.{artifact.name}"))
            seen_missing_location = True
        if artifact.verification_status == VerificationStatus.failed and not seen_failed:
            issues.append(
                _issue(
                    "ARTIFACT_VERIFICATION_FAILED",
                    field=f"artifacts.{artifact.name}",
                    error=artifact.verification_error,
                )
            )
            seen_failed = True
        if not artifact.sha256 and not seen_missing_checksum:
            issues.append(_issue("ARTIFACT_MISSING_CHECKSUM", field=f"artifacts.{artifact.name}"))
            seen_missing_checksum = True
        elif (
            artifact.sha256
            and artifact.verification_status == VerificationStatus.unverified
            and not seen_unverified
        ):
            issues.append(_issue("ARTIFACT_UNVERIFIED", field=f"artifacts.{artifact.name}"))
            seen_unverified = True

    return _finalise(issues, checkpoint.version)


def _finalise(issues: list[Issue], version: int | None) -> ContextEvaluation:
    blocking = [i for i in issues if i.severity == BLOCKING]
    warnings = [i for i in issues if i.severity == WARNING]
    score = max(0, 100 - sum(i.weight for i in issues))
    resumable = not blocking and score >= settings.resumable_min_score

    repairs: list[str] = []
    for issue in blocking + warnings:
        repair = RULES[issue.code].repair
        if repair not in repairs:
            repairs.append(repair)

    return ContextEvaluation(
        resumable=resumable,
        score=score,
        blocking_issues=blocking,
        warnings=warnings,
        recommended_repairs=repairs,
        evaluated_checkpoint_version=version,
        min_score_for_resume=settings.resumable_min_score,
    )
