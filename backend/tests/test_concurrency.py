"""True concurrency tests.

These run real HTTP requests from multiple OS threads against a live uvicorn
server backed by a real PostgreSQL. They are the proof that the mutual-exclusion
guarantee is enforced by the database and not by a lucky interleaving.
"""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from tests.conftest import GOOD_CHECKPOINT, create_workflow, make_recoverable

pytestmark = pytest.mark.concurrency

RACERS = 8


def _race(fn, count: int) -> list:
    """Fire ``count`` copies of ``fn`` as simultaneously as the OS allows."""
    barrier = threading.Barrier(count)

    def runner(index: int):
        barrier.wait()
        return fn(index)

    with ThreadPoolExecutor(max_workers=count) as pool:
        return list(pool.map(runner, range(count)))


def test_exactly_one_of_many_simultaneous_claims_succeeds(base_url, client, agent_keys):
    """Eight agents race for one workflow. Exactly one wins; the rest get 409."""
    workflow = create_workflow(client)
    make_recoverable(client, workflow["id"])
    url = f"{base_url}/api/v1/workflows/{workflow['id']}/claims"

    keys = list(agent_keys.values())

    def attempt(index: int) -> httpx.Response:
        with httpx.Client(timeout=30.0) as racer:
            return racer.post(
                url,
                json={"lease_seconds": 60},
                headers={"Authorization": f"Bearer {keys[index % len(keys)]}"},
            )

    responses = _race(attempt, RACERS)
    statuses = sorted(response.status_code for response in responses)

    winners = [r for r in responses if r.status_code == 201]
    losers = [r for r in responses if r.status_code == 409]

    assert len(winners) == 1, f"expected exactly one winner, got statuses {statuses}"
    assert len(losers) == RACERS - 1, f"expected {RACERS - 1} conflicts, got {statuses}"

    for loser in losers:
        assert loser.json()["error"]["code"] in {"CLAIM_ALREADY_HELD", "WORKFLOW_NOT_RECOVERABLE"}

    # The winner holds a real, unique lease.
    lease = winners[0].json()
    assert lease["fencing_token"] == 1

    active = client.get(f"/api/v1/workflows/{workflow['id']}/claims/active").json()
    assert active["id"] == lease["claim"]["id"]
    assert active["status"] == "active"


def test_only_one_active_claim_row_can_exist(base_url, client, db, agent_keys):
    """Independent of HTTP: the partial unique index rejects a second active claim."""
    from sqlalchemy.exc import IntegrityError

    from app.models import Claim, ClaimStatus

    workflow = create_workflow(client)
    make_recoverable(client, workflow["id"])
    wid = uuid.UUID(workflow["id"])

    def make_claim(generation: int) -> Claim:
        from datetime import timedelta

        from app.db import utcnow

        return Claim(
            workflow_id=wid,
            agent_id=f"agent-{generation}",
            lease_token_hash="0" * 64,
            lease_token_prefix="lease_xxxxxx",
            lease_generation=generation,
            status=ClaimStatus.active,
            expires_at=utcnow() + timedelta(seconds=60),
        )

    db.add(make_claim(1))
    db.commit()

    db.add(make_claim(2))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_concurrent_checkpoint_writes_produce_no_version_gaps(base_url, client):
    """Only one writer can win each version; the losers see 409, never a duplicate."""
    workflow = create_workflow(client)
    wid = workflow["id"]
    url = f"{base_url}/api/v1/workflows/{wid}/checkpoints"
    api_key = client.headers["Authorization"]

    def attempt(index: int) -> httpx.Response:
        body = dict(GOOD_CHECKPOINT, parent_version=1, next_action=f"writer {index}")
        with httpx.Client(timeout=30.0) as racer:
            return racer.post(url, json=body, headers={"Authorization": api_key})

    responses = _race(attempt, RACERS)
    created = [r for r in responses if r.status_code == 201]
    rejected = [r for r in responses if r.status_code == 409]

    assert len(created) == 1
    assert len(rejected) == RACERS - 1
    assert all(r.json()["error"]["code"] == "STALE_CHECKPOINT_VERSION" for r in rejected)

    versions = [c["version"] for c in client.get(f"/api/v1/workflows/{wid}/checkpoints").json()["items"]]
    assert versions == [2, 1]


def test_concurrent_idempotent_creates_yield_one_workflow(base_url, client):
    url = f"{base_url}/api/v1/workflows"
    api_key = client.headers["Authorization"]
    payload = {"title": "Race to create", "objective": "Only one should exist"}
    headers = {"Authorization": api_key, "Idempotency-Key": "race-create"}

    def attempt(_: int) -> httpx.Response:
        with httpx.Client(timeout=30.0) as racer:
            return racer.post(url, json=payload, headers=headers)

    responses = _race(attempt, RACERS)
    assert all(r.status_code == 201 for r in responses)

    ids = {r.json()["id"] for r in responses}
    assert len(ids) == 1, f"idempotency key produced {len(ids)} distinct workflows"

    listed = client.get("/api/v1/workflows", params={"search": "Race to create"}).json()
    assert len(listed["items"]) == 1
