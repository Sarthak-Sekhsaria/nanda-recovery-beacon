"""Append-only audit trail."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.logging_config import redact
from app.models import EventType, RecoveryEvent


def record_event(
    db: Session,
    *,
    workflow_id: uuid.UUID,
    event_type: EventType,
    actor_agent_id: str | None = None,
    request_id: str | None = None,
    checkpoint_version: int | None = None,
    lease_generation: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> RecoveryEvent:
    """Append an audit record inside the caller's transaction.

    The event is flushed but not committed: audit records and the state change
    they describe commit together, or not at all.
    """
    event = RecoveryEvent(
        workflow_id=workflow_id,
        event_type=event_type,
        actor_agent_id=actor_agent_id,
        request_id=request_id,
        checkpoint_version=checkpoint_version,
        lease_generation=lease_generation,
        meta=redact(metadata or {}),
    )
    db.add(event)
    db.flush()
    return event
