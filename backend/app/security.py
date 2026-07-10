"""API-key and lease-token handling.

Rules:
* Raw secrets are never written to the database. Only SHA-256 hashes are stored.
* Comparison of any secret-derived value uses ``hmac.compare_digest``.
* A short, non-secret prefix is stored alongside the hash purely so operators can
  correlate a key with a log line without ever seeing the secret.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import utcnow
from app.models import ApiKey

API_KEY_PREFIX = "nrb_"
LEASE_TOKEN_PREFIX = "lease_"
PREFIX_LENGTH = 12


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def generate_api_key() -> tuple[str, str, str]:
    """Return ``(raw_key, key_hash, key_prefix)``. The raw key is shown once."""
    raw = API_KEY_PREFIX + secrets.token_urlsafe(32)
    return raw, sha256_hex(raw), raw[:PREFIX_LENGTH]


def generate_lease_token() -> tuple[str, str, str]:
    """Return ``(raw_token, token_hash, token_prefix)``."""
    raw = LEASE_TOKEN_PREFIX + secrets.token_urlsafe(32)
    return raw, sha256_hex(raw), raw[:PREFIX_LENGTH]


def verify_secret(raw: str, expected_hash: str) -> bool:
    """Constant-time comparison of a presented secret against a stored hash."""
    return hmac.compare_digest(sha256_hex(raw), expected_hash)


def lookup_api_key(db: Session, raw_key: str) -> ApiKey | None:
    """Resolve a raw API key to its record, or None. Constant-time on the hash."""
    if not raw_key or not raw_key.startswith(API_KEY_PREFIX):
        return None
    candidates = (
        db.execute(
            select(ApiKey).where(
                ApiKey.key_prefix == raw_key[:PREFIX_LENGTH],
                ApiKey.active.is_(True),
            )
        )
        .scalars()
        .all()
    )
    presented = sha256_hex(raw_key)
    for candidate in candidates:
        if hmac.compare_digest(presented, candidate.key_hash):
            return candidate
    return None


def create_api_key(
    db: Session, agent_id: str, label: str | None = None, is_admin: bool = False
) -> str:
    """Create a key for ``agent_id`` and return the raw key exactly once."""
    raw, key_hash, key_prefix = generate_api_key()
    db.add(
        ApiKey(
            agent_id=agent_id,
            label=label,
            key_hash=key_hash,
            key_prefix=key_prefix,
            is_admin=is_admin,
            active=True,
        )
    )
    db.flush()
    return raw


def touch_api_key(db: Session, api_key: ApiKey) -> None:
    api_key.last_used_at = utcnow()


def lease_expiry(seconds: int, *, now: datetime | None = None) -> datetime:
    return (now or utcnow()) + timedelta(seconds=seconds)


def seconds_until(moment: datetime) -> int:
    delta = moment - datetime.now(timezone.utc)
    return max(0, int(delta.total_seconds()))
