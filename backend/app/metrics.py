"""Prometheus metrics. Exposed as OpenMetrics text at GET /metrics."""

from __future__ import annotations

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

REGISTRY = CollectorRegistry(auto_describe=True)

http_requests_total = Counter(
    "nrb_http_requests_total",
    "HTTP requests handled, by method, route template and status class.",
    ["method", "route", "status"],
    registry=REGISTRY,
)
http_request_duration_seconds = Histogram(
    "nrb_http_request_duration_seconds",
    "HTTP request latency in seconds.",
    ["method", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
    registry=REGISTRY,
)

workflows_created_total = Counter(
    "nrb_workflows_created_total", "Workflows created.", registry=REGISTRY
)
workflows_completed_total = Counter(
    "nrb_workflows_completed_total", "Workflows completed.", registry=REGISTRY
)
checkpoints_created_total = Counter(
    "nrb_checkpoints_created_total", "Checkpoints created.", registry=REGISTRY
)
heartbeats_total = Counter("nrb_heartbeats_total", "Heartbeats received.", registry=REGISTRY)

claims_acquired_total = Counter(
    "nrb_claims_acquired_total", "Claims successfully acquired.", registry=REGISTRY
)
claim_conflicts_total = Counter(
    "nrb_claim_conflicts_total",
    "Claim attempts rejected because another agent held the lease or the state was wrong.",
    ["reason"],
    registry=REGISTRY,
)
claims_renewed_total = Counter("nrb_claims_renewed_total", "Lease renewals.", registry=REGISTRY)
claims_released_total = Counter(
    "nrb_claims_released_total", "Leases voluntarily released.", registry=REGISTRY
)
claims_expired_total = Counter(
    "nrb_claims_expired_total", "Leases expired by the reaper.", registry=REGISTRY
)

failures_detected_total = Counter(
    "nrb_failures_detected_total",
    "Workflow failures detected, by detection mechanism.",
    ["reason"],  # explicit | heartbeat_timeout
    registry=REGISTRY,
)
recoveries_total = Counter(
    "nrb_recoveries_total", "Workflows resumed by a replacement agent.", registry=REGISTRY
)
workflows_made_recoverable_total = Counter(
    "nrb_workflows_made_recoverable_total", "Workflows offered for recovery.", registry=REGISTRY
)
dead_lettered_total = Counter(
    "nrb_dead_lettered_total", "Workflows moved to dead_letter.", registry=REGISTRY
)
stale_updates_rejected_total = Counter(
    "nrb_stale_updates_rejected_total",
    "Updates rejected for a stale checkpoint version or superseded lease.",
    ["reason"],  # stale_version | stale_fencing_token | expired_lease
    registry=REGISTRY,
)
artifact_verifications_total = Counter(
    "nrb_artifact_verifications_total",
    "Artifact checksum verifications.",
    ["result"],  # verified | failed
    registry=REGISTRY,
)
rate_limited_total = Counter(
    "nrb_rate_limited_total", "Requests rejected by the rate limiter.", registry=REGISTRY
)

context_score = Histogram(
    "nrb_context_completeness_score",
    "Distribution of checkpoint completeness scores at evaluation time.",
    buckets=(10, 20, 30, 40, 50, 60, 70, 80, 90, 100),
    registry=REGISTRY,
)
recovery_seconds = Histogram(
    "nrb_recovery_duration_seconds",
    "Seconds between a workflow becoming recoverable and being resumed.",
    buckets=(1, 5, 15, 30, 60, 120, 300, 900, 3600),
    registry=REGISTRY,
)
reaper_sweep_seconds = Histogram(
    "nrb_reaper_sweep_duration_seconds",
    "Duration of a failure-detection sweep.",
    registry=REGISTRY,
)
reaper_last_success_timestamp = Gauge(
    "nrb_reaper_last_success_timestamp_seconds",
    "Unix timestamp of the last completed reaper sweep.",
    registry=REGISTRY,
)

def render_latest() -> tuple[bytes, str]:
    """Prometheus exposition bytes and their content type, for GET /metrics."""
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
