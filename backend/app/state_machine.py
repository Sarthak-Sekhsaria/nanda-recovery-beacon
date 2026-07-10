"""Workflow status transitions.

Status is never assigned directly by a route handler. Every change goes through
:func:`transition`, which rejects any edge not present in ``ALLOWED``.

    active ──heartbeat timeout──▶ suspected_failed ──grace elapsed──▶ recoverable
      │                                  │                                 │
      │ explicit fail                    │ heartbeat                       │ claim
      ▼                                  ▼                                 ▼
    recoverable ◀──lease expired/released── claimed ──resume──▶ active   claimed
      │                                                                    │
      └── max recoveries exceeded ─▶ dead_letter          complete ────────┘
                                                                  ▼
                                                              completed
"""

from __future__ import annotations

from app.errors import InvalidStateTransition
from app.models import Workflow, WorkflowStatus

S = WorkflowStatus

ALLOWED: dict[WorkflowStatus, set[WorkflowStatus]] = {
    S.active: {S.suspected_failed, S.recoverable, S.completed, S.cancelled, S.dead_letter},
    S.suspected_failed: {S.active, S.recoverable, S.cancelled, S.dead_letter},
    S.recoverable: {S.claimed, S.cancelled, S.dead_letter},
    # `claimed -> recoverable` happens when the lease expires or is released.
    # `claimed -> active` happens on resume.
    S.claimed: {S.active, S.recoverable, S.completed, S.cancelled, S.dead_letter},
    S.completed: set(),
    S.cancelled: set(),
    S.dead_letter: {S.recoverable},  # only via an explicit admin re-open
}

TERMINAL: frozenset[WorkflowStatus] = frozenset({S.completed, S.cancelled})

#: Statuses in which the workflow's current agent may submit progress.
WRITABLE: frozenset[WorkflowStatus] = frozenset({S.active, S.suspected_failed, S.claimed})

#: Statuses from which a replacement agent may take over.
CLAIMABLE: frozenset[WorkflowStatus] = frozenset({S.recoverable})


def can_transition(current: WorkflowStatus, target: WorkflowStatus) -> bool:
    if current == target:
        return True
    return target in ALLOWED.get(current, set())


def transition(workflow: Workflow, target: WorkflowStatus, *, reason: str = "") -> None:
    """Move ``workflow`` to ``target`` or raise ``InvalidStateTransition``."""
    current = workflow.status
    if current == target:
        return
    if target not in ALLOWED.get(current, set()):
        raise InvalidStateTransition(
            f"Cannot move workflow from '{current.value}' to '{target.value}'.",
            details={
                "current_status": current.value,
                "requested_status": target.value,
                "allowed_next": sorted(s.value for s in ALLOWED.get(current, set())),
                "reason": reason,
            },
        )
    workflow.status = target
