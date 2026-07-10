"""Failure detection and lease expiry.

Safety with multiple application instances
------------------------------------------
A sweep is guarded by ``pg_try_advisory_lock``. Exactly one instance performs a
given sweep; every other instance sees ``False`` and returns immediately. Nothing
depends on an in-memory timer being unique, so the service is correct whether it
runs as one process, four Gunicorn workers, or three Render instances.

Rows are selected ``FOR UPDATE SKIP LOCKED`` so a sweep never blocks on a
workflow that an API request is mutating at that moment; it is simply picked up
on the next pass.

Three transitions happen here:

1. ``active`` -> ``suspected_failed``  when now > last_heartbeat + heartbeat_timeout
2. ``suspected_failed`` -> ``recoverable`` (or ``dead_letter``) when the grace period
   also elapses
3. ``claims.active`` -> ``expired`` when now >= expires_at, returning the workflow
   to ``recoverable``

Run it in one of two ways:

* in-process (default):  RUN_REAPER_IN_API=true, started on application startup
* standalone worker:     ``python -m app.reaper --loop``   (see Makefile: make worker)
"""

from __future__ import annotations

import argparse
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import metrics
from app.config import settings
from app.db import REAPER_LOCK_KEY, advisory_lock, session_scope, utcnow
from app.logging_config import configure_logging, get_logger, log
from app.models import (
    Claim,
    ClaimStatus,
    EventType,
    FailurePolicy,
    Workflow,
    WorkflowStatus,
)
from app.services.events import record_event
from app.state_machine import transition

logger = get_logger(__name__)

_last_opportunistic_sweep: float = 0.0
_last_success_at: datetime | None = None


def last_success_at() -> datetime | None:
    """When the most recent sweep completed in this process, if any."""
    return _last_success_at


@dataclass
class SweepResult:
    claims_expired: int = 0
    suspected: int = 0
    made_recoverable: int = 0
    dead_lettered: int = 0
    skipped_locked: bool = False

    def as_dict(self) -> dict[str, int | bool]:
        return {
            "claims_expired": self.claims_expired,
            "suspected": self.suspected,
            "made_recoverable": self.made_recoverable,
            "dead_lettered": self.dead_lettered,
            "skipped_locked": self.skipped_locked,
        }


def _expire_claims(db: Session, now, limit: int) -> tuple[int, list[Workflow]]:
    expired_claims = (
        db.execute(
            select(Claim)
            .where(Claim.status == ClaimStatus.active, Claim.expires_at <= now)
            .order_by(Claim.expires_at.asc())
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        .scalars()
        .all()
    )

    touched: list[Workflow] = []
    for claim in expired_claims:
        claim.status = ClaimStatus.expired
        claim.released_at = now
        claim.release_reason = "lease_expired"
        metrics.claims_expired_total.inc()

        workflow = db.execute(
            select(Workflow).where(Workflow.id == claim.workflow_id).with_for_update()
        ).scalar_one()

        record_event(
            db,
            workflow_id=workflow.id,
            event_type=EventType.claim_expired,
            actor_agent_id=claim.agent_id,
            lease_generation=claim.lease_generation,
            metadata={"claim_id": str(claim.id), "expired_at": now.isoformat()},
        )

        if workflow.status in {WorkflowStatus.claimed, WorkflowStatus.active}:
            transition(workflow, WorkflowStatus.recoverable, reason="lease_expired")
            workflow.current_agent_id = None
            workflow.failed_at = workflow.failed_at or now
            record_event(
                db,
                workflow_id=workflow.id,
                event_type=EventType.workflow_made_recoverable,
                metadata={"trigger": "lease_expired"},
            )
            metrics.workflows_made_recoverable_total.inc()
            touched.append(workflow)

    return len(expired_claims), touched


def _suspect_silent_workflows(db: Session, now, limit: int) -> int:
    candidates = (
        db.execute(
            select(Workflow)
            .where(Workflow.status == WorkflowStatus.active)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        .scalars()
        .all()
    )
    count = 0
    for workflow in candidates:
        deadline = workflow.last_heartbeat_at + timedelta(seconds=workflow.heartbeat_timeout_seconds)
        if now <= deadline:
            continue
        transition(workflow, WorkflowStatus.suspected_failed, reason="heartbeat_timeout")
        workflow.failed_at = workflow.failed_at or now
        record_event(
            db,
            workflow_id=workflow.id,
            event_type=EventType.failure_suspected,
            checkpoint_version=workflow.current_checkpoint_version,
            metadata={
                "trigger": "heartbeat_timeout",
                "last_heartbeat_at": workflow.last_heartbeat_at.isoformat(),
                "heartbeat_timeout_seconds": workflow.heartbeat_timeout_seconds,
            },
        )
        metrics.failures_detected_total.labels(reason="heartbeat_timeout").inc()
        count += 1
    return count


def _promote_suspected(db: Session, now, limit: int) -> tuple[int, int]:
    candidates = (
        db.execute(
            select(Workflow)
            .where(Workflow.status == WorkflowStatus.suspected_failed)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        .scalars()
        .all()
    )
    recoverable = dead = 0
    for workflow in candidates:
        # The grace period is measured from when suspicion began (failed_at), not
        # from the last heartbeat. So a workflow suspected in *this* sweep is never
        # promoted in the same sweep: it gets a full grace window as suspected_failed
        # before becoming recoverable, giving a slow-but-alive agent time to heartbeat.
        suspected_since = workflow.failed_at or workflow.last_heartbeat_at
        deadline = suspected_since + timedelta(seconds=settings.suspect_grace_seconds)
        if now <= deadline:
            continue

        exhausted = workflow.recovery_count >= workflow.max_recoveries
        if workflow.failure_policy == FailurePolicy.dead_letter or exhausted:
            transition(workflow, WorkflowStatus.dead_letter, reason="heartbeat_timeout")
            workflow.current_agent_id = None
            record_event(
                db,
                workflow_id=workflow.id,
                event_type=EventType.workflow_dead_lettered,
                metadata={
                    "trigger": "heartbeat_timeout",
                    "reason": "max_recoveries_exhausted" if exhausted else "failure_policy",
                },
            )
            metrics.dead_lettered_total.inc()
            dead += 1
            continue

        transition(workflow, WorkflowStatus.recoverable, reason="heartbeat_timeout")
        workflow.current_agent_id = None
        workflow.failed_at = workflow.failed_at or now
        record_event(
            db,
            workflow_id=workflow.id,
            event_type=EventType.workflow_made_recoverable,
            checkpoint_version=workflow.current_checkpoint_version,
            metadata={"trigger": "heartbeat_timeout"},
        )
        metrics.workflows_made_recoverable_total.inc()
        recoverable += 1
    return recoverable, dead


def sweep_once(db: Session) -> SweepResult:
    """Run one full detection pass. Caller owns the transaction."""
    now = utcnow()
    limit = settings.reaper_batch_size
    result = SweepResult()

    result.claims_expired, _ = _expire_claims(db, now, limit)
    result.suspected = _suspect_silent_workflows(db, now, limit)
    result.made_recoverable, result.dead_lettered = _promote_suspected(db, now, limit)
    return result


def run_sweep() -> SweepResult:
    """Acquire the advisory lock and sweep. Returns immediately if another instance holds it."""
    global _last_success_at
    started = time.perf_counter()
    with session_scope() as db, advisory_lock(db, REAPER_LOCK_KEY) as acquired:
        if not acquired:
            return SweepResult(skipped_locked=True)
        result = sweep_once(db)
        db.commit()

    duration = time.perf_counter() - started
    metrics.reaper_sweep_seconds.observe(duration)
    metrics.reaper_last_success_timestamp.set(time.time())
    _last_success_at = utcnow()
    if any([result.claims_expired, result.suspected, result.made_recoverable, result.dead_lettered]):
        log(logger, logging.INFO, "reaper.sweep", duration_seconds=round(duration, 4), **result.as_dict())
    return result


def maybe_sweep() -> SweepResult:
    """Rate-limited sweep triggered by reads.

    Keeps failure detection working on hosting plans where a background worker is
    not available and the web process sleeps when idle. Never runs more than once
    per ``OPPORTUNISTIC_SWEEP_MIN_INTERVAL_SECONDS``.
    """
    global _last_opportunistic_sweep
    now = time.monotonic()
    if now - _last_opportunistic_sweep < settings.opportunistic_sweep_min_interval_seconds:
        return SweepResult(skipped_locked=True)
    _last_opportunistic_sweep = now
    try:
        return run_sweep()
    except Exception:  # pragma: no cover - never fail a read because of the reaper
        logger.exception("opportunistic sweep failed")
        return SweepResult(skipped_locked=True)


def run_forever(interval_seconds: int | None = None) -> None:  # pragma: no cover - loop
    interval = interval_seconds or settings.reaper_interval_seconds
    log(logger, logging.INFO, "reaper.start", interval_seconds=interval)
    while True:
        try:
            run_sweep()
        except Exception:
            logger.exception("reaper sweep failed; retrying next tick")
        time.sleep(interval)


def main() -> None:  # pragma: no cover - CLI
    parser = argparse.ArgumentParser(description="NANDA Recovery Beacon failure detector")
    parser.add_argument("--loop", action="store_true", help="Run continuously (production worker).")
    parser.add_argument("--interval", type=int, default=None, help="Seconds between sweeps.")
    args = parser.parse_args()

    configure_logging()
    if args.loop:
        run_forever(args.interval)
    else:
        result = run_sweep()
        print(result.as_dict())


if __name__ == "__main__":  # pragma: no cover
    main()
