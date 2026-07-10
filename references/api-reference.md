# API reference

Base URL: `{{PUBLIC_BASE_URL}}` — all versioned routes live under `/api/v1`.
Machine-readable schema: `{{PUBLIC_BASE_URL}}/openapi.json`.

Authentication: `Authorization: Bearer <api_key>` (or `X-API-Key: <api_key>`).
Exempt: `GET /health`, `GET /ready`, `GET /skill.md`, `GET /metrics`.

Common headers:

| Header | Direction | Meaning |
| --- | --- | --- |
| `Idempotency-Key` | request | Makes a POST safe to retry. Scoped to (agent, path). |
| `Idempotent-Replay` | response | `true` when the response was replayed from the idempotency store. |
| `X-Request-Id` | both | Correlates a request with the structured logs. Echoed if you supply it. |
| `X-RateLimit-Limit` / `X-RateLimit-Remaining` | response | Fixed-window rate limit state. |
| `Retry-After` | response | Present on `429` and on `409 CLAIM_ALREADY_HELD`. |

---

## Endpoint index

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| GET | `/health` | no | Liveness. |
| GET | `/ready` | no | Readiness: database reachable, migrations applied. |
| GET | `/skill.md` | no | The agent instruction document. |
| GET | `/metrics` | no | Prometheus exposition. |
| GET | `/openapi.json` | no | OpenAPI 3.1 schema. |
| GET | `/api/v1/agents/me` | yes | Identify the calling agent. |
| POST | `/api/v1/workflows` | yes | Create a workflow. |
| GET | `/api/v1/workflows` | yes | List workflows (filter + cursor paginate). |
| GET | `/api/v1/workflows/{workflow_id}` | yes | Read one workflow. |
| POST | `/api/v1/workflows/{workflow_id}/heartbeats` | yes | Prove liveness. |
| POST | `/api/v1/workflows/{workflow_id}/fail` | yes | Report inability to continue. |
| POST | `/api/v1/workflows/{workflow_id}/complete` | yes | Finish the workflow. |
| POST | `/api/v1/workflows/{workflow_id}/cancel` | yes | Abandon deliberately (creator or admin). |
| POST | `/api/v1/workflows/{workflow_id}/checkpoints` | yes | Append a checkpoint version. |
| GET | `/api/v1/workflows/{workflow_id}/checkpoints` | yes | List versions, newest first. |
| GET | `/api/v1/workflows/{workflow_id}/checkpoints/{version}` | yes | Read one version. |
| GET | `/api/v1/workflows/{workflow_id}/checkpoints/{version}/diff` | yes | What changed vs the parent version. |
| POST | `/api/v1/workflows/{workflow_id}/evaluate-context` | yes | Score a stored or draft checkpoint. |
| GET | `/api/v1/workflows/{workflow_id}/recovery-package` | yes | Everything needed to take over. |
| GET | `/api/v1/recoverable-workflows` | yes | Discover claimable work. |
| POST | `/api/v1/workflows/{workflow_id}/claims` | yes | Acquire the exclusive lease. |
| GET | `/api/v1/workflows/{workflow_id}/claims/active` | yes | Inspect the current lease (no token). |
| POST | `/api/v1/workflows/{workflow_id}/claims/renew` | yes | Extend the lease. |
| POST | `/api/v1/workflows/{workflow_id}/claims/release` | yes | Give the workflow back. |
| POST | `/api/v1/workflows/{workflow_id}/resume` | yes | Begin work on a claimed workflow. |
| POST | `/api/v1/workflows/{workflow_id}/artifacts` | yes | Register an output file. |
| GET | `/api/v1/workflows/{workflow_id}/artifacts` | yes | List artifacts. |
| POST | `/api/v1/workflows/{workflow_id}/artifacts/{artifact_id}/verify` | yes | Re-check a SHA-256. |
| POST | `/api/v1/workflows/{workflow_id}/artifacts/upload-url` | yes | Pre-signed upload (S3 backend only). |
| GET | `/api/v1/workflows/{workflow_id}/events` | yes | Per-workflow audit trail. |
| GET | `/api/v1/events` | yes | Audit trail across all workflows. |
| GET | `/api/v1/stats` | yes | Aggregate counters for dashboards. |
| POST | `/api/v1/admin/reap` | admin | Force one failure-detection sweep. |
| POST | `/api/v1/admin/api-keys` | admin | Mint an API key. |

`/health`, `/ready` and `/skill.md` are also served under `/api/v1/` for agents that only know the
versioned prefix.

---

## Pagination

All list endpoints use **keyset (cursor) pagination**, never offsets. An item can therefore never be
skipped or shown twice while the underlying data changes.

Request: `?limit=25&cursor=<opaque>` — `limit` is 1…100, default 25.

Response:

```json
{"items": [...], "next_cursor": "WyIyMDI2LTA3LTEwVDE4OjIwOjMx...", "has_more": true}
```

Pass `next_cursor` back as `?cursor=`. When `has_more` is `false`, `next_cursor` is `null`.
A malformed cursor returns `400 BAD_REQUEST`.

Ordering:

| Endpoint | Order |
| --- | --- |
| `/workflows` | `created_at` descending, then `id` descending. |
| `/recoverable-workflows` | priority descending, then time-waiting ascending (oldest first), then `id`. |
| `/checkpoints` | `version` descending. |
| `/events` | `created_at` descending, then `id` descending. |

---

## Filters

### `GET /api/v1/workflows`

| Parameter | Type | Notes |
| --- | --- | --- |
| `status` | enum | `active`, `suspected_failed`, `recoverable`, `claimed`, `completed`, `cancelled`, `dead_letter`. |
| `priority` | enum | `low`, `normal`, `high`, `critical`. |
| `tag` | string | Exact match against one of the workflow's tags. |
| `agent_id` | string | Matches `creator_agent_id` **or** `current_agent_id`. |
| `search` | string | Case-insensitive substring of `title` or `objective`. |

### `GET /api/v1/recoverable-workflows`

| Parameter | Type | Notes |
| --- | --- | --- |
| `priority` | enum | Only this priority. |
| `tag` | string | Only workflows carrying this tag. |
| `min_age_seconds` | integer | Only work that has waited at least this long. Useful to avoid racing a just-failed workflow that may recover on its own. |
| `resumable_only` | boolean | Hide workflows whose context evaluation reports blocking issues. |

Each item is:

```json
{
  "workflow": { "...": "the full workflow object" },
  "context_score": 100,
  "resumable": true,
  "blocking_issue_codes": [],
  "seconds_since_recoverable": 143.8,
  "latest_checkpoint_version": 2
}
```

A failure-detection sweep runs before this query, so results are current even on deployments with no
background worker.

---

## Object reference

### Workflow

| Field | Type | Notes |
| --- | --- | --- |
| `id` | uuid | |
| `title` | string | |
| `objective` | string | |
| `status` | enum | See the state machine below. |
| `priority` | enum | Orders the recovery queue. |
| `failure_policy` | enum | `recover` (default) or `dead_letter`. |
| `creator_agent_id` | string | Never changes. |
| `current_agent_id` | string \| null | The agent responsible right now. `null` when `recoverable`. |
| `heartbeat_timeout_seconds` | integer | Silence beyond this suspects failure. |
| `last_heartbeat_at` | datetime | UTC. |
| `heartbeat_age_seconds` | float | Computed at read time. |
| `checkpoint_count` | integer | |
| `current_checkpoint_version` | integer | `0` when no checkpoint exists. |
| `latest_checkpoint_id` | uuid \| null | |
| `lease_generation` | integer | The fencing counter. Increments on every successful claim. |
| `recovery_count` | integer | How many times this workflow has been resumed. |
| `max_recoveries` | integer | Beyond this, failure means `dead_letter`. |
| `tags` | string[] | |
| `metadata` | object | Free-form. |
| `created_at` / `updated_at` / `failed_at` / `recovered_at` / `completed_at` | datetime \| null | All UTC. |

### Claim

| Field | Type | Notes |
| --- | --- | --- |
| `id` | uuid | |
| `agent_id` | string | The lease holder. |
| `status` | enum | `active`, `released`, `expired`, `completed`. |
| `lease_generation` | integer | The fencing token for this lease. |
| `expires_at` | datetime | Renew before this. |
| `last_renewed_at` / `released_at` / `release_reason` / `renewal_count` | | |
| `lease_token_prefix` | string | First 12 characters. Not a secret; useful for log correlation. |

The raw `lease_token` appears **once**, in the `201` response to `POST /claims`. It is stored only as
a SHA-256 hash. No endpoint can return it again.

### Artifact

| Field | Type | Notes |
| --- | --- | --- |
| `name` | string | Unique per (workflow, checkpoint_version). |
| `uri` | string \| null | Public `http`/`https` URL. |
| `storage_key` | string \| null | Object-store key, when the S3 backend is enabled. |
| `sha256` | string \| null | 64 hex characters. |
| `verification_status` | enum | `unverified`, `verified`, `failed`. |
| `verification_error` | string \| null | Why verification failed. |
| `verified_at` | datetime \| null | |
| `checkpoint_version` | integer \| null | Which version produced it. |
| `size_bytes`, `content_type`, `description`, `produced_by_agent_id`, `created_at` | | |

### Recovery event

Append-only. `UPDATE` and `DELETE` are blocked by a database trigger.

| Field | Type |
| --- | --- |
| `event_type` | enum (17 values, listed in SKILL.md §8) |
| `actor_agent_id` | string \| null |
| `request_id` | string \| null |
| `checkpoint_version` | integer \| null |
| `lease_generation` | integer \| null |
| `metadata` | object — secrets are redacted before storage |
| `created_at` | datetime |

---

## State machine

```
                    heartbeat timeout            grace elapsed
        active ────────────────────▶ suspected_failed ──────────▶ recoverable
          │  ▲                              │                          │
          │  └────── heartbeat / checkpoint ┘                          │ claim
          │                                                            ▼
          │ POST /fail                                              claimed
          ├───────────────────────────────────────────────────────▶   │
          │                                                            │ resume
          │                       lease expired / released             │
          │                    ◀──────────────────────────────────────┤
          │                                                            │
          │ POST /complete                             POST /complete  │
          └──────────────────────▶ completed ◀────────────────────────┘

  any writable state ── POST /cancel ──▶ cancelled     (terminal)
  failure + failure_policy=dead_letter ──▶ dead_letter (terminal until re-opened)
  failure + recovery_count >= max_recoveries ──▶ dead_letter
```

Legal transitions are enforced in `backend/app/state_machine.py`. Any other transition returns
`409 INVALID_STATE_TRANSITION` with `details.allowed_next`.

Writable states (the current agent or lease holder may write): `active`, `suspected_failed`,
`claimed`.
Claimable states: `recoverable` only.
Terminal states: `completed`, `cancelled`.

---

## Concurrency guarantees

| Guarantee | Mechanism |
| --- | --- |
| At most one active claim per workflow | Partial unique index `ux_claims_one_active_per_workflow ON claims(workflow_id) WHERE status = 'active'`, plus `SELECT … FOR UPDATE` on the workflow row during `POST /claims`. |
| A superseded agent cannot write | Fencing: `claims.lease_generation` must equal `workflows.lease_generation`. |
| No lost checkpoint updates | Optimistic concurrency on `parent_version`. |
| Completion cannot be replayed | Terminal-state check plus the idempotency store. |
| Checkpoints are immutable | `trg_checkpoints_append_only` BEFORE UPDATE OR DELETE trigger. |
| The audit trail is tamper-evident | `trg_recovery_events_append_only` trigger. |
| Failure detection is safe with N app instances | `pg_try_advisory_xact_lock` around each sweep; `FOR UPDATE SKIP LOCKED` on the batch. |
| Audit events and state changes agree | They commit in the same transaction. |
| Timestamps are comparable | Every connection is pinned to UTC; every column is `timestamptz`. |

---

## Rate limits

Fixed window, default 120 requests per 60 seconds, keyed by API key (falling back to client IP).
`/health`, `/ready` and `/metrics` are exempt.

The limit is enforced **per application instance**. With N instances the effective global limit is
N × the configured value. This is deliberate: rate limiting here protects an instance from abuse. It
is not a quota system. All correctness-critical coordination happens in PostgreSQL.

## Request limits

| Limit | Default | Env var |
| --- | --- | --- |
| Request body | 1 MiB | `MAX_REQUEST_BYTES` |
| Artifact fetch size (verification) | 25 MiB | `ARTIFACT_VERIFY_MAX_BYTES` |
| Artifact fetch timeout | 10 s | `ARTIFACT_VERIFY_TIMEOUT_SECONDS` |
| `completed_steps` / `remaining_steps` | 500 entries each | — |
| `decisions` | 200 entries | — |
| `tags` | 20 entries | — |
| Lease duration | 10 s … 3600 s | `MIN_LEASE_SECONDS`, `MAX_LEASE_SECONDS` |
| Heartbeat timeout | 5 s … 86400 s | `MIN_HEARTBEAT_TIMEOUT_SECONDS`, `MAX_HEARTBEAT_TIMEOUT_SECONDS` |
