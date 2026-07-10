"""End-to-end recovery lifecycle.

Agent A starts work and dies. Agent B discovers, claims, reads the recovery
package, resumes, finishes and completes. The audit trail is then verified.
"""

from __future__ import annotations

import time

from app.reaper import run_sweep
from tests.conftest import GOOD_CHECKPOINT, create_workflow


def test_full_recovery_lifecycle(client, other_client):
    # --- Agent A: create the work -------------------------------------------
    workflow = create_workflow(
        client,
        title="Compare five scholarship programs",
        heartbeat_timeout_seconds=1,
        priority="critical",
        tags=["research", "e2e"],
    )
    wid = workflow["id"]
    assert workflow["status"] == "active"

    # --- Agent A: make progress ----------------------------------------------
    second = client.post(
        f"/api/v1/workflows/{wid}/checkpoints",
        json=dict(
            GOOD_CHECKPOINT,
            parent_version=1,
            completed_steps=["Found five programs", "Collected eligibility requirements"],
            remaining_steps=["Compare deadlines", "Produce recommendation"],
            next_action="Compare application deadlines",
        ),
    )
    assert second.status_code == 201
    assert second.json()["version"] == 2

    # --- Agent A: dies. The heartbeat deadline passes. ------------------------
    time.sleep(1.2)
    run_sweep()  # active -> suspected_failed
    time.sleep(1.2)
    run_sweep()  # suspected_failed -> recoverable

    assert client.get(f"/api/v1/workflows/{wid}").json()["status"] == "recoverable"

    # --- Agent B: discover ----------------------------------------------------
    queue = other_client.get(
        "/api/v1/recoverable-workflows", params={"priority": "critical", "tag": "e2e"}
    ).json()
    discovered = next(item for item in queue["items"] if item["workflow"]["id"] == wid)
    assert discovered["resumable"] is True
    assert discovered["context_score"] == 100
    assert discovered["latest_checkpoint_version"] == 2

    # --- Agent B: read the recovery package before touching anything ----------
    package = other_client.get(f"/api/v1/workflows/{wid}/recovery-package").json()
    assert package["context_evaluation"]["resumable"] is True
    assert package["active_claim"] is None
    assert package["latest_checkpoint"]["version"] == 2
    assert package["checkpoint_history"] == [1, 2]
    assert package["resume_instructions"]["claim_first"] is True
    assert package["resume_instructions"]["expected_parent_version"] == 2
    assert "Found five programs" in package["resume_instructions"]["must_not_repeat"]
    assert package["resume_instructions"]["next_action"] == "Compare application deadlines"

    # --- Agent B: claim -------------------------------------------------------
    acquired = other_client.post(
        f"/api/v1/workflows/{wid}/claims", json={"lease_seconds": 120}
    ).json()
    lease = acquired["lease_token"]
    assert acquired["fencing_token"] == 1

    # Agent A, were it alive, can no longer write.
    intruder = client.post(f"/api/v1/workflows/{wid}/heartbeats", json={})
    assert intruder.status_code == 403

    # --- Agent B: resume ------------------------------------------------------
    resumed = other_client.post(f"/api/v1/workflows/{wid}/resume", json={"lease_token": lease})
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "active"
    assert resumed.json()["current_agent_id"] == "agent-b"

    # --- Agent B: keep the lease alive and finish the work --------------------
    renewed = other_client.post(
        f"/api/v1/workflows/{wid}/claims/renew", json={"lease_token": lease, "lease_seconds": 120}
    )
    assert renewed.status_code == 200

    final = other_client.post(
        f"/api/v1/workflows/{wid}/checkpoints",
        json=dict(
            GOOD_CHECKPOINT,
            parent_version=2,
            lease_token=lease,
            completed_steps=[
                "Found five programs",
                "Collected eligibility requirements",
                "Compared deadlines",
                "Produced recommendation",
            ],
            remaining_steps=[],
            next_action="Nothing remains; ready to complete",
            context_summary="All five programs compared. Recommendation produced.",
        ),
    )
    assert final.status_code == 201
    assert final.json()["version"] == 3
    assert final.json()["producing_agent_id"] == "agent-b"

    # --- Agent B: complete ----------------------------------------------------
    completed = other_client.post(
        f"/api/v1/workflows/{wid}/complete",
        json={"lease_token": lease, "final_checkpoint_version": 3, "summary": "Recommended program C"},
        headers={"Idempotency-Key": "e2e-complete"},
    )
    assert completed.status_code == 200
    body = completed.json()
    assert body["status"] == "completed"
    assert body["completed_at"] is not None
    assert body["recovery_count"] == 1

    # A retried completion replays rather than conflicting.
    replay = other_client.post(
        f"/api/v1/workflows/{wid}/complete",
        json={"lease_token": lease, "final_checkpoint_version": 3, "summary": "Recommended program C"},
        headers={"Idempotency-Key": "e2e-complete"},
    )
    assert replay.status_code == 200
    assert replay.headers["Idempotent-Replay"] == "true"

    # --- The audit trail tells the whole story --------------------------------
    events = client.get(f"/api/v1/workflows/{wid}/events", params={"limit": 100}).json()["items"]
    kinds = [event["event_type"] for event in events]

    for expected in (
        "workflow_created",
        "checkpoint_created",
        "failure_suspected",
        "workflow_made_recoverable",
        "claim_acquired",
        "workflow_resumed",
        "workflow_completed",
    ):
        assert expected in kinds, f"missing '{expected}' in audit trail: {kinds}"

    # Events are newest-first and carry the acting agent.
    completion = next(e for e in events if e["event_type"] == "workflow_completed")
    assert completion["actor_agent_id"] == "agent-b"
    assert completion["checkpoint_version"] == 3

    creation = next(e for e in events if e["event_type"] == "workflow_created")
    assert creation["actor_agent_id"] == "agent-a"

    # Checkpoints remain readable and immutable after completion.
    history = client.get(f"/api/v1/workflows/{wid}/checkpoints").json()["items"]
    assert [c["version"] for c in history] == [3, 2, 1]
    assert history[-1]["producing_agent_id"] == "agent-a"

    # The completed workflow is no longer offered for recovery.
    queue_after = other_client.get("/api/v1/recoverable-workflows").json()["items"]
    assert all(item["workflow"]["id"] != wid for item in queue_after)


def test_lease_token_never_appears_in_any_read_response(client, other_client):
    workflow = create_workflow(client)
    client.post(f"/api/v1/workflows/{workflow['id']}/fail", json={"reason": "died"})
    lease = other_client.post(
        f"/api/v1/workflows/{workflow['id']}/claims", json={"lease_seconds": 60}
    ).json()["lease_token"]

    for path in (
        f"/api/v1/workflows/{workflow['id']}",
        f"/api/v1/workflows/{workflow['id']}/recovery-package",
        f"/api/v1/workflows/{workflow['id']}/events",
        f"/api/v1/workflows/{workflow['id']}/claims/active",
        "/api/v1/recoverable-workflows",
        "/api/v1/stats",
    ):
        response = client.get(path)
        assert lease not in response.text, f"lease token leaked from {path}"
