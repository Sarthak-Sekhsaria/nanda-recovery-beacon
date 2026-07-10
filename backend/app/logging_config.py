"""Structured JSON logging with request-id propagation and secret redaction."""

from __future__ import annotations

import json
import logging
import re
import sys
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

from app.config import settings

request_id_ctx: ContextVar[str] = ContextVar("request_id", default="-")
agent_id_ctx: ContextVar[str] = ContextVar("agent_id", default="-")

# Anything resembling a credential is scrubbed before it can reach a log sink.
_SECRET_KEYS = {
    "authorization",
    "api_key",
    "apikey",
    "x-api-key",
    "lease_token",
    "x-lease-token",
    "key_hash",
    "password",
    "secret",
    "token",
}
_TOKEN_PATTERN = re.compile(r"\b(nrb_|lease_)[A-Za-z0-9_\-]{6,}")


def redact(value: Any) -> Any:
    """Recursively replace secret-looking values with a stable placeholder."""
    if isinstance(value, dict):
        return {
            k: ("[REDACTED]" if k.lower() in _SECRET_KEYS else redact(v)) for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return _TOKEN_PATTERN.sub(lambda m: f"{m.group(1)}[REDACTED]", value)
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()),
            "service": settings.service_name,
            "environment": settings.environment,
            "request_id": request_id_ctx.get(),
            "agent_id": agent_id_ctx.get(),
        }
        extra = getattr(record, "extra_fields", None)
        if extra:
            payload.update(redact(extra))
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(settings.log_level.upper())

    # uvicorn's own handlers would double-print in plain text.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers = [handler]
        logger.propagate = False
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def log(logger: logging.Logger, level: int, message: str, **fields: Any) -> None:
    logger.log(level, message, extra={"extra_fields": fields})


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
