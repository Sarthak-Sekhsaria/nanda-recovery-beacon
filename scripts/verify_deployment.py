#!/usr/bin/env python3
"""Smoke-test a live NANDA Recovery Beacon deployment (no jq required).

    python scripts/verify_deployment.py https://your-api.onrender.com [API_KEY]

Drives the whole recovery lifecycle against the live service and checks the audit
trail and security headers. Exits non-zero on the first failure. Needs only httpx
(installed in the backend venv):

    backend/.venv/Scripts/python.exe scripts/verify_deployment.py <url>
"""

from __future__ import annotations

import sys

try:
    import httpx
except ModuleNotFoundError:
    sys.exit("httpx is required. Run this with the backend venv's Python.")

GREEN, RED, RESET = "\033[32m", "\033[31m", "\033[0m"


def main() -> int:
    if len(sys.argv) < 2:
        sys.exit("usage: verify_deployment.py <base-url> [api-key]")
    base = sys.argv[1].rstrip("/")
    key = sys.argv[2] if len(sys.argv) > 2 else None

    if key:
        a = {"Authorization": f"Bearer {key}"}
        b = {"Authorization": f"Bearer {key}"}
    else:
        print("No API key supplied: assuming DEMO_MODE=true, using X-Agent-Id.")
        a = {"X-Agent-Id": "verify-agent-a"}
        b = {"X-Agent-Id": "verify-agent-b"}

    c = httpx.Client(base_url=base, timeout=30.0)
    passed = failed = 0

    def chk(label: str, got, want) -> None:
        nonlocal passed, failed
        if got == want:
            passed += 1
            print(f"  {GREEN}PASS{RESET}  {label:46} {got}")
        else:
            failed += 1
            print(f"  {RED}FAIL{RESET}  {label:46} got {got!r} want {want!r}")

    print(f"\nVerifying {base}\n-- public endpoints --")
    for path, exp in [
        ("/health", 200),
        ("/ready", 200),
        ("/skill.md", 200),
        ("/openapi.json", 200),
        ("/metrics", 200),
        ("/docs", 200),
    ]:
        chk(f"GET {path}", c.get(path).status_code, exp)

    skill = c.get("/skill.md").text
    chk("skill.md frontmatter", skill.splitlines()[0] if skill else "", "---")
    chk("skill.md placeholder substituted", "{{PUBLIC_BASE_URL}}" not in skill, True)
    chk("metrics exposition non-empty", "nrb_workflows_created_total" in c.get("/metrics").text, True)

    print("-- recovery lifecycle --")
    wf = c.post(
        "/api/v1/workflows",
        headers=a,
        json={
            "title": "Deployment smoke test",
            "objective": "Prove the recovery lifecycle works on this deployment",
            "priority": "low",
            "heartbeat_timeout_seconds": 30,
            "tags": ["smoke-test"],
            "initial_checkpoint": {
                "objective": "Prove the recovery lifecycle works on this deployment",
                "completed_steps": ["Created the workflow"],
                "remaining_steps": ["Fail it", "Recover it", "Complete it"],
                "next_action": "Report an explicit failure",
                "context_summary": "A synthetic workflow created by verify_deployment.py.",
                "decisions": [{"decision": "Use low priority", "reason": "This is only a smoke test"}],
            },
        },
    )
    if wf.status_code != 201:
        print(f"  {RED}FAIL{RESET}  could not create a workflow: {wf.status_code} {wf.text[:200]}")
        return 1
    wid = wf.json()["id"]
    print(f"  workflow: {wid}")

    chk("create -> active", wf.json()["status"], "active")
    chk("heartbeat", c.post(f"/api/v1/workflows/{wid}/heartbeats", headers=a, json={}).status_code, 200)
    chk(
        "evaluate-context resumable",
        c.post(f"/api/v1/workflows/{wid}/evaluate-context", headers=a, json={}).json()["resumable"],
        True,
    )
    chk(
        "fail -> recoverable",
        c.post(f"/api/v1/workflows/{wid}/fail", headers=a, json={"reason": "synthetic"}).json()["status"],
        "recoverable",
    )
    queue = c.get("/api/v1/recoverable-workflows", headers=b, params={"tag": "smoke-test", "limit": 100}).json()
    chk("discover in queue", any(i["workflow"]["id"] == wid for i in queue["items"]), True)
    pkg = c.get(f"/api/v1/workflows/{wid}/recovery-package", headers=b).json()
    chk("recovery-package resumable", pkg["context_evaluation"]["resumable"], True)

    claim = c.post(f"/api/v1/workflows/{wid}/claims", headers=b, json={"lease_seconds": 120}).json()
    lease = claim["lease_token"]
    chk("claim -> fencing token", claim["fencing_token"], 1)
    chk(
        "second claim conflicts",
        c.post(f"/api/v1/workflows/{wid}/claims", headers=a, json={"lease_seconds": 120}).json()["error"]["code"],
        "CLAIM_ALREADY_HELD",
    )
    chk(
        "resume",
        c.post(f"/api/v1/workflows/{wid}/resume", headers=b, json={"lease_token": lease}).json()["status"],
        "active",
    )
    chk(
        "stale parent_version -> 409",
        c.post(
            f"/api/v1/workflows/{wid}/checkpoints",
            headers=b,
            json={"parent_version": 0, "lease_token": lease, "objective": "x", "remaining_steps": ["y"], "next_action": "z"},
        ).json()["error"]["code"],
        "STALE_CHECKPOINT_VERSION",
    )
    chk(
        "checkpoint v2",
        c.post(
            f"/api/v1/workflows/{wid}/checkpoints",
            headers=b,
            json={
                "parent_version": 1,
                "lease_token": lease,
                "objective": "Prove the recovery lifecycle works on this deployment",
                "completed_steps": ["Created the workflow", "Failed it", "Recovered it"],
                "remaining_steps": [],
                "next_action": "Nothing remains; ready to complete",
                "context_summary": "Created, failed, discovered, claimed and resumed.",
            },
        ).json()["version"],
        2,
    )
    complete_headers = dict(b, **{"Idempotency-Key": f"complete-{wid}"})
    chk(
        "complete",
        c.post(
            f"/api/v1/workflows/{wid}/complete",
            headers=complete_headers,
            json={"lease_token": lease, "final_checkpoint_version": 2, "summary": "smoke test"},
        ).json()["status"],
        "completed",
    )
    chk(
        "completion replays idempotently",
        c.post(
            f"/api/v1/workflows/{wid}/complete",
            headers=complete_headers,
            json={"lease_token": lease, "final_checkpoint_version": 2, "summary": "smoke test"},
        ).headers.get("idempotent-replay"),
        "true",
    )

    events = {e["event_type"] for e in c.get(f"/api/v1/workflows/{wid}/events", headers=b, params={"limit": 100}).json()["items"]}
    for want in [
        "workflow_created",
        "checkpoint_created",
        "explicit_failure_reported",
        "workflow_made_recoverable",
        "claim_acquired",
        "workflow_resumed",
        "stale_update_rejected",
        "workflow_completed",
    ]:
        chk(f"audit: {want}", want in events, True)

    chk(
        "immutable history intact",
        [x["version"] for x in c.get(f"/api/v1/workflows/{wid}/checkpoints", headers=b).json()["items"]],
        [2, 1],
    )

    print("-- security --")
    chk("unknown route -> 404", c.get("/api/v1/nope").status_code, 404)
    chk("bad key -> 401", c.get("/api/v1/workflows", headers={"Authorization": "Bearer nrb_invalid"}).status_code, 401)
    chk("security header", c.get("/health").headers.get("x-content-type-options"), "nosniff")

    print(f"\npassed: {passed}   failed: {failed}")
    print(f"workflow used: {base}/api/v1/workflows/{wid}\n")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
