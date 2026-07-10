"""Shared FastAPI dependencies: authentication, pagination, request context."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Query, Request
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db, utcnow
from app.errors import AdminRequired, Unauthenticated
from app.logging_config import agent_id_ctx
from app.security import lookup_api_key

AGENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:@\-]{1,128}$")


@dataclass(frozen=True)
class Agent:
    agent_id: str
    is_admin: bool
    authenticated: bool


def _bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization")
    if header and header.lower().startswith("bearer "):
        return header[7:].strip()
    return request.headers.get("x-api-key")


def get_current_agent(
    request: Request, db: Annotated[Session, Depends(get_db)]
) -> Agent:
    """Identify the calling agent from its API key.

    In DEMO_MODE the service accepts unauthenticated calls and attributes them to
    the agent named in ``X-Agent-Id``. That exists so a hackathon judge can drive
    the API from curl without provisioning a key; it must never be enabled for a
    deployment holding real work.
    """
    token = _bearer_token(request)

    if token:
        api_key = lookup_api_key(db, token)
        if api_key is None:
            raise Unauthenticated()
        # Cheap liveness stamp: at most one write per key per minute.
        now = utcnow()
        if api_key.last_used_at is None or (now - api_key.last_used_at).total_seconds() > 60:
            api_key.last_used_at = now
            db.commit()
        agent_id_ctx.set(api_key.agent_id)
        return Agent(agent_id=api_key.agent_id, is_admin=api_key.is_admin, authenticated=True)

    if settings.demo_mode:
        raw = request.headers.get("x-agent-id", settings.demo_agent_id).strip()
        agent_id = raw if AGENT_ID_PATTERN.match(raw) else settings.demo_agent_id
        agent_id_ctx.set(agent_id)
        return Agent(agent_id=agent_id, is_admin=False, authenticated=False)

    raise Unauthenticated()


def require_admin(agent: Annotated[Agent, Depends(get_current_agent)]) -> Agent:
    if not agent.is_admin:
        raise AdminRequired()
    return agent


def get_request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "-")


@dataclass(frozen=True)
class Pagination:
    limit: int
    cursor: str | None


def pagination(
    limit: Annotated[
        int,
        Query(ge=1, le=settings.max_page_size, description="Maximum items to return."),
    ] = settings.default_page_size,
    cursor: Annotated[
        str | None, Query(description="Opaque cursor from the previous page's next_cursor.")
    ] = None,
) -> Pagination:
    return Pagination(limit=limit, cursor=cursor)


CurrentAgent = Annotated[Agent, Depends(get_current_agent)]
AdminAgent = Annotated[Agent, Depends(require_admin)]
DbSession = Annotated[Session, Depends(get_db)]
RequestId = Annotated[str, Depends(get_request_id)]
PageParams = Annotated[Pagination, Depends(pagination)]
