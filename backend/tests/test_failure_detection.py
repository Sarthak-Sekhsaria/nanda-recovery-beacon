"""Heartbeat-timeout detection, lease expiry and the reaper's distributed safety."""

from __future__ import annotations

import time

from app.db import SessionLocal
from app.reaper import run_sweep, sweep_once
from tests.conftest import claim, create_workflow, make_recoverable


def _sweep() -> None:
    """Run one sweep in its own transaction, as the worker does."""
    run_sweep()


def test_silent_workflow_becomes_suspected_then_recoverable(client):
    workflow = create_workflow(client, heartbeat_timeout_seconds=1)
    wid = workflow["id"]

    time.sleep(1.2)
    _sweep()
    assert client.get(f"/api/v1/workflows/{wid}").json()["status"] == "suspected_failed"

    # SUSPECT_GRACE_SECONDS is 1 in the test environment.
    time.sleep(1.2)
    _sweep()
    body = client.get(f"/api/v1/workflows/{wid}").json()
    assert body["status"] == "recoverable"
    assert body["failed_at"] is not None

    events = [e["event_type"] for e in client.get(f"/api/v1/workflows/{wid}/events").json()["items"]]
    assert "failure_suspected" in events
    assert "workflow_made_recoverable" in events


def test_a_heartbeat_revives_a_suspected_workflow(client):
    workflow = create_workflow(client, heartbeat_timeout_seconds=1)
    wid = workflow["id"]

    time.sleep(1.2)
    _sweep()
    assert client.get(f"/api/v1/workflows/{wid}").json()["status"] == "suspected_failed"

    response = client.post(f"/api/v1/workflows/{wid}/heartbeats", json={})
    assert response.status_code == 200
    assert response.json()["status"] == "active"


def test_a_checkpoint_is_also_proof_of_life(client):
    from tests.conftest import GOOD_CHECKPOINT

    workflow = create_workflow(client, heartbeat_timeout_seconds=1)
    wid = workflow["id"]
    time.sleep(1.2)
    _sweep()

    response = client.post(
        f"/api/v1/workflows/{wid}/checkpoints", json=dict(GOOD_CHECKPOINT, parent_version=1)
    )
    assert response.status_code == 201
    assert client.get(f"/api/v1/workflows/{wid}").json()["status"] == "active"


def test_dead_letter_policy_skips_the_recovery_queue(client):
    workflow = create_workflow(client, heartbeat_timeout_seconds=1, failure_policy="dead_letter")
    wid = workflow["id"]

    time.sleep(1.2)
    _sweep()
    time.sleep(1.2)
    _sweep()

    assert client.get(f"/api/v1/workflows/{wid}").json()["status"] == "dead_letter"


def test_the_reaper_expires_abandoned_leases(client, other_client):
    workflow = create_workflow(client)
    make_recoverable(client, workflow["id"])
    wid = workflow["id"]

    claim(other_client, wid, lease_seconds=1)
    assert client.get(f"/api/v1/workflows/{wid}").json()["status"] == "claimed"

    time.sleep(1.2)
    _sweep()

    body = client.get(f"/api/v1/workflows/{wid}").json()
    assert body["status"] == "recoverable"
    assert body["current_agent_id"] is None

    events = [e["event_type"] for e in client.get(f"/api/v1/workflows/{wid}/events").json()["items"]]
    assert "claim_expired" in events


def test_only_one_instance_sweeps_at_a_time(client):
    """The advisory lock makes a second concurrent sweep a no-op, not a duplicate."""
    holder = SessionLocal()
    try:
        # Hold the reaper's advisory lock in an open transaction.
        from app.db import REAPER_LOCK_KEY, try_advisory_lock

        assert try_advisory_lock(holder, REAPER_LOCK_KEY) is True

        result = run_sweep()
        assert result.skipped_locked is True
    finally:
        holder.rollback()  # releases the transaction-scoped lock
        holder.close()

    # With the lock free, the sweep runs normally.
    assert run_sweep().skipped_locked is False


def test_sweep_is_idempotent(client):
    create_workflow(client, heartbeat_timeout_seconds=1)
    time.sleep(1.2)

    first = run_sweep()
    second = run_sweep()

    assert first.suspected == 1
    assert second.suspected == 0


def test_recoverable_queue_reflects_expired_leases_without_a_worker(client, other_client):
    """GET /recoverable-workflows performs an opportunistic sweep."""
    workflow = create_workflow(client)
    make_recoverable(client, workflow["id"])
    claim(other_client, workflow["id"], lease_seconds=1)

    time.sleep(1.2)
    queue = client.get("/api/v1/recoverable-workflows").json()

    assert any(item["workflow"]["id"] == workflow["id"] for item in queue["items"])


def test_sweep_once_reports_counts(client, db):
    create_workflow(client, heartbeat_timeout_seconds=1)
    time.sleep(1.2)

    result = sweep_once(db)
    db.commit()

    assert result.suspected == 1
    assert result.claims_expired == 0
