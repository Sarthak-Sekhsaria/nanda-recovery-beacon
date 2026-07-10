"""Claim leasing: exclusivity, renewal, release, expiry and fencing."""

from __future__ import annotations

import time

from tests.conftest import (
    GOOD_CHECKPOINT,
    claim,
    create_workflow,
    error_code,
    make_recoverable,
)


def _recoverable(client) -> str:
    workflow = create_workflow(client)
    make_recoverable(client, workflow["id"])
    return workflow["id"]


def test_cannot_claim_an_active_workflow(client, other_client):
    workflow = create_workflow(client)
    response = claim(other_client, workflow["id"])

    assert response.status_code == 409
    assert error_code(response) == "WORKFLOW_NOT_RECOVERABLE"


def test_claiming_a_recoverable_workflow_returns_a_lease_once(client, other_client):
    wid = _recoverable(client)
    response = claim(other_client, wid)

    assert response.status_code == 201
    body = response.json()
    assert body["lease_token"].startswith("lease_")
    assert body["fencing_token"] == 1
    assert body["claim"]["agent_id"] == "agent-b"
    assert body["workflow"]["status"] == "claimed"
    assert body["workflow"]["current_agent_id"] == "agent-b"

    # The token is never returned again.
    active = other_client.get(f"/api/v1/workflows/{wid}/claims/active").json()
    assert "lease_token" not in active
    assert active["lease_token_prefix"] == body["lease_token"][:12]


def test_second_claim_conflicts(client, other_client, admin_client):
    wid = _recoverable(client)
    assert claim(other_client, wid).status_code == 201

    second = claim(admin_client, wid)
    assert second.status_code == 409
    assert error_code(second) == "CLAIM_ALREADY_HELD"
    assert second.json()["error"]["details"]["held_by_agent_id"] == "agent-b"
    assert second.json()["error"]["retryable"] is True
    assert "Retry-After" in second.headers


def test_blocking_context_issues_must_be_acknowledged(client, other_client):
    workflow = create_workflow(
        client,
        initial_checkpoint=dict(GOOD_CHECKPOINT, next_action=None),  # blocking issue
    )
    make_recoverable(client, workflow["id"])

    refused = claim(other_client, workflow["id"])
    assert refused.status_code == 422
    details = refused.json()["error"]["details"]
    assert details["code"] == "BLOCKING_CONTEXT_ISSUES"
    assert any(i["code"] == "MISSING_NEXT_ACTION" for i in details["blocking_issues"])

    accepted = claim(other_client, workflow["id"], acknowledge_blocking_issues=True)
    assert accepted.status_code == 201


def test_writes_require_the_lease_token_once_claimed(client, other_client):
    wid = _recoverable(client)
    lease = claim(other_client, wid).json()["lease_token"]

    without = other_client.post(f"/api/v1/workflows/{wid}/heartbeats", json={})
    assert without.status_code == 403
    assert error_code(without) == "NOT_LEASE_HOLDER"

    with_token = other_client.post(
        f"/api/v1/workflows/{wid}/heartbeats", json={"lease_token": lease}
    )
    assert with_token.status_code == 200


def test_a_different_agent_cannot_use_a_stolen_looking_token(client, other_client, admin_client):
    wid = _recoverable(client)
    lease = claim(other_client, wid).json()["lease_token"]

    response = admin_client.post(
        f"/api/v1/workflows/{wid}/heartbeats", json={"lease_token": lease}
    )
    assert response.status_code == 403
    assert error_code(response) == "NOT_LEASE_HOLDER"


def test_renewing_extends_the_lease(client, other_client):
    wid = _recoverable(client)
    acquired = claim(other_client, wid, lease_seconds=10).json()

    renewed = other_client.post(
        f"/api/v1/workflows/{wid}/claims/renew",
        json={"lease_token": acquired["lease_token"], "lease_seconds": 60},
    )
    assert renewed.status_code == 200
    assert renewed.json()["renewal_count"] == 1
    assert renewed.json()["expires_at"] > acquired["lease_expires_at"]


def test_releasing_returns_the_workflow_to_the_queue(client, other_client):
    wid = _recoverable(client)
    lease = claim(other_client, wid).json()["lease_token"]

    released = other_client.post(
        f"/api/v1/workflows/{wid}/claims/release",
        json={"lease_token": lease, "reason": "cannot_reach_upstream"},
    )
    assert released.status_code == 200
    assert released.json()["status"] == "released"

    workflow = client.get(f"/api/v1/workflows/{wid}").json()
    assert workflow["status"] == "recoverable"
    assert workflow["current_agent_id"] is None

    # Someone else may claim it straight away.
    assert claim(client, wid).status_code == 201


def test_an_expired_lease_is_rejected_with_410(client, other_client):
    wid = _recoverable(client)
    lease = claim(other_client, wid, lease_seconds=1).json()["lease_token"]
    time.sleep(1.5)

    response = other_client.post(
        f"/api/v1/workflows/{wid}/heartbeats", json={"lease_token": lease}
    )
    assert response.status_code == 410
    assert error_code(response) == "LEASE_EXPIRED"


def test_an_expired_lease_cannot_be_renewed(client, other_client):
    wid = _recoverable(client)
    lease = claim(other_client, wid, lease_seconds=1).json()["lease_token"]
    time.sleep(1.5)

    response = other_client.post(
        f"/api/v1/workflows/{wid}/claims/renew", json={"lease_token": lease, "lease_seconds": 60}
    )
    assert response.status_code == 410
    assert error_code(response) == "LEASE_EXPIRED"


def test_expired_claim_can_be_taken_over_and_the_old_token_is_fenced(client, other_client, admin_client):
    """The classic fencing scenario from distributed locking."""
    wid = _recoverable(client)
    old_lease = claim(other_client, wid, lease_seconds=1).json()["lease_token"]
    time.sleep(1.5)

    # A second agent claims: this expires the old lease and bumps the fencing token.
    takeover = claim(admin_client, wid, lease_seconds=60)
    assert takeover.status_code == 201
    assert takeover.json()["fencing_token"] == 2

    # The original holder's token is now superseded, not merely expired.
    stale = other_client.post(
        f"/api/v1/workflows/{wid}/checkpoints",
        json=dict(GOOD_CHECKPOINT, parent_version=1, lease_token=old_lease),
    )
    assert stale.status_code == 409
    assert error_code(stale) == "FENCING_TOKEN_STALE"
    assert stale.json()["error"]["details"]["your_lease_generation"] == 1
    assert stale.json()["error"]["details"]["current_lease_generation"] == 2


def test_unknown_lease_token_is_403(client, other_client):
    wid = _recoverable(client)
    claim(other_client, wid)

    response = other_client.post(
        f"/api/v1/workflows/{wid}/heartbeats", json={"lease_token": "lease_totally_made_up"}
    )
    assert response.status_code == 403
    assert error_code(response) == "NOT_LEASE_HOLDER"


def test_resume_moves_a_claimed_workflow_back_to_active(client, other_client):
    wid = _recoverable(client)
    lease = claim(other_client, wid).json()["lease_token"]

    resumed = other_client.post(f"/api/v1/workflows/{wid}/resume", json={"lease_token": lease})
    assert resumed.status_code == 200
    body = resumed.json()
    assert body["status"] == "active"
    assert body["current_agent_id"] == "agent-b"
    assert body["recovery_count"] == 1
    assert body["recovered_at"] is not None


def test_resume_requires_the_claimed_state(client, other_client):
    workflow = create_workflow(client)
    response = other_client.post(
        f"/api/v1/workflows/{workflow['id']}/resume", json={"lease_token": "lease_x"}
    )
    assert response.status_code == 403  # no claim exists, and you are not the current agent


def test_claims_active_is_404_when_no_lease_exists(client):
    wid = _recoverable(client)
    response = client.get(f"/api/v1/workflows/{wid}/claims/active")
    assert response.status_code == 404
    assert error_code(response) == "CLAIM_NOT_FOUND"
