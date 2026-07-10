"""Workflow creation, authentication, idempotency and state-transition rules."""

from __future__ import annotations

import uuid

from tests.conftest import GOOD_CHECKPOINT, create_workflow, error_code, make_recoverable


def test_create_workflow_starts_active_with_initial_checkpoint(client):
    workflow = create_workflow(client)

    assert workflow["status"] == "active"
    assert workflow["creator_agent_id"] == "agent-a"
    assert workflow["current_agent_id"] == "agent-a"
    assert workflow["current_checkpoint_version"] == 1
    assert workflow["checkpoint_count"] == 1
    assert workflow["lease_generation"] == 0
    assert workflow["latest_checkpoint_id"] is not None


def test_create_workflow_without_checkpoint_has_version_zero(client):
    workflow = create_workflow(client, initial_checkpoint=None)
    assert workflow["current_checkpoint_version"] == 0
    assert workflow["latest_checkpoint_id"] is None


def test_missing_api_key_is_401_with_machine_readable_error(anon_client):
    response = anon_client.post("/api/v1/workflows", json={"title": "x", "objective": "y"})

    assert response.status_code == 401
    body = response.json()
    assert body["error"]["code"] == "UNAUTHENTICATED"
    assert body["error"]["retryable"] is False
    assert body["request_id"]


def test_invalid_api_key_is_401(anon_client):
    response = anon_client.post(
        "/api/v1/workflows",
        json={"title": "x", "objective": "y"},
        headers={"Authorization": "Bearer nrb_not_a_real_key_at_all"},
    )
    assert response.status_code == 401
    assert error_code(response) == "UNAUTHENTICATED"


def test_unknown_workflow_is_404(client):
    response = client.get(f"/api/v1/workflows/{uuid.uuid4()}")
    assert response.status_code == 404
    assert error_code(response) == "WORKFLOW_NOT_FOUND"


def test_malformed_body_is_422_with_violations(client):
    response = client.post("/api/v1/workflows", json={"title": "", "objective": "y"})
    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "SCHEMA_VALIDATION_FAILED"
    assert body["error"]["details"]["violations"]


def test_idempotent_create_returns_the_same_workflow(client):
    payload = {
        "title": "Idempotent workflow",
        "objective": "Only one of me should exist",
        "initial_checkpoint": GOOD_CHECKPOINT,
    }
    headers = {"Idempotency-Key": "create-once-1"}

    first = client.post("/api/v1/workflows", json=payload, headers=headers)
    second = client.post("/api/v1/workflows", json=payload, headers=headers)

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    assert second.headers["Idempotent-Replay"] == "true"

    listed = client.get("/api/v1/workflows", params={"search": "Idempotent workflow"}).json()
    assert len(listed["items"]) == 1


def test_reusing_an_idempotency_key_with_a_different_body_is_409(client):
    headers = {"Idempotency-Key": "create-once-2"}
    client.post("/api/v1/workflows", json={"title": "A", "objective": "A"}, headers=headers)

    response = client.post(
        "/api/v1/workflows", json={"title": "B", "objective": "B"}, headers=headers
    )
    assert response.status_code == 409
    assert error_code(response) == "IDEMPOTENCY_KEY_REUSED"


def test_heartbeat_extends_the_deadline(client):
    workflow = create_workflow(client)
    response = client.post(f"/api/v1/workflows/{workflow['id']}/heartbeats", json={})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "active"
    assert body["next_heartbeat_due_at"] > body["last_heartbeat_at"]


def test_another_agent_cannot_heartbeat_someone_elses_workflow(client, other_client):
    workflow = create_workflow(client)
    response = other_client.post(f"/api/v1/workflows/{workflow['id']}/heartbeats", json={})

    assert response.status_code == 403
    assert error_code(response) == "FORBIDDEN"


def test_explicit_failure_makes_the_workflow_recoverable(client):
    workflow = create_workflow(client)
    failed = make_recoverable(client, workflow["id"], "Upstream API returned 503")

    assert failed["status"] == "recoverable"
    assert failed["current_agent_id"] is None
    assert failed["failed_at"] is not None


def test_failure_policy_dead_letter_skips_recovery(client):
    workflow = create_workflow(client, failure_policy="dead_letter")
    failed = make_recoverable(client, workflow["id"])

    assert failed["status"] == "dead_letter"


def test_max_recoveries_exhausted_moves_to_dead_letter(client, other_client):
    workflow = create_workflow(client, max_recoveries=0)
    failed = make_recoverable(client, workflow["id"])
    assert failed["status"] == "dead_letter"


def test_cannot_heartbeat_a_recoverable_workflow(client):
    workflow = create_workflow(client)
    make_recoverable(client, workflow["id"])

    response = client.post(f"/api/v1/workflows/{workflow['id']}/heartbeats", json={})
    assert response.status_code == 409
    assert error_code(response) == "INVALID_STATE_TRANSITION"


def test_cannot_complete_a_workflow_with_remaining_steps(client):
    workflow = create_workflow(client)
    response = client.post(
        f"/api/v1/workflows/{workflow['id']}/complete", json={"final_checkpoint_version": 1}
    )

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "COMPLETION_REQUIREMENTS_NOT_MET"
    unmet = [r["requirement"] for r in body["error"]["details"]["unmet_requirements"]]
    assert "NO_REMAINING_STEPS" in unmet


def test_complete_rejects_a_stale_final_version(client):
    workflow = create_workflow(client)
    response = client.post(
        f"/api/v1/workflows/{workflow['id']}/complete", json={"final_checkpoint_version": 99}
    )
    assert response.status_code == 409
    assert error_code(response) == "STALE_CHECKPOINT_VERSION"


def test_completion_is_replay_protected(client):
    workflow = create_workflow(client)
    finish = dict(GOOD_CHECKPOINT, remaining_steps=[], next_action="done", parent_version=1)
    client.post(f"/api/v1/workflows/{workflow['id']}/checkpoints", json=finish)

    body = {"final_checkpoint_version": 2}
    first = client.post(f"/api/v1/workflows/{workflow['id']}/complete", json=body)
    assert first.status_code == 200
    assert first.json()["status"] == "completed"

    # A naive retry (no Idempotency-Key) is rejected...
    second = client.post(f"/api/v1/workflows/{workflow['id']}/complete", json=body)
    assert second.status_code == 409
    assert error_code(second) == "WORKFLOW_ALREADY_COMPLETED"


def test_completion_with_idempotency_key_replays_instead_of_conflicting(client):
    workflow = create_workflow(client)
    finish = dict(GOOD_CHECKPOINT, remaining_steps=[], next_action="done", parent_version=1)
    client.post(f"/api/v1/workflows/{workflow['id']}/checkpoints", json=finish)

    headers = {"Idempotency-Key": "complete-once"}
    body = {"final_checkpoint_version": 2}
    first = client.post(f"/api/v1/workflows/{workflow['id']}/complete", json=body, headers=headers)
    second = client.post(f"/api/v1/workflows/{workflow['id']}/complete", json=body, headers=headers)

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.headers["Idempotent-Replay"] == "true"
    assert first.json()["completed_at"] == second.json()["completed_at"]


def test_cancel_requires_the_creator(client, other_client):
    workflow = create_workflow(client)

    denied = other_client.post(f"/api/v1/workflows/{workflow['id']}/cancel", json={"reason": "no"})
    assert denied.status_code == 403

    allowed = client.post(f"/api/v1/workflows/{workflow['id']}/cancel", json={"reason": "obsolete"})
    assert allowed.status_code == 200
    assert allowed.json()["status"] == "cancelled"


def test_cancelled_workflow_cannot_transition_further(client):
    workflow = create_workflow(client)
    client.post(f"/api/v1/workflows/{workflow['id']}/cancel", json={"reason": "obsolete"})

    response = client.post(f"/api/v1/workflows/{workflow['id']}/heartbeats", json={})
    assert response.status_code == 409
    assert error_code(response) == "INVALID_STATE_TRANSITION"


def test_workflow_listing_is_cursor_paginated(client):
    for index in range(5):
        create_workflow(client, title=f"Workflow {index}", initial_checkpoint=None)

    first = client.get("/api/v1/workflows", params={"limit": 2}).json()
    assert len(first["items"]) == 2
    assert first["has_more"] is True

    second = client.get(
        "/api/v1/workflows", params={"limit": 2, "cursor": first["next_cursor"]}
    ).json()
    assert len(second["items"]) == 2

    first_ids = {item["id"] for item in first["items"]}
    second_ids = {item["id"] for item in second["items"]}
    assert not (first_ids & second_ids)


def test_malformed_cursor_is_400(client):
    response = client.get("/api/v1/workflows", params={"cursor": "not-base64!!"})
    assert response.status_code == 400
    assert error_code(response) == "BAD_REQUEST"
