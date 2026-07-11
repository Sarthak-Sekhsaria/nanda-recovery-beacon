---
name: nanda-recovery-beacon
description: Use this service to checkpoint, detect, claim, resume, and safely complete interrupted AI-agent workflows without duplicating work.
---

# NANDA Recovery Beacon

A REST service that stores the progress of a long-running agent task so that, if the agent stops,
a different agent can take the task over and finish it without repeating completed work.

**Base URL:** `{{PUBLIC_BASE_URL}}`

This document is complete. You do not need any other information to use the service.
Send every request to the base URL above.

---

## Quick reference (read this first)

You are always in one of two roles.

**OWNER** — you are doing new work and want it to survive your own failure:

```
POST /api/v1/workflows                      # start; save the returned "id" as workflow_id
  ... do a step ...
POST /api/v1/workflows/{id}/checkpoints     # record progress after each step
POST /api/v1/workflows/{id}/heartbeats      # every < heartbeat_timeout_seconds while working
POST /api/v1/workflows/{id}/complete        # when remaining_steps is empty
POST /api/v1/workflows/{id}/fail            # instead, if you cannot continue
```

**RESCUER** — you are taking over another agent's failed work:

```
GET  /api/v1/recoverable-workflows?resumable_only=true   # find work
GET  /api/v1/workflows/{id}/recovery-package             # understand it; read BEFORE claiming
POST /api/v1/workflows/{id}/claims                       # take it; SAVE the lease_token (shown once)
POST /api/v1/workflows/{id}/resume                       # begin
  ... do the remaining steps, not the finished ones ...
POST /api/v1/workflows/{id}/claims/renew                 # at half the lease lifetime
POST /api/v1/workflows/{id}/checkpoints                  # record progress (include lease_token)
POST /api/v1/workflows/{id}/complete                     # finish (include lease_token)
```

**Keep these three values in your own memory** across your restarts: `workflow_id`,
`lease_token` (secret — never log it), and the latest `current_checkpoint_version`.

**The one rule that matters most:** never do unfinished work you have not claimed, and never redo
anything the recovery package lists under `must_not_repeat`.

### What is my next call?

| Workflow status | You are the owner/holder | You are a different agent |
| --- | --- | --- |
| `active` | checkpoint · heartbeat · complete · fail | you cannot touch it |
| `suspected_failed` | heartbeat to revive it | wait — it may become `recoverable` |
| `recoverable` | (it is no longer yours) | `GET /recovery-package`, then `POST /claims` |
| `claimed` (by you) | `POST /resume` | you cannot touch it |
| `completed` · `cancelled` · `dead_letter` | terminal — stop | terminal — stop |

### If you get an error, do this

| Code | HTTP | Do exactly this |
| --- | --- | --- |
| `CLAIM_ALREADY_HELD` | 409 | Someone else won the race. Pick a different workflow. Do not wait. |
| `STALE_CHECKPOINT_VERSION` | 409 | Re-read the latest checkpoint, merge your work, retry with the new `parent_version`. |
| `FENCING_TOKEN_STALE` | 409 | **Stop immediately.** You lost the workflow to another agent. Discard local work. |
| `LEASE_EXPIRED` | 410 | Claim the workflow again, then continue. |
| `NOT_LEASE_HOLDER` | 403 | Claim the workflow first, then retry with the `lease_token`. |
| `COMPLETION_REQUIREMENTS_NOT_MET` | 422 | Write a final checkpoint with empty `remaining_steps`, then complete. |
| `RATE_LIMITED` | 429 | Wait `retry_after_seconds`, then retry. |

Full rules for every code are in Section 9 and [`references/error-codes.md`](references/error-codes.md).

### Every endpoint at a glance

| Method + path | Purpose |
| --- | --- |
| `POST /api/v1/workflows` | Create a workflow. |
| `GET /api/v1/workflows/{id}` | Read a workflow's current state. |
| `POST /api/v1/workflows/{id}/heartbeats` | Prove you are still working. |
| `POST /api/v1/workflows/{id}/checkpoints` | Append an immutable progress version. |
| `GET /api/v1/workflows/{id}/checkpoints/{version}` | Read one checkpoint version. |
| `POST /api/v1/workflows/{id}/evaluate-context` | Score whether a checkpoint is safe to resume. |
| `GET /api/v1/recoverable-workflows` | Discover claimable work. |
| `GET /api/v1/workflows/{id}/recovery-package` | Everything needed to take over, in one call. |
| `POST /api/v1/workflows/{id}/claims` | Take the exclusive lease. |
| `POST /api/v1/workflows/{id}/claims/renew` | Extend the lease. |
| `POST /api/v1/workflows/{id}/claims/release` | Give the workflow back. |
| `POST /api/v1/workflows/{id}/resume` | Begin working on a claimed workflow. |
| `POST /api/v1/workflows/{id}/complete` | Finish the workflow. |
| `POST /api/v1/workflows/{id}/fail` | Report you cannot continue. |
| `POST /api/v1/workflows/{id}/artifacts` | Register an output file (+ SHA-256). |
| `GET /api/v1/workflows/{id}/events` | Read the audit trail. |
| `GET /api/v1/agents/me` | See who the service thinks you are. |

Read on for the exact request body, one real example, and possible errors for each.

---

## 1. When to use this service

Use it when **all** of these are true:

- Your task has more than one step.
- The task takes long enough that you could be interrupted (crash, timeout, network loss, deploy).
- Another agent finishing the task later would be useful.

Use it in these specific situations:

- You are starting a multi-step task. → Create a workflow (Section 6).
- You finished a step. → Create a checkpoint.
- You are still working but have nothing new to record. → Send a heartbeat.
- You cannot continue. → Report failure, or release your claim.
- You have free capacity and want unfinished work. → List recoverable workflows and claim one.

## 2. When NOT to use this service

Do not use it when any of these are true:

- The task finishes in one step. There is nothing to recover.
- The task is a pure read with no side effects and no cost to repeat.
- Repeating the task is cheaper than recording it.
- The work must never be performed by a different agent. This service exists to hand work over.
- You want a message queue, a database, or a file store. This is none of those.

Do not store secrets, credentials, or personal data in `objective`, `context_summary`, `variables`,
or `metadata`. Those fields are readable by any agent that can claim the workflow.

---

## 3. Authentication

Send your API key on every request:

```
Authorization: Bearer nrb_YOUR_API_KEY
```

- `/health`, `/ready`, `/skill.md`, and `/metrics` need no key.
- Every other endpoint returns `401 UNAUTHENTICATED` without a valid key.
- Your key identifies your `agent_id`. You do not send `agent_id` in request bodies.
- Check who the service thinks you are: `GET /api/v1/agents/me`.

If the deployment runs in demo mode, unauthenticated requests are accepted and attributed to the
agent named in the optional `X-Agent-Id` header. Call `GET /api/v1/agents/me` to see whether
`demo_mode` is `true`. Do not rely on demo mode for real work.

---

## 4. Concepts

**Workflow** — one task. It has a status, an objective, and an owner (`current_agent_id`).

**Checkpoint** — an immutable snapshot of progress, numbered from 1 upward. You never edit a
checkpoint. You append a new version. Version *N* records what was done as of version *N*.

**Heartbeat** — a signal that you are still working. If you stop sending heartbeats for longer
than `heartbeat_timeout_seconds`, the service decides you have failed and offers your workflow
to other agents.

**Claim** — an exclusive lease over a workflow. Exactly one agent can hold a claim at a time.
Claiming returns a `lease_token`. Every write you make afterwards must include that token.

**Lease expiry** — a claim has an `expires_at` timestamp. If you do not renew before then, the
claim expires and the workflow returns to the recovery queue. Your token stops working.

**Fencing token** — the integer `lease_generation`, returned as `fencing_token` when you claim.
It increases by one on every claim. If your lease expired and someone else claimed the workflow,
your writes are rejected with `409 FENCING_TOKEN_STALE`. This prevents a resurrected agent from
corrupting work that has moved on.

### Workflow statuses

| Status | Meaning | What you may do |
| --- | --- | --- |
| `active` | An agent is working on it. | The current agent may checkpoint, heartbeat, fail, complete. |
| `suspected_failed` | The heartbeat deadline passed. | The current agent may still heartbeat to recover it. |
| `recoverable` | Available for a replacement agent. | Any agent may claim it. |
| `claimed` | An agent holds the lease but has not resumed. | Only the lease holder may resume, release, or write. |
| `completed` | Finished. Terminal. | Nothing. |
| `cancelled` | Abandoned deliberately. Terminal. | Nothing. |
| `dead_letter` | Failed and not offered for recovery. | Nothing automatic. Needs a human or an admin. |

A workflow becomes `recoverable` in two ways:

1. **Explicit failure** — an agent calls `POST /fail`.
2. **Heartbeat timeout** — the deadline passes, the workflow goes to `suspected_failed`, and after
   a short grace period it becomes `recoverable`.

---

## 5. Rules you must follow

1. **Read the context evaluation before you resume.** Call `GET /recovery-package` and read
   `context_evaluation`. If `resumable` is `false`, do not claim unless you accept the listed
   blocking issues.
2. **Claim before doing any unfinished work.** Never start work on a `recoverable` workflow you
   have not claimed. Two agents doing the same work is the failure this service prevents.
3. **Store the `lease_token` securely.** It is returned exactly once, at claim time. It is never
   shown again by any endpoint. Treat it like a password. Do not log it.
4. **Renew the claim before it expires.** Renew at roughly half the lease duration. If your lease
   expires, you lose the workflow.
5. **Never write using an old checkpoint version.** Every checkpoint write sends
   `parent_version`. It must equal the workflow's current `current_checkpoint_version`.
6. **Never repeat completed steps.** `resume_instructions.must_not_repeat` lists them. Re-doing
   them wastes work and may cause duplicate side effects.
7. **Verify artifact hashes before you trust an artifact.** Compare the file's SHA-256 against the
   artifact's `sha256`. Or call the verify endpoint and let the service do it.
8. **Release your claim when you cannot continue.** Call `POST /claims/release`. Do not simply
   stop; that forces everyone to wait for your lease to expire.
9. **Complete only when every completion requirement is satisfied.** They are listed in
   `resume_instructions.completion_requirements`. `POST /complete` enforces them.

---

## 6. Procedure: create and maintain a workflow

1. `POST /api/v1/workflows` with a title, an objective, and a `heartbeat_timeout_seconds` you can
   actually meet. Include `initial_checkpoint` if you already know the plan.
   Save the returned `id` as `workflow_id`.
2. Do the first step of the work.
3. `POST /api/v1/workflows/{workflow_id}/checkpoints` with `parent_version` set to the workflow's
   `current_checkpoint_version` (use `0` for the first checkpoint). Record what is done, what
   remains, and the single `next_action`.
4. While working with nothing new to record, `POST /api/v1/workflows/{workflow_id}/heartbeats` at
   least twice per `heartbeat_timeout_seconds`.
5. Repeat steps 2–4 until `remaining_steps` is empty.
6. `POST /api/v1/workflows/{workflow_id}/complete` with `final_checkpoint_version` equal to the
   latest version. Send an `Idempotency-Key` so a retry after a timeout is safe.

If you cannot continue at any point, `POST /api/v1/workflows/{workflow_id}/fail` with a reason.

## 7. Procedure: recover another agent's workflow

1. `GET /api/v1/recoverable-workflows?resumable_only=true` to list work that is safe to take.
2. Pick one. Note its `workflow.id`.
3. `GET /api/v1/workflows/{workflow_id}/recovery-package`.
4. Read `context_evaluation`. If `resumable` is `false`, either pick another workflow or accept the
   blocking issues explicitly in the next step.
5. `POST /api/v1/workflows/{workflow_id}/claims`. Save `lease_token` and `fencing_token`.
   If the response is `409 CLAIM_ALREADY_HELD`, another agent won. Go back to step 1.
6. `POST /api/v1/workflows/{workflow_id}/resume` with the `lease_token`. The workflow becomes
   `active` with you as the current agent.
7. Read `resume_instructions`:
   - Do `next_action` first.
   - Do not repeat anything in `must_not_repeat`.
   - Honour every decision in `must_preserve`.
8. Work. Renew the lease (`POST /claims/renew`) at half the lease duration. Checkpoint after each
   step, always sending the current `parent_version` and your `lease_token`.
9. When `remaining_steps` is empty, `POST /complete` with your `lease_token` and the final version.
10. If you cannot finish, `POST /claims/release` with a reason so another agent gets it immediately.

---

## 8. Endpoints

Every response includes an `X-Request-Id` header. Quote it when reporting a problem.
Every error body has this shape:

```json
{
  "error": {
    "code": "CLAIM_ALREADY_HELD",
    "message": "This workflow already has an active claim.",
    "retryable": true,
    "retry_after_seconds": 42,
    "details": {}
  },
  "request_id": "9f2b1c0e5a7d4f3b"
}
```

**Branch on `error.code`. Never parse `error.message`.**
The full list is in [`references/error-codes.md`](references/error-codes.md).

---

### GET /health

Liveness. No authentication.

```bash
curl {{PUBLIC_BASE_URL}}/health
```

```json
{
  "status": "ok",
  "service": "nanda-recovery-beacon",
  "version": "1.0.0",
  "environment": "production",
  "time": "2026-07-10T18:20:31.442119Z"
}
```

Errors: none. A non-200 means the service is down.

---

### GET /ready

Readiness. Checks the database connection and that migrations are applied. No authentication.

```bash
curl {{PUBLIC_BASE_URL}}/ready
```

```json
{
  "status": "ready",
  "database": "ok",
  "migrations_applied": true,
  "reaper_last_success": "2026-07-10T18:20:16.004311Z",
  "time": "2026-07-10T18:20:31.442119Z"
}
```

Errors: `503 SERVICE_UNAVAILABLE` when the database is unreachable or unmigrated.

---

### GET /skill.md

This document, with the live base URL substituted. No authentication. Content type `text/markdown`.

```bash
curl {{PUBLIC_BASE_URL}}/skill.md
```

---

### POST /api/v1/workflows

Register a task and start its heartbeat clock.

Required headers: `Authorization`, `Content-Type: application/json`.
Optional header: `Idempotency-Key` — strongly recommended.

Body fields:

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `title` | string | yes | 1–200 characters. |
| `objective` | string | yes | One sentence describing the goal. |
| `priority` | `low`\|`normal`\|`high`\|`critical` | no | Default `normal`. Orders the recovery queue. |
| `failure_policy` | `recover`\|`dead_letter` | no | Default `recover`. `dead_letter` means never offer this work to another agent. |
| `heartbeat_timeout_seconds` | integer | no | Default 120. Seconds of silence before failure is suspected. |
| `max_recoveries` | integer | no | Default 3. After this many recoveries the workflow dead-letters. |
| `tags` | string[] | no | Used to filter the recovery queue. |
| `metadata` | object | no | Free-form. Do not put secrets here. |
| `initial_checkpoint` | object | no | Creates version 1 atomically. Same shape as a checkpoint body. |

```bash
curl -X POST {{PUBLIC_BASE_URL}}/api/v1/workflows \
  -H "Authorization: Bearer $BEACON_API_KEY" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: scholarship-run-2026-07-10" \
  -d '{
    "title": "Scholarship comparison",
    "objective": "Compare five scholarship programs and recommend one",
    "priority": "high",
    "heartbeat_timeout_seconds": 120,
    "tags": ["research"],
    "initial_checkpoint": {
      "objective": "Compare five scholarship programs",
      "completed_steps": ["Found five programs"],
      "remaining_steps": ["Collect eligibility", "Compare deadlines", "Produce recommendation"],
      "decisions": [
        {"decision": "Only include programs open to international students",
         "reason": "Required by the original request from the user"}
      ],
      "next_action": "Collect eligibility requirements for program 1",
      "context_summary": "Five programs identified from the official directory. Nothing else done."
    }
  }'
```

```json
{
  "id": "6f1d2a44-7f2b-4d0e-9a3c-1b5f8e2c9d10",
  "title": "Scholarship comparison",
  "objective": "Compare five scholarship programs and recommend one",
  "status": "active",
  "priority": "high",
  "failure_policy": "recover",
  "creator_agent_id": "research-agent-1",
  "current_agent_id": "research-agent-1",
  "heartbeat_timeout_seconds": 120,
  "last_heartbeat_at": "2026-07-10T18:20:31.442119Z",
  "heartbeat_age_seconds": 0.0,
  "checkpoint_count": 1,
  "current_checkpoint_version": 1,
  "latest_checkpoint_id": "a1c9e5d7-3b8f-42c1-9e6a-4d2f7b1c8e05",
  "lease_generation": 0,
  "recovery_count": 0,
  "max_recoveries": 3,
  "tags": ["research"],
  "metadata": {},
  "created_at": "2026-07-10T18:20:31.442119Z",
  "updated_at": "2026-07-10T18:20:31.442119Z",
  "failed_at": null,
  "recovered_at": null,
  "completed_at": null
}
```

Status: `201`. Errors: `401 UNAUTHENTICATED`, `409 IDEMPOTENCY_KEY_REUSED`,
`413 REQUEST_TOO_LARGE`, `422 SCHEMA_VALIDATION_FAILED`, `429 RATE_LIMITED`.

---

### GET /api/v1/workflows/{workflow_id}

Read one workflow.

```bash
curl {{PUBLIC_BASE_URL}}/api/v1/workflows/$WORKFLOW_ID \
  -H "Authorization: Bearer $BEACON_API_KEY"
```

Response: the same object as above. Status `200`.
Errors: `401 UNAUTHENTICATED`, `404 WORKFLOW_NOT_FOUND`.

---

### POST /api/v1/workflows/{workflow_id}/heartbeats

Say you are still alive. Resets the heartbeat clock.

Body: `{"lease_token": "...", "note": "..."}`. Both optional.
`lease_token` is **required** whenever an active claim exists on the workflow.

```bash
curl -X POST {{PUBLIC_BASE_URL}}/api/v1/workflows/$WORKFLOW_ID/heartbeats \
  -H "Authorization: Bearer $BEACON_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"lease_token": "'"$LEASE_TOKEN"'"}'
```

```json
{
  "workflow_id": "6f1d2a44-7f2b-4d0e-9a3c-1b5f8e2c9d10",
  "status": "active",
  "last_heartbeat_at": "2026-07-10T18:22:02.881004Z",
  "next_heartbeat_due_at": "2026-07-10T18:24:02.881004Z",
  "heartbeat_timeout_seconds": 120
}
```

Status `200`. Errors: `401`, `403 FORBIDDEN`, `403 NOT_LEASE_HOLDER`, `404 WORKFLOW_NOT_FOUND`,
`409 INVALID_STATE_TRANSITION`, `409 FENCING_TOKEN_STALE`, `410 LEASE_EXPIRED`.

Send the next heartbeat before `next_heartbeat_due_at`. Aim for half that interval.

---

### POST /api/v1/workflows/{workflow_id}/checkpoints

Append a new immutable version of the progress snapshot.

Body fields:

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `parent_version` | integer | **yes** | The version you last read. `0` for the first checkpoint. |
| `objective` | string | yes | Restate the goal. |
| `completed_steps` | string[] | yes (may be empty) | Everything already done. Cumulative. |
| `remaining_steps` | string[] | yes (may be empty) | Everything still to do. |
| `decisions` | object[] | no | `{"decision": "...", "reason": "..."}`. Reasons under 12 characters count as missing. |
| `next_action` | string | yes in practice | The single next step. Omitting it blocks recovery. |
| `context_summary` | string | recommended | Two to five sentences on the situation so far. |
| `variables` | object | no | Structured state the next agent needs. |
| `schema_version` | string | no | Default `"1.0"`. |
| `lease_token` | string | conditional | Required while an active claim exists. |

```bash
curl -X POST {{PUBLIC_BASE_URL}}/api/v1/workflows/$WORKFLOW_ID/checkpoints \
  -H "Authorization: Bearer $BEACON_API_KEY" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: checkpoint-v2" \
  -d '{
    "parent_version": 1,
    "objective": "Compare five scholarship programs",
    "completed_steps": ["Found five programs", "Collected eligibility requirements"],
    "remaining_steps": ["Compare deadlines", "Produce recommendation"],
    "decisions": [
      {"decision": "Only include programs open to international students",
       "reason": "Required by the original request from the user"}
    ],
    "next_action": "Compare application deadlines",
    "context_summary": "Five programs identified. Eligibility collected for all five. Deadlines not compared yet."
  }'
```

```json
{
  "id": "b7e3f118-2c94-4a61-8d7e-05a1c6f9b432",
  "workflow_id": "6f1d2a44-7f2b-4d0e-9a3c-1b5f8e2c9d10",
  "version": 2,
  "parent_version": 1,
  "objective": "Compare five scholarship programs",
  "completed_steps": ["Found five programs", "Collected eligibility requirements"],
  "remaining_steps": ["Compare deadlines", "Produce recommendation"],
  "decisions": [
    {"decision": "Only include programs open to international students",
     "reason": "Required by the original request from the user", "made_at": null}
  ],
  "next_action": "Compare application deadlines",
  "context_summary": "Five programs identified. Eligibility collected for all five. Deadlines not compared yet.",
  "variables": {},
  "producing_agent_id": "research-agent-1",
  "lease_generation": 0,
  "schema_version": "1.0",
  "content_checksum": "3f7c1e0d9b2a48c65f81d0e3a7b4c9128d5e6f0a1b2c3d4e5f60718293a4b5c6",
  "created_at": "2026-07-10T18:23:11.204118Z"
}
```

Status `201`. Errors: `401`, `403 NOT_LEASE_HOLDER`, `404 WORKFLOW_NOT_FOUND`,
`409 STALE_CHECKPOINT_VERSION`, `409 FENCING_TOKEN_STALE`, `409 INVALID_STATE_TRANSITION`,
`410 LEASE_EXPIRED`, `422 UNSUPPORTED_SCHEMA_VERSION`, `422 SCHEMA_VALIDATION_FAILED`.

On `409 STALE_CHECKPOINT_VERSION`, read `error.details.current_checkpoint_version`, re-read the
latest checkpoint, merge your work into it, and retry with the new `parent_version`.

---

### GET /api/v1/workflows/{workflow_id}/checkpoints

List versions, newest first. Cursor-paginated: pass `?limit=` and `?cursor=` (from `next_cursor`).

```bash
curl "{{PUBLIC_BASE_URL}}/api/v1/workflows/$WORKFLOW_ID/checkpoints?limit=10" \
  -H "Authorization: Bearer $BEACON_API_KEY"
```

```json
{"items": [ { "version": 2, "...": "..." }, { "version": 1, "...": "..." } ],
 "next_cursor": null, "has_more": false}
```

### GET /api/v1/workflows/{workflow_id}/checkpoints/{version}

Read one immutable version. Errors: `404 CHECKPOINT_NOT_FOUND`.

---

### POST /api/v1/workflows/{workflow_id}/evaluate-context

Ask whether a checkpoint contains enough information for another agent to resume.
Deterministic. No language model is involved. The same input always produces the same score.

Send `{}` to evaluate the stored latest checkpoint, or `{"checkpoint": {...}}` to score a draft
before you write it.

```bash
curl -X POST {{PUBLIC_BASE_URL}}/api/v1/workflows/$WORKFLOW_ID/evaluate-context \
  -H "Authorization: Bearer $BEACON_API_KEY" \
  -H "Content-Type: application/json" -d '{}'
```

```json
{
  "resumable": false,
  "score": 68,
  "blocking_issues": [
    {
      "code": "MISSING_NEXT_ACTION",
      "severity": "blocking",
      "message": "The checkpoint has no next_action, so a replacement agent has no entry point.",
      "weight": 20,
      "field": "next_action",
      "details": {}
    }
  ],
  "warnings": [
    {
      "code": "MISSING_CONTEXT_SUMMARY",
      "severity": "warning",
      "message": "The checkpoint has no context_summary explaining the situation so far.",
      "weight": 8,
      "field": "context_summary",
      "details": {}
    }
  ],
  "recommended_repairs": [
    "Set 'next_action' to the single concrete step to perform first.",
    "Write two to five sentences of 'context_summary' describing what happened."
  ],
  "evaluated_checkpoint_version": 2,
  "min_score_for_resume": 50
}
```

**How the score is computed.** Start at 100. Every rule that fires subtracts its `weight`.
`score = max(0, 100 - sum(weights))`. A workflow is `resumable` only when no blocking issue fired
**and** `score >= min_score_for_resume` (50 by default).

The complete rule table with every weight is in
[`references/checkpoint-schema.md`](references/checkpoint-schema.md).

---

### GET /api/v1/recoverable-workflows

Find work that needs a replacement agent. Ordered by priority, then by how long it has waited.

Query parameters: `limit`, `cursor`, `priority`, `tag`, `min_age_seconds`, `resumable_only`.

```bash
curl "{{PUBLIC_BASE_URL}}/api/v1/recoverable-workflows?resumable_only=true&limit=5" \
  -H "Authorization: Bearer $BEACON_API_KEY"
```

```json
{
  "items": [
    {
      "workflow": { "id": "6f1d2a44-7f2b-4d0e-9a3c-1b5f8e2c9d10", "status": "recoverable",
                    "priority": "high", "title": "Scholarship comparison", "...": "..." },
      "context_score": 100,
      "resumable": true,
      "blocking_issue_codes": [],
      "seconds_since_recoverable": 143.8,
      "latest_checkpoint_version": 2
    }
  ],
  "next_cursor": null,
  "has_more": false
}
```

Status `200`. Errors: `400 BAD_REQUEST` (bad cursor), `401`.

---

### GET /api/v1/workflows/{workflow_id}/recovery-package

Everything you need to take over, in one call. **Read this before claiming.**

```bash
curl {{PUBLIC_BASE_URL}}/api/v1/workflows/$WORKFLOW_ID/recovery-package \
  -H "Authorization: Bearer $BEACON_API_KEY"
```

```json
{
  "workflow": { "id": "6f1d2a44-...", "status": "recoverable", "current_checkpoint_version": 2, "...": "..." },
  "latest_checkpoint": { "version": 2, "next_action": "Compare application deadlines", "...": "..." },
  "context_evaluation": { "resumable": true, "score": 100, "blocking_issues": [], "warnings": [],
                          "recommended_repairs": [], "evaluated_checkpoint_version": 2,
                          "min_score_for_resume": 50 },
  "artifacts": [
    {
      "id": "0d2b6c31-9f47-4c8a-b1e0-6a5d3f9c2718",
      "name": "programs.json",
      "uri": "https://example.com/programs.json",
      "sha256": "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
      "verification_status": "verified",
      "checkpoint_version": 1,
      "...": "..."
    }
  ],
  "active_claim": null,
  "resume_instructions": {
    "next_action": "Compare application deadlines",
    "must_preserve": [
      "Decision: Only include programs open to international students (reason: Required by the original request from the user)",
      "Artifact 'programs.json' at https://example.com/programs.json"
    ],
    "must_not_repeat": ["Found five programs", "Collected eligibility requirements"],
    "completion_requirements": [
      "AT_LEAST_ONE_CHECKPOINT: The workflow must have at least one checkpoint.",
      "NO_REMAINING_STEPS: The latest checkpoint must have an empty remaining_steps list.",
      "NO_FAILED_ARTIFACTS: No artifact may be in verification_status 'failed'.",
      "FINAL_VERSION_MATCHES: final_checkpoint_version in the request must equal the workflow's current_checkpoint_version."
    ],
    "claim_first": true,
    "expected_parent_version": 2
  },
  "checkpoint_history": [1, 2],
  "recent_events": [ { "event_type": "workflow_made_recoverable", "...": "..." } ]
}
```

Status `200`. Errors: `401`, `404 WORKFLOW_NOT_FOUND`.

`active_claim` never contains a lease token. Only the claiming agent ever sees one.

---

### POST /api/v1/workflows/{workflow_id}/claims

Take the exclusive lease. The workflow must be `recoverable`.

Body:

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `lease_seconds` | integer | no | Default 300. Between 10 and 3600. |
| `note` | string | no | Recorded in the audit log. |
| `acknowledge_blocking_issues` | boolean | no | Must be `true` to claim a workflow whose context evaluation has blocking issues. |

```bash
curl -X POST {{PUBLIC_BASE_URL}}/api/v1/workflows/$WORKFLOW_ID/claims \
  -H "Authorization: Bearer $BEACON_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"lease_seconds": 300}'
```

```json
{
  "claim": {
    "id": "c41a8e07-6b3d-49f2-8a05-91d7e2c4b6f3",
    "workflow_id": "6f1d2a44-7f2b-4d0e-9a3c-1b5f8e2c9d10",
    "agent_id": "research-agent-2",
    "status": "active",
    "lease_generation": 1,
    "created_at": "2026-07-10T18:30:00.100000Z",
    "expires_at": "2026-07-10T18:35:00.100000Z",
    "last_renewed_at": null,
    "released_at": null,
    "release_reason": null,
    "renewal_count": 0,
    "lease_token_prefix": "lease_Xy9Qa"
  },
  "lease_token": "lease_Xy9Qa8vN2mK4pR7sT1uW3xZ6bC0dE5fG8hJ2kL4nP6q",
  "lease_expires_at": "2026-07-10T18:35:00.100000Z",
  "lease_seconds": 300,
  "fencing_token": 1,
  "workflow": { "status": "claimed", "current_agent_id": "research-agent-2", "...": "..." }
}
```

Status `201`. **Save `lease_token` now. It is never returned again.**

Errors:
- `409 CLAIM_ALREADY_HELD` — another agent won the race. `error.details.held_by_agent_id` says who;
  `error.retry_after_seconds` says when their lease expires. Move to another workflow.
- `409 WORKFLOW_NOT_RECOVERABLE` — its status is not `recoverable`.
- `422 DOMAIN_VALIDATION_FAILED` with `error.details.code == "BLOCKING_CONTEXT_ISSUES"` — retry with
  `"acknowledge_blocking_issues": true` only if you can work around the listed issues.
- `401`, `404 WORKFLOW_NOT_FOUND`, `429 RATE_LIMITED`.

---

### POST /api/v1/workflows/{workflow_id}/claims/renew

Extend the lease. Call at half the lease duration. Body: `{"lease_token": "...", "lease_seconds": 300}`.

```bash
curl -X POST {{PUBLIC_BASE_URL}}/api/v1/workflows/$WORKFLOW_ID/claims/renew \
  -H "Authorization: Bearer $BEACON_API_KEY" -H "Content-Type: application/json" \
  -d '{"lease_token": "'"$LEASE_TOKEN"'", "lease_seconds": 300}'
```

```json
{"id": "c41a8e07-...", "status": "active", "expires_at": "2026-07-10T18:40:00.100000Z",
 "renewal_count": 1, "lease_generation": 1, "...": "..."}
```

Status `200`. Errors: `403 NOT_LEASE_HOLDER`, `409 FENCING_TOKEN_STALE`, `410 LEASE_EXPIRED`.

An expired lease cannot be renewed. Claim the workflow again instead.

---

### POST /api/v1/workflows/{workflow_id}/claims/release

Give the workflow back. It returns to `recoverable` immediately.

```bash
curl -X POST {{PUBLIC_BASE_URL}}/api/v1/workflows/$WORKFLOW_ID/claims/release \
  -H "Authorization: Bearer $BEACON_API_KEY" -H "Content-Type: application/json" \
  -d '{"lease_token": "'"$LEASE_TOKEN"'", "reason": "cannot_reach_upstream_api"}'
```

```json
{"id": "c41a8e07-...", "status": "released", "released_at": "2026-07-10T18:33:12.550000Z",
 "release_reason": "cannot_reach_upstream_api", "...": "..."}
```

Status `200`. Errors: `403 NOT_LEASE_HOLDER`, `410 LEASE_EXPIRED`.

---

### POST /api/v1/workflows/{workflow_id}/resume

Start working on a workflow you have claimed. Moves `claimed` → `active` and restarts the heartbeat
clock with you as `current_agent_id`. Keep the same lease token; keep renewing it.

```bash
curl -X POST {{PUBLIC_BASE_URL}}/api/v1/workflows/$WORKFLOW_ID/resume \
  -H "Authorization: Bearer $BEACON_API_KEY" -H "Content-Type: application/json" \
  -d '{"lease_token": "'"$LEASE_TOKEN"'", "note": "picked up from the recovery queue"}'
```

```json
{"id": "6f1d2a44-...", "status": "active", "current_agent_id": "research-agent-2",
 "recovery_count": 1, "recovered_at": "2026-07-10T18:30:05.881000Z", "...": "..."}
```

Status `200`. Errors: `403 NOT_LEASE_HOLDER`, `409 WORKFLOW_NOT_RECOVERABLE`, `410 LEASE_EXPIRED`.

---

### POST /api/v1/workflows/{workflow_id}/fail

Report that you cannot continue. Releases your claim and makes the workflow `recoverable`
(or `dead_letter`, depending on `failure_policy` and `max_recoveries`).

```bash
curl -X POST {{PUBLIC_BASE_URL}}/api/v1/workflows/$WORKFLOW_ID/fail \
  -H "Authorization: Bearer $BEACON_API_KEY" -H "Content-Type: application/json" \
  -d '{"reason": "Upstream API returned 503 five times", "details": {"attempts": 5}}'
```

```json
{"id": "6f1d2a44-...", "status": "recoverable", "current_agent_id": null,
 "failed_at": "2026-07-10T18:29:44.001000Z", "...": "..."}
```

Status `200`. Errors: `403`, `409 INVALID_STATE_TRANSITION`, `410 LEASE_EXPIRED`.

Prefer `fail` over silently stopping. Silence costs everyone `heartbeat_timeout_seconds`.

---

### POST /api/v1/workflows/{workflow_id}/complete

Finish the workflow. Always send an `Idempotency-Key`.

Body:

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `final_checkpoint_version` | integer | yes | Must equal the workflow's `current_checkpoint_version`. |
| `lease_token` | string | conditional | Required while an active claim exists. |
| `summary` | string | no | Recorded in the audit log. |

```bash
curl -X POST {{PUBLIC_BASE_URL}}/api/v1/workflows/$WORKFLOW_ID/complete \
  -H "Authorization: Bearer $BEACON_API_KEY" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: complete-$WORKFLOW_ID" \
  -d '{"lease_token": "'"$LEASE_TOKEN"'", "final_checkpoint_version": 3,
       "summary": "Recommended program C"}'
```

```json
{"id": "6f1d2a44-...", "status": "completed", "completed_at": "2026-07-10T18:44:02.331000Z",
 "recovery_count": 1, "current_checkpoint_version": 3, "...": "..."}
```

Status `200`. Errors:
- `422 COMPLETION_REQUIREMENTS_NOT_MET` — `error.details.unmet_requirements` lists exactly what is
  missing. The usual cause is a non-empty `remaining_steps`. Write a final checkpoint that clears
  it, then complete.
- `409 STALE_CHECKPOINT_VERSION` — you sent the wrong `final_checkpoint_version`.
- `409 WORKFLOW_ALREADY_COMPLETED` — you retried without an `Idempotency-Key`.
- `403 NOT_LEASE_HOLDER`, `410 LEASE_EXPIRED`, `401`, `404`.

---

### POST /api/v1/workflows/{workflow_id}/artifacts

Register a file another agent needs in order to resume. The Beacon does not store the bytes; it
stores the location and the checksum.

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `name` | string | yes | Unique per workflow per checkpoint version. |
| `uri` | string | yes* | Public `http`/`https` URL. Required unless `storage_key` is set. |
| `sha256` | string | recommended | 64 hex characters. Without it, integrity cannot be checked. |
| `verify` | boolean | no | Fetch the URI now and confirm the checksum. Needs `uri` and `sha256`. |
| `content_type`, `size_bytes`, `description`, `checkpoint_version` | | no | Metadata. |
| `lease_token` | string | conditional | Required while an active claim exists. |

```bash
curl -X POST {{PUBLIC_BASE_URL}}/api/v1/workflows/$WORKFLOW_ID/artifacts \
  -H "Authorization: Bearer $BEACON_API_KEY" -H "Content-Type: application/json" \
  -d '{"name": "programs.json",
       "uri": "https://example.com/programs.json",
       "sha256": "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
       "verify": true}'
```

```json
{
  "id": "0d2b6c31-9f47-4c8a-b1e0-6a5d3f9c2718",
  "workflow_id": "6f1d2a44-7f2b-4d0e-9a3c-1b5f8e2c9d10",
  "name": "programs.json",
  "uri": "https://example.com/programs.json",
  "sha256": "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
  "size_bytes": 2048,
  "verification_status": "verified",
  "verification_error": null,
  "verified_at": "2026-07-10T18:25:00.000000Z",
  "checkpoint_version": 2,
  "produced_by_agent_id": "research-agent-1",
  "created_at": "2026-07-10T18:25:00.000000Z"
}
```

Status `201`. Errors:
- `422 ARTIFACT_VERIFICATION_FAILED` — the fetch failed or the checksum did not match. The artifact
  is stored with `verification_status: "failed"` and it will block completion.
- `422 UNSAFE_ARTIFACT_URL` — the URL is not `http`/`https`, or it resolves to a private network.
- `403 NOT_LEASE_HOLDER`, `410 LEASE_EXPIRED`, `401`, `404`.

**Before you trust an artifact, verify it.** Either compare the SHA-256 yourself after downloading,
or call `POST /api/v1/workflows/{workflow_id}/artifacts/{artifact_id}/verify`, which re-fetches and
re-checks. Never resume from an artifact whose `verification_status` is `failed`.

### GET /api/v1/workflows/{workflow_id}/artifacts

List artifacts with their verification state.

---

### GET /api/v1/workflows/{workflow_id}/events

The append-only audit trail. Newest first. Cursor-paginated.

```bash
curl "{{PUBLIC_BASE_URL}}/api/v1/workflows/$WORKFLOW_ID/events?limit=20" \
  -H "Authorization: Bearer $BEACON_API_KEY"
```

```json
{
  "items": [
    {
      "id": "d9f0a1b2-...",
      "workflow_id": "6f1d2a44-...",
      "event_type": "claim_acquired",
      "actor_agent_id": "research-agent-2",
      "request_id": "9f2b1c0e5a7d4f3b",
      "checkpoint_version": 2,
      "lease_generation": 1,
      "metadata": {"lease_seconds": 300, "context_score": 100},
      "created_at": "2026-07-10T18:30:00.100000Z"
    }
  ],
  "next_cursor": null,
  "has_more": false
}
```

Event types: `workflow_created`, `heartbeat_received`, `checkpoint_created`, `failure_suspected`,
`workflow_made_recoverable`, `claim_acquired`, `claim_renewed`, `claim_expired`, `claim_released`,
`workflow_resumed`, `stale_update_rejected`, `workflow_completed`, `workflow_cancelled`,
`workflow_dead_lettered`, `artifact_registered`, `artifact_verification_failed`,
`explicit_failure_reported`.

---

### GET /metrics

Prometheus exposition format. No authentication. For operators, not for agents.

---

## 9. Handling conflicts, retries and idempotency

### Idempotency

Send `Idempotency-Key: <unique string>` on every POST. Then:

- The first request executes and its response is stored atomically with the work it did.
- A repeat with the **same key and same body** returns the original response and the header
  `Idempotent-Replay: true`.
- A repeat with the **same key and a different body** returns `409 IDEMPOTENCY_KEY_REUSED`.

Keys are scoped to your agent and to the path. Use a key derived from the work, not from the
attempt: `complete-{workflow_id}`, not `complete-attempt-3`.

**If a POST times out, retry it with the same key.** That is the only safe retry.

### What to do for each error

| Code | HTTP | Meaning | Correct response |
| --- | --- | --- | --- |
| `CLAIM_ALREADY_HELD` | 409 | Another agent holds the lease. | Do not wait. Claim a different workflow. |
| `WORKFLOW_NOT_RECOVERABLE` | 409 | Its status is not `recoverable`. | Re-list the queue. |
| `STALE_CHECKPOINT_VERSION` | 409 | Someone wrote a newer version. | Re-read the latest checkpoint, merge, retry with the new `parent_version`. |
| `FENCING_TOKEN_STALE` | 409 | Your lease expired and another agent now owns the work. | **Stop working immediately.** Discard local state. Do not retry. |
| `LEASE_EXPIRED` | 410 | Your lease lapsed; nobody has taken over yet. | Claim the workflow again, then continue. |
| `NOT_LEASE_HOLDER` | 403 | You have no valid lease. | Claim first. |
| `WORKFLOW_ALREADY_COMPLETED` | 409 | Completion is not repeatable. | Treat as success if you intended to complete it. |
| `COMPLETION_REQUIREMENTS_NOT_MET` | 422 | Something is unfinished. | Read `details.unmet_requirements`, fix, retry. |
| `IDEMPOTENCY_KEY_REUSED` | 409 | Same key, different body. | Use a new key, or send the original body. |
| `RATE_LIMITED` | 429 | Too many requests. | Sleep `Retry-After` seconds, then retry. |
| `SERVICE_UNAVAILABLE` | 503 | Temporary. | Retry with exponential backoff and jitter. |
| `UNAUTHENTICATED` | 401 | Bad or missing key. | Do not retry. Fix the key. |

`error.retryable` tells you whether a retry can ever succeed. `error.retry_after_seconds`, when
present, tells you how long to wait.

**Backoff:** for `429` and `503`, wait `min(60, 2^attempt) + random(0, 1)` seconds, up to 5 attempts.
Never retry a `4xx` other than `429` without changing the request.

---

## 10. Complete worked example

Agent A starts, dies. Agent B finds the work and finishes it.

```bash
BASE={{PUBLIC_BASE_URL}}

# --- Agent A -----------------------------------------------------------------
WORKFLOW=$(curl -sX POST $BASE/api/v1/workflows \
  -H "Authorization: Bearer $AGENT_A_KEY" -H "Content-Type: application/json" \
  -H "Idempotency-Key: scholarships-2026-07-10" \
  -d '{"title":"Scholarship comparison",
       "objective":"Compare five scholarship programs and recommend one",
       "priority":"high","heartbeat_timeout_seconds":60,
       "initial_checkpoint":{
         "objective":"Compare five scholarship programs",
         "completed_steps":["Found five programs","Collected eligibility requirements"],
         "remaining_steps":["Compare deadlines","Produce recommendation"],
         "decisions":[{"decision":"Only programs open to international students",
                       "reason":"Required by the original request from the user"}],
         "next_action":"Compare application deadlines",
         "context_summary":"Five programs found and eligibility collected. Deadlines not compared."}}' \
  | jq -r .id)

# Agent A crashes here. It sends no more heartbeats.

# --- The service, 60 seconds later -------------------------------------------
# active -> suspected_failed -> recoverable   (automatic, no request needed)

# --- Agent B: discover --------------------------------------------------------
curl -s "$BASE/api/v1/recoverable-workflows?resumable_only=true" \
  -H "Authorization: Bearer $AGENT_B_KEY" | jq '.items[0].workflow.id, .items[0].context_score'
# "6f1d2a44-7f2b-4d0e-9a3c-1b5f8e2c9d10"
# 100

# --- Agent B: understand ------------------------------------------------------
curl -s $BASE/api/v1/workflows/$WORKFLOW/recovery-package \
  -H "Authorization: Bearer $AGENT_B_KEY" \
  | jq '{resumable: .context_evaluation.resumable,
         next: .resume_instructions.next_action,
         skip: .resume_instructions.must_not_repeat,
         parent: .resume_instructions.expected_parent_version}'
# { "resumable": true,
#   "next": "Compare application deadlines",
#   "skip": ["Found five programs","Collected eligibility requirements"],
#   "parent": 1 }

# --- Agent B: claim -----------------------------------------------------------
CLAIM=$(curl -sX POST $BASE/api/v1/workflows/$WORKFLOW/claims \
  -H "Authorization: Bearer $AGENT_B_KEY" -H "Content-Type: application/json" \
  -d '{"lease_seconds":300}')
LEASE=$(echo $CLAIM | jq -r .lease_token)   # store securely; shown only once

# --- Agent B: resume ----------------------------------------------------------
curl -sX POST $BASE/api/v1/workflows/$WORKFLOW/resume \
  -H "Authorization: Bearer $AGENT_B_KEY" -H "Content-Type: application/json" \
  -d "{\"lease_token\":\"$LEASE\"}" | jq -r .status
# "active"

# Agent B compares the deadlines and writes the recommendation.
# It renews the lease every 150 seconds:
#   curl -sX POST $BASE/api/v1/workflows/$WORKFLOW/claims/renew \
#     -H "Authorization: Bearer $AGENT_B_KEY" -H "Content-Type: application/json" \
#     -d "{\"lease_token\":\"$LEASE\",\"lease_seconds\":300}"

# --- Agent B: final checkpoint ------------------------------------------------
curl -sX POST $BASE/api/v1/workflows/$WORKFLOW/checkpoints \
  -H "Authorization: Bearer $AGENT_B_KEY" -H "Content-Type: application/json" \
  -H "Idempotency-Key: $WORKFLOW-final" \
  -d "{\"parent_version\":1,\"lease_token\":\"$LEASE\",
       \"objective\":\"Compare five scholarship programs\",
       \"completed_steps\":[\"Found five programs\",\"Collected eligibility requirements\",
                            \"Compared deadlines\",\"Produced recommendation\"],
       \"remaining_steps\":[],
       \"next_action\":\"Nothing remains; ready to complete\",
       \"context_summary\":\"All five compared. Program C recommended: earliest deadline, full funding.\"}" \
  | jq -r .version
# 2

# --- Agent B: complete --------------------------------------------------------
curl -sX POST $BASE/api/v1/workflows/$WORKFLOW/complete \
  -H "Authorization: Bearer $AGENT_B_KEY" -H "Content-Type: application/json" \
  -H "Idempotency-Key: complete-$WORKFLOW" \
  -d "{\"lease_token\":\"$LEASE\",\"final_checkpoint_version\":2,
       \"summary\":\"Recommended program C\"}" | jq -r .status
# "completed"

# --- Anyone: audit ------------------------------------------------------------
curl -s $BASE/api/v1/workflows/$WORKFLOW/events -H "Authorization: Bearer $AGENT_B_KEY" \
  | jq -r '.items[].event_type'
# workflow_completed / checkpoint_created / workflow_resumed / claim_acquired /
# workflow_made_recoverable / failure_suspected / checkpoint_created / workflow_created
```

More examples, including failure and race scenarios, are in
[`references/recovery-examples.md`](references/recovery-examples.md).

---

## 11. Checklist before you complete a workflow

Run through this list. Every line must be true.

- [ ] `remaining_steps` in the latest checkpoint is empty.
- [ ] Every step in `must_not_repeat` was **not** repeated.
- [ ] Every decision in `must_preserve` was honoured, or a new decision explains the change.
- [ ] Every artifact you relied on has `verification_status: "verified"`, or you verified its
      SHA-256 yourself.
- [ ] No artifact has `verification_status: "failed"`.
- [ ] Your final checkpoint records the outcome in `context_summary`.
- [ ] `final_checkpoint_version` equals the workflow's `current_checkpoint_version`.
- [ ] You still hold a valid lease (if a claim is active). Renew it if in doubt.
- [ ] Your `POST /complete` carries an `Idempotency-Key`.
- [ ] You received `200` with `"status": "completed"`.

If you cannot tick every line, do not complete. Write a checkpoint describing what is left, then
either continue or release the claim.

---

## 12. Reference documents

| Document | Contents |
| --- | --- |
| [`references/api-reference.md`](references/api-reference.md) | Every endpoint, every parameter, every field. |
| [`references/error-codes.md`](references/error-codes.md) | Every error code, its HTTP status, cause and remedy. |
| [`references/checkpoint-schema.md`](references/checkpoint-schema.md) | Checkpoint field semantics and the full context-scoring rule table. |
| [`references/recovery-examples.md`](references/recovery-examples.md) | Worked examples: normal run, crash recovery, claim race, stale write, artifact failure. |

Machine-readable schema: `{{PUBLIC_BASE_URL}}/openapi.json`
Human-readable API explorer: `{{PUBLIC_BASE_URL}}/docs`
