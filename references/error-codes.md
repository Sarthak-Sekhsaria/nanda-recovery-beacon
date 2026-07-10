# Error codes

Every non-2xx response has this body:

```json
{
  "error": {
    "code": "STALE_CHECKPOINT_VERSION",
    "message": "parent_version does not match the workflow's current checkpoint version.",
    "retryable": false,
    "retry_after_seconds": null,
    "details": {
      "your_parent_version": 1,
      "current_checkpoint_version": 3,
      "hint": "GET the workflow, read current_checkpoint_version, retry."
    }
  },
  "request_id": "9f2b1c0e5a7d4f3b"
}
```

**Branch on `error.code`.** Messages are for humans and may change. Codes are stable.
`request_id` also arrives in the `X-Request-Id` response header; quote it when reporting a problem.

`error.retryable` says whether an identical retry could ever succeed.
`error.retry_after_seconds`, when present, says how long to wait first.

---

## 400 — malformed request

| Code | Cause | What to do |
| --- | --- | --- |
| `BAD_REQUEST` | The request could not be parsed. Usually a malformed pagination `cursor`. | Drop the cursor and start from the first page. Do not retry unchanged. |

## 401 — authentication

| Code | Cause | What to do |
| --- | --- | --- |
| `UNAUTHENTICATED` | Missing, malformed, revoked or unknown API key. | Fix the `Authorization: Bearer <key>` header. **Never retry unchanged.** |

## 403 — permission

| Code | Cause | What to do |
| --- | --- | --- |
| `FORBIDDEN` | You are not the workflow's `current_agent_id`, and no claim exists for you to hold. | If the workflow is `recoverable`, claim it. Otherwise leave it alone. |
| `NOT_LEASE_HOLDER` | An active claim exists and you did not present a valid lease token for it — or the token belongs to another agent. | Claim the workflow, then retry with the returned `lease_token`. |
| `ADMIN_REQUIRED` | The endpoint needs an admin key. | Not available to normal agents. |

## 404 — not found

| Code | Cause | What to do |
| --- | --- | --- |
| `NOT_FOUND` | No route matches the path. | Check the path against `/openapi.json`. |
| `WORKFLOW_NOT_FOUND` | No workflow with that id. | Re-list. It may have been created by a different deployment. |
| `CHECKPOINT_NOT_FOUND` | No checkpoint with that version. | `GET /workflows/{id}` and read `current_checkpoint_version`. |
| `CLAIM_NOT_FOUND` | The workflow has no active claim. | Expected when the workflow is not claimed. |
| `ARTIFACT_NOT_FOUND` | No artifact with that id on that workflow. | Re-list the artifacts. |

## 409 — conflict

The most important family. All of these mean *the world changed under you*.

| Code | Cause | What to do |
| --- | --- | --- |
| `CLAIM_ALREADY_HELD` | Another agent won the race for the lease. `details.held_by_agent_id` names them; `retry_after_seconds` is when their lease lapses. | **Do not wait for it.** Pick a different workflow from the queue. Retrying is only sensible if that specific workflow is the only work available. |
| `WORKFLOW_NOT_RECOVERABLE` | You tried to claim a workflow whose status is not `recoverable`, or resume one that is not `claimed`. | Re-read the workflow. Re-list the queue. |
| `INVALID_STATE_TRANSITION` | The action is not legal from the workflow's current status. `details.allowed_next` lists what is. | Re-read the workflow status and choose a legal action. |
| `STALE_CHECKPOINT_VERSION` | Your `parent_version` (or `final_checkpoint_version`) is not the current version. Someone wrote a newer checkpoint. | Read `details.current_checkpoint_version`, fetch that checkpoint, merge your work into it, retry with the new `parent_version`. The rejection is recorded in the audit log as `stale_update_rejected`. |
| `FENCING_TOKEN_STALE` | Your lease expired **and** another agent has since claimed the workflow. Your `lease_generation` is behind the workflow's. | **Stop working immediately.** Discard any local state. Another agent owns this work. Do not retry, do not re-claim unless it returns to the queue. |
| `IDEMPOTENCY_KEY_REUSED` | Same `Idempotency-Key`, different request body. | Use a fresh key, or re-send the exact original body. |
| `WORKFLOW_ALREADY_COMPLETED` | Completion is not repeatable. | If you meant to complete it, treat this as success. To make retries clean, always send an `Idempotency-Key` on `POST /complete`. |

## 410 — gone

| Code | Cause | What to do |
| --- | --- | --- |
| `LEASE_EXPIRED` | Your lease lapsed. Nobody has taken over yet. | Claim the workflow again. You will get a new token and a new fencing token. Then continue. An expired lease can never be renewed. |

## 413 — payload

| Code | Cause | What to do |
| --- | --- | --- |
| `REQUEST_TOO_LARGE` | The body exceeds `MAX_REQUEST_BYTES` (1 MiB by default). | Move large content into an artifact and reference it by URL. |

## 422 — valid JSON, invalid domain

| Code | Cause | What to do |
| --- | --- | --- |
| `SCHEMA_VALIDATION_FAILED` | The body failed schema validation. `details.violations` lists each field, message and type. | Fix the body. Never retry unchanged. |
| `DOMAIN_VALIDATION_FAILED` | A domain rule was violated. On `POST /claims`, `details.code == "BLOCKING_CONTEXT_ISSUES"` means the checkpoint is not safe to resume from. | Read `details.blocking_issues`. Either pick another workflow, or re-send with `"acknowledge_blocking_issues": true`. |
| `COMPLETION_REQUIREMENTS_NOT_MET` | `details.unmet_requirements` lists exactly which requirements failed. | Usually `NO_REMAINING_STEPS`: write a final checkpoint with an empty `remaining_steps`, then complete. |
| `UNSUPPORTED_SCHEMA_VERSION` | The checkpoint's `schema_version` is unknown to this deployment. `details.supported` lists what is accepted. | Re-send with a supported version. |
| `ARTIFACT_VERIFICATION_FAILED` | The artifact could not be fetched, or its SHA-256 did not match. The artifact is stored with `verification_status: "failed"` and will block completion. | Re-upload the file, or register it with the correct checksum. Never resume from a failed artifact. |
| `UNSAFE_ARTIFACT_URL` | The URL is not `http`/`https`, or it resolves to a private, loopback or link-local address. | Host the artifact somewhere publicly reachable. |

## 429 — rate limit

| Code | Cause | What to do |
| --- | --- | --- |
| `RATE_LIMITED` | Too many requests in the window. `Retry-After` and `error.retry_after_seconds` say how long to wait. | Sleep that long, then retry. Reduce heartbeat frequency if you hit this repeatedly. |

## 501 / 503

| Code | HTTP | Cause | What to do |
| --- | --- | --- | --- |
| `STORAGE_BACKEND_DISABLED` | 501 | Direct artifact upload is not enabled on this deployment. | Host the artifact yourself and register it by `uri` + `sha256`. |
| `SERVICE_UNAVAILABLE` | 503 | The database is unreachable, or migrations are not applied. | Retry with exponential backoff and jitter. If `/ready` also fails, the deployment is unhealthy. |
| `INTERNAL_ERROR` | 500 | An unexpected error. Nothing was necessarily persisted. | Retry once with the same `Idempotency-Key`. Report the `request_id`. |

---

## Retry policy

```
retryable = error.retryable

if not retryable:
    fix the request or abandon the workflow. Never loop.

if error.retry_after_seconds is not None:
    sleep(error.retry_after_seconds)
else:
    sleep(min(60, 2 ** attempt) + random.uniform(0, 1))

max 5 attempts
```

Special cases that override the table:

- **`FENCING_TOKEN_STALE` is never retryable**, even though the HTTP status is 409. Another agent
  owns the work. Stop.
- **`CLAIM_ALREADY_HELD` is marked retryable**, but the right response is almost always to claim a
  different workflow rather than to wait.
- **`LEASE_EXPIRED` is retryable only after re-claiming.** Retrying the same write with the same
  token will fail forever.
- **Any POST that times out at the network level** should be retried with the same
  `Idempotency-Key`. That is the one case where you cannot see the status code and must assume the
  work may have landed.
