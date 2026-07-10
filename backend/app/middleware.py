"""HTTP middleware: request context, size limits, rate limiting, security headers."""

from __future__ import annotations

import logging
import time
import uuid

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response
from starlette.types import ASGIApp

from app import metrics
from app.config import settings
from app.errors import RateLimited, RequestTooLarge
from app.logging_config import agent_id_ctx, get_logger, log, request_id_ctx
from app.rate_limit import limiter
from app.security import PREFIX_LENGTH

logger = get_logger("app.request")

REQUEST_ID_HEADER = "X-Request-Id"
EXEMPT_PATHS = frozenset({"/health", "/ready", "/metrics"})


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assigns a request id, times the request, emits one structured log line."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex
        request.state.request_id = request_id
        token = request_id_ctx.set(request_id)
        agent_token = agent_id_ctx.set("-")

        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            duration = time.perf_counter() - started
            log(
                logger,
                logging.ERROR,
                "request.failed",
                method=request.method,
                path=request.url.path,
                duration_ms=round(duration * 1000, 2),
            )
            raise
        finally:
            request_id_ctx.reset(token)
            agent_id_ctx.reset(agent_token)

        duration = time.perf_counter() - started
        route = request.scope.get("route")
        route_template = getattr(route, "path", request.url.path)

        response.headers[REQUEST_ID_HEADER] = request_id
        metrics.http_requests_total.labels(
            method=request.method, route=route_template, status=str(response.status_code)
        ).inc()
        metrics.http_request_duration_seconds.labels(
            method=request.method, route=route_template
        ).observe(duration)

        if request.url.path not in EXEMPT_PATHS:
            log(
                logger,
                logging.INFO,
                "request.completed",
                method=request.method,
                path=request.url.path,
                route=route_template,
                status=response.status_code,
                duration_ms=round(duration * 1000, 2),
            )
        return response


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    """Rejects oversized bodies before they are parsed."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        super().__init__(app)
        self.max_bytes = max_bytes

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        content_length = request.headers.get("content-length")
        if content_length and content_length.isdigit() and int(content_length) > self.max_bytes:
            error = RequestTooLarge(
                details={"max_request_bytes": self.max_bytes, "content_length": int(content_length)}
            )
            return JSONResponse(
                error.to_body(getattr(request.state, "request_id", "-")),
                status_code=error.status_code,
            )
        return await call_next(request)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Fixed-window limit keyed by API key prefix, falling back to client IP."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if not settings.rate_limit_enabled or request.url.path in EXEMPT_PATHS:
            return await call_next(request)

        header = request.headers.get("authorization") or request.headers.get("x-api-key") or ""
        token = header[7:] if header.lower().startswith("bearer ") else header
        # The prefix is not a secret and is enough to distinguish callers.
        client_host = request.client.host if request.client else "unknown"
        bucket = token[:PREFIX_LENGTH] if token else f"ip:{client_host}"

        allowed, remaining, retry_after = limiter.check(bucket)
        if not allowed:
            metrics.rate_limited_total.inc()
            error = RateLimited(retry_after_seconds=retry_after)
            return JSONResponse(
                error.to_body(getattr(request.state, "request_id", "-")),
                status_code=error.status_code,
                headers={
                    "Retry-After": str(retry_after),
                    "X-RateLimit-Limit": str(limiter.limit),
                    "X-RateLimit-Remaining": "0",
                },
            )

        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(limiter.limit)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        response.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
        if settings.is_production:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response
