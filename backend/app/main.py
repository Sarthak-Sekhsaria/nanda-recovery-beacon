"""FastAPI application factory.

Run locally:      uvicorn app.main:app --reload
Run in prod:      uvicorn app.main:app --host 0.0.0.0 --port $PORT
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app import reaper
from app.api.system import VERSION
from app.api.system import router as system_router
from app.api.system import v1_router as system_v1_router
from app.api.v1 import artifacts, checkpoints, claims, events, workflows
from app.config import settings
from app.errors import BeaconError, NotFound, SchemaValidationError
from app.logging_config import configure_logging, get_logger, log
from app.metrics import render_latest
from app.middleware import (
    BodySizeLimitMiddleware,
    RateLimitMiddleware,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
)

logger = get_logger("app.main")

DESCRIPTION = """
**NANDA Recovery Beacon** is recovery infrastructure for AI-agent workflows.

When an agent crashes, times out, or vanishes mid-task, another agent can discover the
unfinished workflow, claim it under an exclusive lease, read a complete recovery package,
and finish the work without repeating anything.

* Read [`/skill.md`](/skill.md) first -- it is written for autonomous agents.
* Authenticate with `Authorization: Bearer <api_key>`.
* Every error is machine-readable: branch on `error.code`, never on the message.
* No LLM is used anywhere in this service. Context evaluation is deterministic.
"""

TAGS_METADATA = [
    {"name": "workflows", "description": "Create work, prove liveness, fail, and complete."},
    {"name": "checkpoints", "description": "Immutable, versioned progress snapshots."},
    {"name": "claims", "description": "Exclusive time-boxed leases with fencing tokens."},
    {"name": "artifacts", "description": "Outputs needed to resume, with SHA-256 verification."},
    {"name": "audit", "description": "Append-only event history and aggregate statistics."},
    {"name": "system", "description": "Health, readiness, identity and the agent skill document."},
]


def _start_reaper_thread(app: FastAPI) -> threading.Event | None:
    if not settings.run_reaper_in_api:
        return None

    stop = threading.Event()

    def loop() -> None:
        log(logger, logging.INFO, "reaper.thread.start", interval=settings.reaper_interval_seconds)
        while not stop.is_set():
            try:
                reaper.run_sweep()
            except Exception:
                logger.exception("in-process reaper sweep failed")
            stop.wait(settings.reaper_interval_seconds)

    thread = threading.Thread(target=loop, name="nrb-reaper", daemon=True)
    thread.start()
    app.state.reaper_thread = thread
    return stop


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    log(
        logger,
        logging.INFO,
        "service.start",
        version=VERSION,
        environment=settings.environment,
        demo_mode=settings.demo_mode,
        reaper_in_api=settings.run_reaper_in_api,
        public_base_url=settings.public_base_url,
    )
    stop = _start_reaper_thread(app)
    try:
        yield
    finally:
        if stop is not None:
            stop.set()
        log(logger, logging.INFO, "service.stop")


app = FastAPI(
    title="NANDA Recovery Beacon",
    version=VERSION,
    description=DESCRIPTION,
    openapi_tags=TAGS_METADATA,
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    contact={"name": "NANDA Recovery Beacon", "url": settings.public_base_url},
    license_info={"name": "MIT"},
)

# Middleware executes bottom-up: TrustedHost -> CORS -> Security -> RateLimit -> Size -> Context.
app.add_middleware(RequestContextMiddleware)
app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_request_bytes)
app.add_middleware(RateLimitMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allow_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-Agent-Id", "X-API-Key"],
    expose_headers=["X-Request-Id", "Idempotent-Replay", "X-RateLimit-Remaining"],
    max_age=600,
)
if settings.trusted_hosts != ["*"]:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.trusted_hosts)


# --- Exception handlers ------------------------------------------------------
def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "-")


@app.exception_handler(BeaconError)
async def beacon_error_handler(request: Request, exc: BeaconError) -> JSONResponse:
    headers = {}
    if exc.retry_after_seconds:
        headers["Retry-After"] = str(exc.retry_after_seconds)
    if exc.status_code >= 500:
        logger.error("beacon error: %s", exc.code, exc_info=exc)
    return JSONResponse(
        exc.to_body(_request_id(request)), status_code=exc.status_code, headers=headers
    )


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    error = SchemaValidationError(
        details={
            "violations": [
                {
                    "location": list(err.get("loc", [])),
                    "message": err.get("msg"),
                    "type": err.get("type"),
                }
                for err in exc.errors()[:20]
            ]
        }
    )
    return JSONResponse(error.to_body(_request_id(request)), status_code=422)


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    if exc.status_code == 404:
        error: BeaconError = NotFound(
            "No route matches that path. See /docs for the full API.",
            details={"path": request.url.path},
        )
    else:
        error = BeaconError(str(exc.detail))
        error.status_code = exc.status_code
        error.code = f"HTTP_{exc.status_code}"
    return JSONResponse(error.to_body(_request_id(request)), status_code=error.status_code)


@app.exception_handler(Exception)
async def unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("unhandled exception")
    error = BeaconError("An unexpected error occurred. The request id identifies it in the logs.")
    return JSONResponse(error.to_body(_request_id(request)), status_code=500)


# --- Routes ------------------------------------------------------------------
app.include_router(system_router)
app.include_router(system_v1_router, prefix="/api/v1")
# Alias: agents that only know the versioned prefix still find health/ready/skill.md.
app.include_router(system_router, prefix="/api/v1", include_in_schema=False)

for module in (workflows, checkpoints, claims, artifacts, events):
    app.include_router(module.router, prefix="/api/v1")

app.state.started_at = time.time()


@app.get("/metrics", include_in_schema=False)
def metrics_endpoint() -> Response:
    """Prometheus exposition. No authentication. A plain route (not a mounted
    sub-app) so it answers at exactly /metrics with no trailing-slash redirect."""
    body, content_type = render_latest()
    return Response(content=body, media_type=content_type)


@app.get("/", include_in_schema=False)
def root() -> dict:
    base = settings.public_base_url.rstrip("/")
    return {
        "service": settings.service_name,
        "version": VERSION,
        "description": "Recovery and coordination service for interrupted AI-agent workflows.",
        "skill_md": f"{base}/skill.md",
        "openapi": f"{base}/openapi.json",
        "docs": f"{base}/docs",
        "health": f"{base}/health",
        "recoverable_workflows": f"{base}/api/v1/recoverable-workflows",
        "uptime_seconds": round(time.time() - app.state.started_at, 1),
    }
