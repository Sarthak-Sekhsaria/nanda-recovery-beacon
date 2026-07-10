"""Seed realistic sample workflows.

    python -m app.cli seed [--reset]

Creates one workflow in each interesting state so the dashboard and the recovery
queue have something truthful to show:

* an active workflow that is heartbeating normally
* a recoverable workflow abandoned mid-research (rich context, safe to resume)
* a recoverable workflow with blocking context issues (unsafe to resume)
* a claimed workflow currently held by a replacement agent
* a completed workflow with a full recovery history
* a dead-lettered workflow whose failure policy forbids recovery
"""

from __future__ import annotations

import sys

from sqlalchemy import delete, select, text

from app.db import session_scope, utcnow
from app.models import (
    ApiKey,
    Artifact,
    Checkpoint,
    Claim,
    FailurePolicy,
    Priority,
    RecoveryEvent,
    VerificationStatus,
    Workflow,
)
from app.schemas import CheckpointBody, Decision, WorkflowCreate
from app.security import create_api_key
from app.services import checkpoints as checkpoint_service
from app.services import claims as claim_service
from app.services import workflows as workflow_service

SEED_TAG = "seed"

RESEARCHER = "research-agent-1"
REPLACEMENT = "research-agent-2"
INGESTOR = "ingest-agent-1"


def _checkpoint(**overrides) -> CheckpointBody:
    base = {
        "objective": "Compare five scholarship programs and recommend one",
        "completed_steps": ["Found five programs", "Collected eligibility requirements"],
        "remaining_steps": ["Compare deadlines", "Produce recommendation"],
        "decisions": [
            Decision(
                decision="Only include programs open to international students",
                reason="Required by the original request from the user",
            )
        ],
        "next_action": "Compare application deadlines across the five programs",
        "context_summary": (
            "Five scholarship programs were identified from the official directory. "
            "Eligibility requirements were collected for all five and stored in "
            "programs.json. Deadlines have not been compared yet."
        ),
        "variables": {"source": "https://example.com/programs", "programs_found": 5},
    }
    base.update(overrides)
    return CheckpointBody(**base)


def _create(db, agent: str, **kwargs) -> Workflow:
    payload = WorkflowCreate(**kwargs)
    return workflow_service.create_workflow(
        db, payload=payload, agent_id=agent, idempotency_key=None, request_id="seed"
    )


def _reset_seed_data(db) -> None:
    """Remove previously seeded rows. Append-only triggers are disabled just for this."""
    ids = list(db.execute(select(Workflow.id).where(Workflow.tags.contains([SEED_TAG]))).scalars().all())
    if not ids:
        return
    db.execute(delete(Artifact).where(Artifact.workflow_id.in_(ids)))
    db.execute(delete(Claim).where(Claim.workflow_id.in_(ids)))
    # checkpoints and recovery_events are append-only: disable the triggers for cleanup.
    db.execute(text("ALTER TABLE checkpoints DISABLE TRIGGER trg_checkpoints_append_only"))
    db.execute(text("ALTER TABLE recovery_events DISABLE TRIGGER trg_recovery_events_append_only"))
    db.execute(delete(Checkpoint).where(Checkpoint.workflow_id.in_(ids)))
    db.execute(delete(RecoveryEvent).where(RecoveryEvent.workflow_id.in_(ids)))
    db.execute(text("ALTER TABLE checkpoints ENABLE TRIGGER trg_checkpoints_append_only"))
    db.execute(text("ALTER TABLE recovery_events ENABLE TRIGGER trg_recovery_events_append_only"))
    db.execute(delete(Workflow).where(Workflow.id.in_(ids)))
    db.flush()
    print(f"removed {len(ids)} seeded workflows")


def ensure_keys(db) -> dict[str, str]:
    created: dict[str, str] = {}
    for agent, is_admin in ((RESEARCHER, False), (REPLACEMENT, False), ("admin", True)):
        exists = db.execute(select(ApiKey).where(ApiKey.agent_id == agent)).scalar_one_or_none()
        if exists is None:
            created[agent] = create_api_key(db, agent_id=agent, label="seed", is_admin=is_admin)
    return created


def seed(*, reset: bool = False) -> None:
    with session_scope() as db:
        if reset:
            _reset_seed_data(db)

        keys = ensure_keys(db)

        # 1. Active, healthy.
        active = _create(
            db,
            RESEARCHER,
            title="Summarise Q3 grant applications",
            objective="Summarise every Q3 grant application and flag incomplete ones",
            priority=Priority.normal,
            tags=[SEED_TAG, "summarisation"],
            heartbeat_timeout_seconds=300,
            initial_checkpoint=_checkpoint(
                objective="Summarise every Q3 grant application and flag incomplete ones",
                completed_steps=["Downloaded 42 applications"],
                remaining_steps=["Summarise each application", "Flag incomplete submissions"],
                next_action="Summarise application 1 of 42",
                context_summary="42 applications downloaded from the grants portal. None summarised yet.",
            ),
        )

        # 2. Recoverable with rich context -- the happy recovery path.
        recoverable = _create(
            db,
            RESEARCHER,
            title="Scholarship comparison",
            objective="Compare five scholarship programs and recommend one",
            priority=Priority.high,
            tags=[SEED_TAG, "research", "scholarships"],
            heartbeat_timeout_seconds=60,
            initial_checkpoint=_checkpoint(),
        )
        db.add(
            Artifact(
                workflow_id=recoverable.id,
                name="programs.json",
                uri="https://raw.githubusercontent.com/nanda-recovery-beacon/fixtures/main/programs.json",
                sha256="9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
                content_type="application/json",
                description="Five candidate programs with eligibility data",
                checkpoint_version=1,
                produced_by_agent_id=RESEARCHER,
                verification_status=VerificationStatus.unverified,
            )
        )
        db.flush()
        workflow_service.report_failure(
            db,
            workflow_id=recoverable.id,
            agent_id=RESEARCHER,
            reason="Upstream scholarship API returned 503 five times in a row",
            lease_token=None,
            details={"attempts": 5, "last_status": 503},
            request_id="seed",
        )

        # 3. Recoverable but unsafe: no next_action, contradictory steps.
        blocked = _create(
            db,
            INGESTOR,
            title="Ingest partner dataset",
            objective="Ingest the partner dataset and normalise column names",
            priority=Priority.critical,
            tags=[SEED_TAG, "ingest"],
            heartbeat_timeout_seconds=30,
            initial_checkpoint=CheckpointBody(
                objective="Ingest the partner dataset and normalise column names",
                completed_steps=["Download dataset", "Normalise column names"],
                remaining_steps=["Normalise column names"],  # contradicts completed_steps
                decisions=[],
                next_action=None,  # blocking: no entry point
                context_summary=None,  # warning
                variables={},
            ),
        )
        workflow_service.report_failure(
            db,
            workflow_id=blocked.id,
            agent_id=INGESTOR,
            reason="Process killed by the container runtime (OOM)",
            lease_token=None,
            request_id="seed",
        )

        # 4. Claimed by a replacement agent right now.
        claimed = _create(
            db,
            RESEARCHER,
            title="Draft literature review",
            objective="Draft a literature review of retrieval-augmented generation papers",
            priority=Priority.normal,
            tags=[SEED_TAG, "writing"],
            heartbeat_timeout_seconds=60,
            initial_checkpoint=_checkpoint(
                objective="Draft a literature review of retrieval-augmented generation papers",
                completed_steps=["Collected 18 papers", "Grouped papers by method"],
                remaining_steps=["Write the comparison section"],
                next_action="Write the comparison section covering dense vs sparse retrieval",
                context_summary="18 papers collected and grouped. Comparison section not written.",
            ),
        )
        workflow_service.report_failure(
            db,
            workflow_id=claimed.id,
            agent_id=RESEARCHER,
            reason="Agent shut down during a deployment",
            lease_token=None,
            request_id="seed",
        )
        claim_service.acquire(
            db,
            workflow_id=claimed.id,
            agent_id=REPLACEMENT,
            lease_seconds=900,
            acknowledge_blocking_issues=False,
            request_id="seed",
            note="Picked up from the recovery queue",
        )

        # 5. Completed after a real recovery.
        completed = _create(
            db,
            RESEARCHER,
            title="Verify citation links",
            objective="Verify that every citation link in the report resolves",
            priority=Priority.low,
            tags=[SEED_TAG, "verification"],
            heartbeat_timeout_seconds=60,
            initial_checkpoint=_checkpoint(
                objective="Verify that every citation link in the report resolves",
                completed_steps=["Extracted 63 citation links"],
                remaining_steps=["Check each link", "Report broken links"],
                next_action="Check link 1 of 63",
                context_summary="63 links extracted from the report. None checked yet.",
            ),
        )
        workflow_service.report_failure(
            db,
            workflow_id=completed.id,
            agent_id=RESEARCHER,
            reason="Network egress blocked",
            lease_token=None,
            request_id="seed",
        )
        _, token, _ = claim_service.acquire(
            db,
            workflow_id=completed.id,
            agent_id=REPLACEMENT,
            lease_seconds=900,
            acknowledge_blocking_issues=False,
            request_id="seed",
        )
        claim_service.resume(
            db,
            workflow_id=completed.id,
            agent_id=REPLACEMENT,
            lease_token=token,
            request_id="seed",
        )
        workflow = claim_service.get_workflow_for_update(db, completed.id)
        active_claim = claim_service.get_active_claim(db, completed.id)
        checkpoint_service.create_checkpoint(
            db,
            workflow=workflow,
            body=_checkpoint(
                objective="Verify that every citation link in the report resolves",
                completed_steps=[
                    "Extracted 63 citation links",
                    "Checked each link",
                    "Reported 4 broken links",
                ],
                remaining_steps=[],
                next_action="Nothing remains; ready to complete",
                context_summary="All 63 links checked. Four are broken and were reported.",
                decisions=[
                    Decision(
                        decision="Treat 301 redirects as valid links",
                        reason="A permanent redirect still resolves for a human reader",
                    )
                ],
            ),
            parent_version=1,
            agent_id=REPLACEMENT,
            claim=active_claim,
            request_id="seed",
        )
        workflow_service.complete_workflow(
            db,
            workflow_id=completed.id,
            agent_id=REPLACEMENT,
            lease_token=token,
            final_checkpoint_version=2,
            summary="63 links checked, 4 broken links reported.",
            request_id="seed",
        )

        # 6. Dead letter: failure policy forbids recovery.
        dead = _create(
            db,
            INGESTOR,
            title="Delete stale customer records",
            objective="Delete customer records older than the retention window",
            priority=Priority.high,
            failure_policy=FailurePolicy.dead_letter,
            tags=[SEED_TAG, "destructive"],
            heartbeat_timeout_seconds=60,
            initial_checkpoint=_checkpoint(
                objective="Delete customer records older than the retention window",
                completed_steps=["Identified 1,204 candidate records"],
                remaining_steps=["Delete records", "Write deletion audit log"],
                next_action="Delete the first batch of 100 records",
                context_summary="Candidates identified. Nothing deleted yet.",
            ),
        )
        workflow_service.report_failure(
            db,
            workflow_id=dead.id,
            agent_id=INGESTOR,
            reason="Refused to continue: deletion is not idempotent and must not be retried blindly",
            lease_token=None,
            request_id="seed",
        )

        print(f"seeded at {utcnow().isoformat()}")
        for name, workflow in [
            ("active", active),
            ("recoverable", recoverable),
            ("recoverable (blocked)", blocked),
            ("claimed", claimed),
            ("completed", completed),
            ("dead_letter", dead),
        ]:
            print(f"  {name:<22} {workflow.id}")

    if keys:
        print("\nAPI keys created (store them now, they are not recoverable):")
        for agent, raw in keys.items():
            print(f"  {agent:<20} {raw}")


if __name__ == "__main__":
    seed(reset="--reset" in sys.argv)
