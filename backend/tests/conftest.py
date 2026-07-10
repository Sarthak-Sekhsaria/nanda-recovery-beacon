"""Test bootstrap.

Requires a real PostgreSQL: the guarantees under test (partial unique indexes,
``SELECT ... FOR UPDATE``, advisory locks, append-only triggers) do not exist in
SQLite. Point ``TEST_DATABASE_URL`` at any PostgreSQL 14+ instance; a free Neon
project works.

Everything is created inside a throwaway schema (``DB_SCHEMA``), so running the
suite against the same database that backs a deployment cannot touch its tables.
"""

from __future__ import annotations

import os
import socket
import threading
import time
from collections.abc import Iterator

import pytest

TEST_SCHEMA = os.environ.get("TEST_DB_SCHEMA", "beacon_test")
_TEST_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")

if not _TEST_URL:
    pytest.exit(
        "TEST_DATABASE_URL is not set. Point it at a PostgreSQL instance, e.g.\n"
        "  export TEST_DATABASE_URL='postgresql+psycopg://user:pass@host/db?sslmode=require'",
        returncode=1,
    )

# These must be set before `app.*` is imported for the first time.
os.environ["DATABASE_URL"] = _TEST_URL
os.environ["DB_SCHEMA"] = TEST_SCHEMA
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("DEMO_MODE", "false")
os.environ.setdefault("RUN_REAPER_IN_API", "false")  # tests drive the reaper explicitly
os.environ.setdefault("RATE_LIMIT_REQUESTS", "100000")
os.environ.setdefault("SUSPECT_GRACE_SECONDS", "1")
os.environ.setdefault("MIN_HEARTBEAT_TIMEOUT_SECONDS", "1")
os.environ.setdefault("MIN_LEASE_SECONDS", "1")
os.environ.setdefault("ARTIFACT_ALLOW_PRIVATE_NETWORKS", "true")  # local fixture server
os.environ.setdefault("ARTIFACT_VERIFY_MAX_BYTES", "65536")
os.environ.setdefault("OPPORTUNISTIC_SWEEP_MIN_INTERVAL_SECONDS", "0")

import httpx  # noqa: E402
import uvicorn  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.db import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    APPEND_ONLY_FUNCTION_SQL,
    APPEND_ONLY_TRIGGERS_SQL,
)
from app.rate_limit import limiter  # noqa: E402
from app.security import create_api_key  # noqa: E402

TABLES = [
    "idempotency_records",
    "recovery_events",
    "claims",
    "artifacts",
    "checkpoints",
    "workflows",
]


@pytest.fixture(scope="session", autouse=True)
def _schema() -> Iterator[None]:
    with engine.begin() as conn:
        conn.execute(text(f"DROP SCHEMA IF EXISTS {TEST_SCHEMA} CASCADE"))
        conn.execute(text(f"CREATE SCHEMA {TEST_SCHEMA}"))
        conn.execute(text(f"SET search_path TO {TEST_SCHEMA}"))
    # create_all builds every table and index declared on the models, including the
    # partial unique index ux_claims_one_active_per_workflow. The append-only triggers
    # are raw SQL, so they are applied separately here (mirroring the Alembic migration).
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text(APPEND_ONLY_FUNCTION_SQL))
        for trigger_sql in APPEND_ONLY_TRIGGERS_SQL:
            conn.execute(text(trigger_sql))
        # The test schema is built with create_all rather than Alembic, so stamp an
        # alembic_version row. This makes GET /ready (which checks migrations are
        # applied) behave exactly as it does against a migrated production database.
        conn.execute(text("CREATE TABLE IF NOT EXISTS alembic_version (version_num varchar(32) NOT NULL)"))
        conn.execute(text("DELETE FROM alembic_version"))
        conn.execute(text("INSERT INTO alembic_version (version_num) VALUES ('0001')"))
    yield
    with engine.begin() as conn:
        conn.execute(text(f"DROP SCHEMA IF EXISTS {TEST_SCHEMA} CASCADE"))


@pytest.fixture(autouse=True)
def _clean_tables() -> Iterator[None]:
    """Truncate between tests. TRUNCATE bypasses the append-only row triggers."""
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE"))
    limiter.reset()
    yield


@pytest.fixture
def db() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="session")
def base_url() -> Iterator[str]:
    """A real uvicorn server, so concurrency tests exercise the full HTTP stack."""
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", lifespan="on")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.time() + 20
    while not server.started and time.time() < deadline:
        time.sleep(0.02)
    if not server.started:  # pragma: no cover
        raise RuntimeError("test server did not start")

    yield f"http://127.0.0.1:{port}"

    server.should_exit = True
    thread.join(timeout=10)


class AgentClient(httpx.Client):
    """An httpx client pinned to one agent's API key."""

    def __init__(self, base_url: str, agent_id: str, api_key: str) -> None:
        super().__init__(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30.0,
        )
        self.agent_id = agent_id
        self.api_key = api_key


def _make_key(agent_id: str, *, admin: bool = False) -> str:
    session = SessionLocal()
    try:
        raw = create_api_key(session, agent_id=agent_id, label="test", is_admin=admin)
        session.commit()
        return raw
    finally:
        session.close()


@pytest.fixture(scope="session")
def agent_keys() -> dict[str, str]:
    """API keys live for the whole session (api_keys is not truncated between tests)."""
    return {
        "agent-a": _make_key("agent-a"),
        "agent-b": _make_key("agent-b"),
        "agent-c": _make_key("agent-c"),
        "admin": _make_key("admin", admin=True),
    }


@pytest.fixture
def client(base_url: str, agent_keys: dict[str, str]) -> Iterator[AgentClient]:
    with AgentClient(base_url, "agent-a", agent_keys["agent-a"]) as c:
        yield c


@pytest.fixture
def other_client(base_url: str, agent_keys: dict[str, str]) -> Iterator[AgentClient]:
    with AgentClient(base_url, "agent-b", agent_keys["agent-b"]) as c:
        yield c


@pytest.fixture
def admin_client(base_url: str, agent_keys: dict[str, str]) -> Iterator[AgentClient]:
    with AgentClient(base_url, "admin", agent_keys["admin"]) as c:
        yield c


@pytest.fixture
def anon_client(base_url: str) -> Iterator[httpx.Client]:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        yield c


# --- Domain helpers ----------------------------------------------------------
GOOD_CHECKPOINT = {
    "objective": "Compare five scholarship programs",
    "completed_steps": ["Found five programs", "Collected eligibility requirements"],
    "remaining_steps": ["Compare deadlines", "Produce recommendation"],
    "decisions": [
        {
            "decision": "Only include programs open to international students",
            "reason": "Required by the original request from the user",
        }
    ],
    "next_action": "Compare application deadlines",
    "context_summary": (
        "Five programs identified. Eligibility gathered for all five. Deadlines not compared."
    ),
    "variables": {"programs_found": 5},
    "schema_version": "1.0",
}


def create_workflow(client: httpx.Client, **overrides) -> dict:
    payload = {
        "title": "Scholarship comparison",
        "objective": "Compare five scholarship programs and recommend one",
        "priority": "high",
        "heartbeat_timeout_seconds": 60,
        "tags": ["research"],
        "initial_checkpoint": GOOD_CHECKPOINT,
    }
    payload.update(overrides)
    response = client.post("/api/v1/workflows", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def make_recoverable(client: httpx.Client, workflow_id: str, reason: str = "test failure") -> dict:
    response = client.post(f"/api/v1/workflows/{workflow_id}/fail", json={"reason": reason})
    assert response.status_code == 200, response.text
    return response.json()


def claim(client: httpx.Client, workflow_id: str, **body) -> httpx.Response:
    payload = {"lease_seconds": 60}
    payload.update(body)
    return client.post(f"/api/v1/workflows/{workflow_id}/claims", json=payload)


def error_code(response: httpx.Response) -> str:
    return response.json()["error"]["code"]
