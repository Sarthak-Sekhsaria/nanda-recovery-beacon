"""System endpoints: health, readiness, agent identity, and the agent-facing SKILL.md."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from fastapi import APIRouter, Response
from sqlalchemy import text

from app import reaper
from app.api.deps import AdminAgent, CurrentAgent, DbSession
from app.config import settings
from app.db import utcnow
from app.errors import ServiceUnavailable
from app.schemas import HealthOut, ReadyOut
from app.security import create_api_key

#: Mounted at the root *and* under /api/v1 so both /health and /api/v1/health work.
router = APIRouter(tags=["system"])
#: Mounted only under /api/v1.
v1_router = APIRouter(tags=["system"])

VERSION = "1.0.0"

# The placeholder in the repository copy of SKILL.md is replaced with the live
# base URL at serve time, so /skill.md always advertises the URL it is served from.
BASE_URL_PLACEHOLDER = "{{PUBLIC_BASE_URL}}"


def _skill_md_path() -> Path:
    override = os.getenv("SKILL_MD_PATH")
    if override:
        return Path(override)
    here = Path(__file__).resolve()
    for candidate in (
        here.parents[2] / "SKILL.md",  # backend/SKILL.md (Docker image)
        here.parents[3] / "SKILL.md",  # repository root (local + Render)
    ):
        if candidate.exists():
            return candidate
    return here.parents[3] / "SKILL.md"


@lru_cache(maxsize=1)
def _skill_md_cached(mtime: float) -> str:
    return _skill_md_path().read_text(encoding="utf-8")


def skill_markdown() -> str:
    path = _skill_md_path()
    if not path.exists():
        raise ServiceUnavailable("SKILL.md is not present in this deployment.")
    content = _skill_md_cached(path.stat().st_mtime)
    return content.replace(BASE_URL_PLACEHOLDER, settings.public_base_url.rstrip("/"))


@router.get("/health", response_model=HealthOut, summary="Liveness probe (no auth)")
def health() -> HealthOut:
    return HealthOut(
        status="ok",
        service=settings.service_name,
        version=VERSION,
        environment=settings.environment,
        time=utcnow(),
    )


@router.get(
    "/ready",
    response_model=ReadyOut,
    summary="Readiness probe (no auth): checks the database and migrations",
    responses={503: {"description": "Database unreachable or migrations not applied"}},
)
def ready(db: DbSession) -> ReadyOut:
    try:
        db.execute(text("SELECT 1"))
    except Exception as exc:  # pragma: no cover - depends on infrastructure
        raise ServiceUnavailable("Database is unreachable.", details={"error": str(exc)[:200]}) from exc

    try:
        applied = bool(db.execute(text("SELECT version_num FROM alembic_version")).scalar())
    except Exception:
        applied = False

    if not applied:
        raise ServiceUnavailable(
            "Database schema is not migrated. Run 'alembic upgrade head'.",
            details={"migrations_applied": False},
        )

    return ReadyOut(
        status="ready",
        database="ok",
        migrations_applied=True,
        reaper_last_success=reaper.last_success_at(),
        time=utcnow(),
    )


@router.get(
    "/skill.md",
    response_class=Response,
    summary="The agent-facing instruction document (no auth)",
    description=(
        "Returns the current SKILL.md as text/markdown, with the deployment's public "
        "base URL substituted in. An agent needs nothing else to use this service."
    ),
    responses={200: {"content": {"text/markdown": {}}}},
)
def skill_md() -> Response:
    return Response(
        content=skill_markdown(),
        media_type="text/markdown; charset=utf-8",
        headers={"Cache-Control": "public, max-age=60"},
    )


@v1_router.get("/agents/me", summary="Identify the calling agent")
def whoami(agent: CurrentAgent) -> dict:
    return {
        "agent_id": agent.agent_id,
        "authenticated": agent.authenticated,
        "is_admin": agent.is_admin,
        "demo_mode": settings.demo_mode,
    }


@v1_router.post(
    "/admin/api-keys",
    status_code=201,
    summary="Mint an API key for an agent (admin key required)",
    description=(
        "The raw key is returned exactly once and never stored. Only its SHA-256 hash "
        "is persisted. Use `python -m app.cli create-key` to mint the first admin key."
    ),
)
def mint_api_key(
    agent: AdminAgent,
    db: DbSession,
    agent_id: str,
    label: str | None = None,
    is_admin: bool = False,
) -> dict:
    raw = create_api_key(db, agent_id=agent_id, label=label, is_admin=is_admin)
    db.commit()
    return {
        "agent_id": agent_id,
        "api_key": raw,
        "is_admin": is_admin,
        "warning": "Store this now. It cannot be retrieved again.",
    }
