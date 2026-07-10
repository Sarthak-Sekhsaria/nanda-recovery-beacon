"""System endpoints, error envelope, security headers, rate limiting, observability."""

from __future__ import annotations

import pytest

from app.rate_limit import limiter
from tests.conftest import create_workflow, error_code, make_recoverable


def test_health_is_public(anon_client):
    response = anon_client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "nanda-recovery-beacon"


def test_ready_checks_the_database(anon_client):
    response = anon_client.get("/ready")
    assert response.status_code == 200
    assert response.json()["database"] == "ok"


def test_skill_md_is_served_as_markdown_with_the_live_base_url(anon_client):
    response = anon_client.get("/skill.md")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    assert response.text.startswith("---")
    assert "name: nanda-recovery-beacon" in response.text
    assert "{{PUBLIC_BASE_URL}}" not in response.text, "placeholder was not substituted"


def test_skill_md_is_also_under_the_versioned_prefix(anon_client):
    assert anon_client.get("/api/v1/skill.md").status_code == 200


def test_openapi_documents_every_public_route(anon_client):
    spec = anon_client.get("/openapi.json").json()
    paths = spec["paths"]

    for required in (
        "/api/v1/workflows",
        "/api/v1/workflows/{workflow_id}",
        "/api/v1/workflows/{workflow_id}/recovery-package",
        "/api/v1/workflows/{workflow_id}/heartbeats",
        "/api/v1/workflows/{workflow_id}/fail",
        "/api/v1/workflows/{workflow_id}/checkpoints",
        "/api/v1/workflows/{workflow_id}/checkpoints/{version}",
        "/api/v1/workflows/{workflow_id}/evaluate-context",
        "/api/v1/recoverable-workflows",
        "/api/v1/workflows/{workflow_id}/claims",
        "/api/v1/workflows/{workflow_id}/claims/renew",
        "/api/v1/workflows/{workflow_id}/claims/release",
        "/api/v1/workflows/{workflow_id}/resume",
        "/api/v1/workflows/{workflow_id}/complete",
        "/api/v1/workflows/{workflow_id}/events",
        "/api/v1/workflows/{workflow_id}/artifacts",
        "/health",
        "/ready",
        "/skill.md",
    ):
        assert required in paths, f"{required} is missing from the OpenAPI document"


def test_metrics_are_prometheus_formatted(anon_client, client):
    create_workflow(client)
    body = anon_client.get("/metrics").text

    assert "nrb_workflows_created_total" in body
    assert "nrb_http_requests_total" in body
    assert "nrb_claims_acquired_total" in body
    assert "nrb_context_completeness_score" in body


def test_every_response_carries_a_request_id(client):
    response = client.get("/api/v1/workflows")
    assert response.headers["X-Request-Id"]


def test_request_id_is_echoed_when_supplied(client):
    response = client.get("/api/v1/workflows", headers={"X-Request-Id": "abc123"})
    assert response.headers["X-Request-Id"] == "abc123"


def test_errors_carry_the_request_id_of_the_failing_call(client):
    response = client.get(
        "/api/v1/workflows/00000000-0000-0000-0000-000000000000",
        headers={"X-Request-Id": "trace-me"},
    )
    assert response.status_code == 404
    assert response.json()["request_id"] == "trace-me"


def test_security_headers_are_present(anon_client):
    headers = anon_client.get("/health").headers
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"
    assert headers["Referrer-Policy"] == "no-referrer"


def test_unknown_route_returns_the_error_envelope(anon_client):
    response = anon_client.get("/api/v1/does-not-exist")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_rate_limiting_returns_429_with_retry_after(client):
    original = limiter.limit
    limiter.limit = 3
    limiter.reset()
    try:
        statuses = [client.get("/api/v1/workflows").status_code for _ in range(5)]
    finally:
        limiter.limit = original
        limiter.reset()

    assert statuses[:3] == [200, 200, 200]
    assert 429 in statuses

    limiter.limit = 1
    limiter.reset()
    try:
        client.get("/api/v1/workflows")
        limited = client.get("/api/v1/workflows")
    finally:
        limiter.limit = original
        limiter.reset()

    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "RATE_LIMITED"
    assert limited.json()["error"]["retryable"] is True
    assert int(limited.headers["Retry-After"]) >= 1


def test_health_is_exempt_from_rate_limiting(anon_client):
    original = limiter.limit
    limiter.limit = 1
    limiter.reset()
    try:
        statuses = [anon_client.get("/health").status_code for _ in range(4)]
    finally:
        limiter.limit = original
        limiter.reset()
    assert statuses == [200, 200, 200, 200]


def test_oversized_request_body_is_413(client):
    payload = {"title": "x", "objective": "y" * (1024 * 1024 + 10)}
    response = client.post("/api/v1/workflows", json=payload)
    assert response.status_code == 413
    assert error_code(response) == "REQUEST_TOO_LARGE"


def test_agents_me_identifies_the_caller(client):
    body = client.get("/api/v1/agents/me").json()
    assert body["agent_id"] == "agent-a"
    assert body["authenticated"] is True
    assert body["is_admin"] is False


def test_admin_endpoints_require_an_admin_key(client, admin_client):
    denied = client.post("/api/v1/admin/reap")
    assert denied.status_code == 403
    assert error_code(denied) == "ADMIN_REQUIRED"

    allowed = admin_client.post("/api/v1/admin/reap")
    assert allowed.status_code == 200
    assert "claims_expired" in allowed.json()


def test_admin_can_mint_a_key_that_works(client, admin_client, base_url):
    import httpx

    minted = admin_client.post(
        "/api/v1/admin/api-keys", params={"agent_id": "minted-agent", "label": "test"}
    )
    assert minted.status_code == 201
    raw = minted.json()["api_key"]
    assert raw.startswith("nrb_")

    with httpx.Client(base_url=base_url, headers={"Authorization": f"Bearer {raw}"}) as fresh:
        assert fresh.get("/api/v1/agents/me").json()["agent_id"] == "minted-agent"


def test_stats_reflect_real_data(client, other_client):
    active = create_workflow(client, title="Active one")
    recoverable = create_workflow(client, title="Recoverable one")
    make_recoverable(client, recoverable["id"])

    stats = client.get("/api/v1/stats").json()
    assert stats["status_counts"]["active"] == 1
    assert stats["status_counts"]["recoverable"] == 1
    assert stats["total_workflows"] == 2
    assert stats["checkpoints_total"] == 2
    assert sum(stats["context_score_distribution"].values()) == 2
    assert any(e["event_type"] == "workflow_created" for e in stats["recent_events"])
    assert active["id"]


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/api/v1/workflows", 401),
        ("/api/v1/recoverable-workflows", 401),
        ("/api/v1/stats", 401),
        ("/api/v1/events", 401),
    ],
)
def test_reads_require_authentication(anon_client, path, expected):
    assert anon_client.get(path).status_code == expected
