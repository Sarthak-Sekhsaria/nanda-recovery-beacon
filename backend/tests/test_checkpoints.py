"""Checkpoint versioning, immutability and optimistic concurrency."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from tests.conftest import GOOD_CHECKPOINT, create_workflow, error_code

# The append-only trigger raises with SQLSTATE 'restrict_violation' (class 23), which
# SQLAlchemy surfaces as IntegrityError -- a subclass of DBAPIError. Catch the base so
# the assertion does not depend on the exact SQLSTATE-to-exception mapping.


def _checkpoint(parent_version: int, **overrides) -> dict:
    body = dict(GOOD_CHECKPOINT, parent_version=parent_version)
    body.update(overrides)
    return body


def test_checkpoint_versions_increase_monotonically(client):
    workflow = create_workflow(client)
    wid = workflow["id"]

    second = client.post(f"/api/v1/workflows/{wid}/checkpoints", json=_checkpoint(1))
    third = client.post(f"/api/v1/workflows/{wid}/checkpoints", json=_checkpoint(2))

    assert second.status_code == 201
    assert second.json()["version"] == 2
    assert second.json()["parent_version"] == 1
    assert third.json()["version"] == 3

    workflow = client.get(f"/api/v1/workflows/{wid}").json()
    assert workflow["current_checkpoint_version"] == 3
    assert workflow["checkpoint_count"] == 3


def test_stale_parent_version_is_rejected_and_audited(client):
    workflow = create_workflow(client)
    wid = workflow["id"]
    client.post(f"/api/v1/workflows/{wid}/checkpoints", json=_checkpoint(1))

    # Version 2 exists now; writing with parent_version=1 is a lost update.
    response = client.post(f"/api/v1/workflows/{wid}/checkpoints", json=_checkpoint(1))

    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "STALE_CHECKPOINT_VERSION"
    assert body["error"]["details"]["current_checkpoint_version"] == 2

    events = client.get(f"/api/v1/workflows/{wid}/events").json()["items"]
    assert any(event["event_type"] == "stale_update_rejected" for event in events)

    # The rejected write did not create a version.
    assert client.get(f"/api/v1/workflows/{wid}").json()["current_checkpoint_version"] == 2


def test_checkpoints_are_immutable_at_the_database_level(client, db):
    workflow = create_workflow(client)
    wid = workflow["id"]

    with pytest.raises(DBAPIError) as excinfo:
        db.execute(
            text("UPDATE checkpoints SET objective = 'tampered' WHERE workflow_id = :wid"),
            {"wid": wid},
        )
        db.commit()
    assert "append_only_violation" in str(excinfo.value)
    db.rollback()

    with pytest.raises(DBAPIError):
        db.execute(text("DELETE FROM checkpoints WHERE workflow_id = :wid"), {"wid": wid})
        db.commit()
    db.rollback()

    stored = client.get(f"/api/v1/workflows/{wid}/checkpoints/1").json()
    assert stored["objective"] == GOOD_CHECKPOINT["objective"]


def test_recovery_events_are_immutable(client, db):
    workflow = create_workflow(client)

    with pytest.raises(DBAPIError):
        db.execute(
            text("DELETE FROM recovery_events WHERE workflow_id = :wid"), {"wid": workflow["id"]}
        )
        db.commit()
    db.rollback()


def test_checkpoint_content_checksum_is_deterministic(client):
    first = create_workflow(client)
    second = create_workflow(client, title="Another workflow")

    a = client.get(f"/api/v1/workflows/{first['id']}/checkpoints/1").json()
    b = client.get(f"/api/v1/workflows/{second['id']}/checkpoints/1").json()

    # Same content at the same version hashes identically, regardless of workflow.
    assert a["content_checksum"] == b["content_checksum"]
    assert len(a["content_checksum"]) == 64


def test_unsupported_schema_version_is_422(client):
    workflow = create_workflow(client)
    response = client.post(
        f"/api/v1/workflows/{workflow['id']}/checkpoints",
        json=_checkpoint(1, schema_version="9.9"),
    )
    assert response.status_code == 422
    assert error_code(response) == "UNSUPPORTED_SCHEMA_VERSION"


def test_checkpoint_diff_reports_progress(client):
    workflow = create_workflow(client)
    wid = workflow["id"]
    client.post(
        f"/api/v1/workflows/{wid}/checkpoints",
        json=_checkpoint(
            1,
            completed_steps=[*GOOD_CHECKPOINT["completed_steps"], "Compare deadlines"],
            remaining_steps=["Produce recommendation"],
        ),
    )

    diff = client.get(f"/api/v1/workflows/{wid}/checkpoints/2/diff").json()["diff"]
    assert diff["steps_completed_since_parent"] == ["Compare deadlines"]
    assert diff["steps_removed_from_remaining"] == ["Compare deadlines"]


def test_checkpoint_listing_is_newest_first(client):
    workflow = create_workflow(client)
    wid = workflow["id"]
    client.post(f"/api/v1/workflows/{wid}/checkpoints", json=_checkpoint(1))

    versions = [c["version"] for c in client.get(f"/api/v1/workflows/{wid}/checkpoints").json()["items"]]
    assert versions == [2, 1]


def test_idempotent_checkpoint_creation_does_not_double_version(client):
    workflow = create_workflow(client)
    wid = workflow["id"]
    headers = {"Idempotency-Key": "checkpoint-2"}

    first = client.post(f"/api/v1/workflows/{wid}/checkpoints", json=_checkpoint(1), headers=headers)
    second = client.post(f"/api/v1/workflows/{wid}/checkpoints", json=_checkpoint(1), headers=headers)

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    assert client.get(f"/api/v1/workflows/{wid}").json()["current_checkpoint_version"] == 2


def test_missing_parent_version_is_a_schema_error(client):
    workflow = create_workflow(client)
    body = dict(GOOD_CHECKPOINT)  # no parent_version
    response = client.post(f"/api/v1/workflows/{workflow['id']}/checkpoints", json=body)

    assert response.status_code == 422
    assert error_code(response) == "SCHEMA_VALIDATION_FAILED"
