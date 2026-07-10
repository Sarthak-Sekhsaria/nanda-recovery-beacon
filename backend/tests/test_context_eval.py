"""The deterministic completeness evaluator."""

from __future__ import annotations

from app.context_eval import RULES, evaluate_context
from app.models import Checkpoint, VerificationStatus, Workflow, WorkflowStatus
from tests.conftest import GOOD_CHECKPOINT, create_workflow, make_recoverable


def _workflow(status: WorkflowStatus = WorkflowStatus.recoverable) -> Workflow:
    return Workflow(title="t", objective="o", status=status, creator_agent_id="a")


def _checkpoint(**overrides) -> Checkpoint:
    base = {
        "version": 2,
        "parent_version": 1,
        "objective": "Compare five scholarship programs",
        "completed_steps": ["Found five programs"],
        "remaining_steps": ["Compare deadlines"],
        "decisions": [{"decision": "Only international programs", "reason": "Required by the request"}],
        "next_action": "Compare application deadlines",
        "context_summary": "Five programs found. Deadlines not compared.",
        "variables": {},
        "producing_agent_id": "a",
        "schema_version": "1.0",
        "content_checksum": "x",
    }
    base.update(overrides)
    return Checkpoint(**base)


def _codes(evaluation) -> set[str]:
    return {i.code for i in evaluation.blocking_issues + evaluation.warnings}


def test_a_complete_checkpoint_scores_100_and_is_resumable():
    evaluation = evaluate_context(_workflow(), _checkpoint(), [])

    assert evaluation.score == 100
    assert evaluation.resumable is True
    assert evaluation.blocking_issues == []
    assert evaluation.warnings == []
    assert evaluation.recommended_repairs == []


def test_no_checkpoint_is_blocking_and_scores_zero():
    evaluation = evaluate_context(_workflow(), None, [])

    assert evaluation.score == 0
    assert evaluation.resumable is False
    assert _codes(evaluation) == {"NO_CHECKPOINT"}
    assert evaluation.evaluated_checkpoint_version is None


def test_missing_objective_and_next_action_are_blocking():
    evaluation = evaluate_context(_workflow(), _checkpoint(objective="", next_action=None), [])

    assert evaluation.resumable is False
    assert "MISSING_OBJECTIVE" in _codes(evaluation)
    assert "MISSING_NEXT_ACTION" in _codes(evaluation)
    assert evaluation.score == 100 - RULES["MISSING_OBJECTIVE"].weight - RULES["MISSING_NEXT_ACTION"].weight


def test_contradictory_steps_are_blocking():
    evaluation = evaluate_context(
        _workflow(),
        _checkpoint(completed_steps=["Compare deadlines"], remaining_steps=["compare  DEADLINES "]),
        [],
    )

    assert "CONTRADICTORY_STEPS" in _codes(evaluation)
    issue = next(i for i in evaluation.blocking_issues if i.code == "CONTRADICTORY_STEPS")
    assert issue.details["overlapping"] == ["compare deadlines"]


def test_empty_remaining_steps_on_an_incomplete_workflow_is_blocking():
    evaluation = evaluate_context(_workflow(), _checkpoint(remaining_steps=[]), [])
    assert "NO_REMAINING_STEPS_BUT_INCOMPLETE" in _codes(evaluation)


def test_completed_workflow_with_remaining_steps_is_blocking():
    evaluation = evaluate_context(
        _workflow(WorkflowStatus.completed), _checkpoint(remaining_steps=["still todo"]), []
    )
    assert "COMPLETE_WITH_REMAINING_STEPS" in _codes(evaluation)


def test_completed_workflow_with_no_remaining_steps_is_clean():
    evaluation = evaluate_context(
        _workflow(WorkflowStatus.completed), _checkpoint(remaining_steps=[]), []
    )
    assert evaluation.blocking_issues == []


def test_missing_context_summary_and_thin_decision_reasons_are_warnings():
    evaluation = evaluate_context(
        _workflow(),
        _checkpoint(context_summary=None, decisions=[{"decision": "Use X", "reason": "why"}]),
        [],
    )

    assert evaluation.blocking_issues == []
    assert "MISSING_CONTEXT_SUMMARY" in _codes(evaluation)
    assert "DECISION_WITHOUT_REASON" in _codes(evaluation)
    assert evaluation.resumable is True  # warnings alone do not block


def test_unsupported_schema_version_is_blocking():
    evaluation = evaluate_context(_workflow(), _checkpoint(schema_version="99.0"), [])
    assert "UNSUPPORTED_SCHEMA_VERSION" in _codes(evaluation)


def test_checkpoint_version_gap_is_a_warning():
    evaluation = evaluate_context(_workflow(), _checkpoint(version=5, parent_version=2), [])
    assert "CHECKPOINT_VERSION_GAP" in _codes(evaluation)
    assert evaluation.blocking_issues == []


def test_missing_parent_version_after_v1_is_a_warning():
    evaluation = evaluate_context(_workflow(), _checkpoint(version=3, parent_version=None), [])
    assert "MISSING_PARENT_VERSION" in _codes(evaluation)


def test_artifact_rules(db):
    from app.models import Artifact

    unlocated = Artifact(name="a", uri=None, storage_key=None, produced_by_agent_id="a")
    failed = Artifact(
        name="b",
        uri="https://example.com/b",
        sha256="a" * 64,
        produced_by_agent_id="a",
        verification_status=VerificationStatus.failed,
        verification_error="checksum mismatch",
    )
    unverified = Artifact(
        name="c",
        uri="https://example.com/c",
        sha256="b" * 64,
        produced_by_agent_id="a",
        verification_status=VerificationStatus.unverified,
    )
    no_checksum = Artifact(name="d", uri="https://example.com/d", produced_by_agent_id="a")

    codes = _codes(
        evaluate_context(_workflow(), _checkpoint(), [unlocated, failed, unverified, no_checksum])
    )
    assert "ARTIFACT_MISSING_LOCATION" in codes
    assert "ARTIFACT_VERIFICATION_FAILED" in codes
    assert "ARTIFACT_UNVERIFIED" in codes
    assert "ARTIFACT_MISSING_CHECKSUM" in codes


def test_score_never_goes_below_zero():
    evaluation = evaluate_context(
        _workflow(),
        _checkpoint(
            objective="",
            next_action=None,
            remaining_steps=[],
            context_summary=None,
            schema_version="99",
            decisions=[{"decision": "x"}],
            parent_version=None,
            version=4,
        ),
        [],
    )
    assert evaluation.score == 0
    assert evaluation.resumable is False


def test_evaluate_context_endpoint_scores_the_latest_checkpoint(client):
    workflow = create_workflow(client)
    response = client.post(f"/api/v1/workflows/{workflow['id']}/evaluate-context", json={})

    assert response.status_code == 200
    body = response.json()
    assert body["score"] == 100
    assert body["resumable"] is True
    assert body["evaluated_checkpoint_version"] == 1
    assert body["min_score_for_resume"] == 50


def test_evaluate_context_endpoint_scores_a_draft_before_writing_it(client):
    workflow = create_workflow(client)
    draft = dict(GOOD_CHECKPOINT, next_action=None, context_summary=None)

    response = client.post(
        f"/api/v1/workflows/{workflow['id']}/evaluate-context", json={"checkpoint": draft}
    )

    body = response.json()
    assert body["resumable"] is False
    assert [i["code"] for i in body["blocking_issues"]] == ["MISSING_NEXT_ACTION"]
    assert body["recommended_repairs"]

    # The draft was not stored.
    assert client.get(f"/api/v1/workflows/{workflow['id']}").json()["current_checkpoint_version"] == 1


def test_recoverable_queue_exposes_context_scores(client):
    good = create_workflow(client, title="Good context")
    bad = create_workflow(
        client, title="Bad context", initial_checkpoint=dict(GOOD_CHECKPOINT, next_action=None)
    )
    make_recoverable(client, good["id"])
    make_recoverable(client, bad["id"])

    items = client.get("/api/v1/recoverable-workflows").json()["items"]
    by_id = {item["workflow"]["id"]: item for item in items}

    assert by_id[good["id"]]["resumable"] is True
    assert by_id[bad["id"]]["resumable"] is False
    assert "MISSING_NEXT_ACTION" in by_id[bad["id"]]["blocking_issue_codes"]

    resumable_only = client.get(
        "/api/v1/recoverable-workflows", params={"resumable_only": True}
    ).json()["items"]
    assert {item["workflow"]["id"] for item in resumable_only} == {good["id"]}
