"""Small helpers shared across services: canonical JSON, checksums, cursors."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import uuid
from datetime import datetime
from typing import Any

from app.errors import BadRequest


def canonical_json(value: Any) -> str:
    """Stable JSON: sorted keys, no incidental whitespace. Same input, same bytes."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False)


def checksum(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def encode_cursor(parts: list[Any]) -> str:
    raw = canonical_json(parts).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> list[Any]:
    padding = "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode(cursor + padding)
        parts = json.loads(raw)
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise BadRequest("Malformed pagination cursor.", details={"cursor": cursor}) from exc
    if not isinstance(parts, list):
        raise BadRequest("Malformed pagination cursor.", details={"cursor": cursor})
    return parts


def parse_datetime(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise BadRequest("Malformed pagination cursor timestamp.") from exc


def parse_uuid(value: str, field: str = "id") -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise BadRequest(f"'{field}' is not a valid UUID.", details={field: value}) from exc
