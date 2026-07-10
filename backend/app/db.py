"""Database engine, session management and PostgreSQL advisory-lock helpers."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings

# A dedicated schema keeps the test suite from touching production tables when
# both point at the same PostgreSQL server (common with a single Neon project).
DB_SCHEMA = os.getenv("DB_SCHEMA") or None


def _connect_args() -> dict:
    options: list[str] = []
    if settings.db_statement_timeout_ms > 0:
        options.append(f"-c statement_timeout={settings.db_statement_timeout_ms}")
    if DB_SCHEMA:
        options.append(f"-c search_path={DB_SCHEMA},public")
    args: dict = {}
    if options:
        args["options"] = " ".join(options)
    return args


engine: Engine = create_engine(
    settings.database_url,
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
    pool_pre_ping=settings.db_pool_pre_ping,
    pool_recycle=1800,
    future=True,
    connect_args=_connect_args(),
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    """Timezone-aware UTC. All timestamps in this service are UTC."""
    return datetime.now(timezone.utc)


def get_db() -> Iterator[Session]:
    """FastAPI dependency: one session per request, always closed."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope for workers and scripts."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


# --- Advisory locks ----------------------------------------------------------
# Used so that failure detection is safe when several application instances run
# at once. Only the instance holding the lock performs a sweep; the others move
# on immediately instead of blocking.
#
# Transaction-scoped (`_xact_`) rather than session-scoped: PostgreSQL releases
# the lock on COMMIT or ROLLBACK, so a crashed sweep can never strand it in a
# pooled connection.

REAPER_LOCK_KEY = 0x4E52_4231  # "NRB1"


def try_advisory_lock(db: Session, key: int) -> bool:
    """Transaction-scoped try-lock. Returns False when another instance holds it."""
    return bool(db.execute(text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": key}).scalar())


@contextmanager
def advisory_lock(db: Session, key: int) -> Iterator[bool]:
    """Yields whether the lock was acquired. Released when the transaction ends."""
    yield try_advisory_lock(db, key)


@event.listens_for(Engine, "connect")
def _set_utc(dbapi_connection, _record) -> None:  # pragma: no cover - driver glue
    """Force every connection to UTC so timestamps never depend on server locale."""
    try:
        with dbapi_connection.cursor() as cur:
            cur.execute("SET TIME ZONE 'UTC'")
    except Exception:
        pass
