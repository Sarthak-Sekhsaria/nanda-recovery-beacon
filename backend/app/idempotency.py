"""Idempotency-Key support for mutating requests.

Contract
--------
* Send ``Idempotency-Key: <unique string>`` on any POST.
* The first request executes and its response is stored **in the same transaction**
  as the work it performed. Either both land or neither does.
* A repeat with the same key and the same body returns the original response
  verbatim, with ``Idempotent-Replay: true``.
* A repeat with the same key and a *different* body is rejected with
  409 IDEMPOTENCY_KEY_REUSED. Keys are scoped per agent and per path.

This is what makes ``POST /complete`` safe to retry after a network timeout: the
second attempt replays the stored 200 rather than hitting WORKFLOW_ALREADY_COMPLETED.
"""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.errors import IdempotencyKeyReused
from app.models import IdempotencyRecord
from app.util import checksum

HEADER = "Idempotency-Key"
REPLAY_HEADER = "Idempotent-Replay"


def key_from(request: Request) -> str | None:
    value = request.headers.get(HEADER)
    return value.strip()[:255] if value else None


class IdempotencyGuard:
    """Ties an optional Idempotency-Key to one request/response pair."""

    def __init__(
        self,
        db: Session,
        *,
        agent_id: str,
        endpoint: str,
        key: str | None,
        request_body: Any,
    ) -> None:
        self.db = db
        self.agent_id = agent_id
        self.endpoint = endpoint
        self.key = key
        self.request_hash = checksum(request_body) if key else ""

    @property
    def enabled(self) -> bool:
        return bool(self.key)

    def _lookup(self) -> IdempotencyRecord | None:
        return self.db.execute(
            select(IdempotencyRecord).where(
                IdempotencyRecord.agent_id == self.agent_id,
                IdempotencyRecord.endpoint == self.endpoint,
                IdempotencyRecord.idempotency_key == self.key,
            )
        ).scalar_one_or_none()

    def replay(self) -> JSONResponse | None:
        """Return the stored response for a repeated request, if there is one."""
        if not self.enabled:
            return None
        record = self._lookup()
        if record is None:
            return None
        if record.request_hash != self.request_hash:
            raise IdempotencyKeyReused(
                details={"endpoint": self.endpoint, "idempotency_key": self.key}
            )
        return JSONResponse(
            content=record.response_body,
            status_code=record.response_status,
            headers={REPLAY_HEADER: "true"},
        )

    def commit(self, status_code: int, payload: dict) -> JSONResponse:
        """Persist the work *and* the response atomically, then return the response."""
        if self.enabled:
            self.db.add(
                IdempotencyRecord(
                    agent_id=self.agent_id,
                    endpoint=self.endpoint,
                    idempotency_key=self.key,
                    request_hash=self.request_hash,
                    response_status=status_code,
                    response_body=payload,
                )
            )
        try:
            self.db.commit()
        except IntegrityError:
            # Another request with the same key committed while we were working.
            # Discard our work and serve the winner's response.
            self.db.rollback()
            if not self.enabled:
                raise
            record = self._lookup()
            if record is None:
                raise
            if record.request_hash != self.request_hash:
                raise IdempotencyKeyReused(
                    details={"endpoint": self.endpoint, "idempotency_key": self.key}
                ) from None
            return JSONResponse(
                content=record.response_body,
                status_code=record.response_status,
                headers={REPLAY_HEADER: "true"},
            )

        headers = {REPLAY_HEADER: "false"} if self.enabled else None
        return JSONResponse(content=payload, status_code=status_code, headers=headers)
