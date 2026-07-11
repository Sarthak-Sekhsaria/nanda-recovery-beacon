<div align="center">

# NANDA Recovery Beacon

**Recovery infrastructure for AI-agent workflows.**
When an agent crashes, times out, or vanishes mid-task, another agent discovers the unfinished
work, claims it under an exclusive lease, reads a complete recovery package, and finishes it —
without repeating a single completed step.

[Agent instructions (`SKILL.md`)](SKILL.md) ·
[API reference](references/api-reference.md) ·
[Error codes](references/error-codes.md) ·
[Checkpoint schema](references/checkpoint-schema.md) ·
[Recovery examples](references/recovery-examples.md)

</div>

---

## The problem

Agent tasks are long, multi-step, and fragile. A research agent comparing five scholarship programs
might spend twenty minutes gathering data — then its process is killed by a deploy, its network
partitions, or it simply hangs. The work is half done. Nobody knows how far it got. A second agent,
handed the same task, starts from zero and redoes everything, possibly with duplicated side effects.

As agent networks grow, this is the dominant failure mode. Individual agents are unreliable in ways
that are entirely normal for distributed systems: they crash, they time out, they lose the network.
The fix is the same one distributed systems have always used — **durable checkpoints, exclusive
leases, and fencing** — but packaged so that *any* agent can use it through a plain REST API,
guided by a single instruction document.

That is what NANDA Recovery Beacon is. It is **not** an agent. It runs no models, holds no API keys
for any LLM, and makes no autonomous decisions. It is the coordination substrate that unreliable
agents call to make their collective work reliable.

## What it guarantees

| Guarantee | How |
| --- | --- |
| **Exactly one agent** can hold a workflow at a time | Partial unique index + `SELECT … FOR UPDATE` |
| A **superseded agent cannot corrupt** work that moved on | Fencing tokens (monotonic lease generation) |
| **No lost updates** to progress | Optimistic concurrency on checkpoint versions |
| **Completion cannot be replayed** | Terminal-state guard + idempotency store |
| **Checkpoints are immutable** | `BEFORE UPDATE OR DELETE` trigger, not just a missing endpoint |
| The **audit trail is tamper-evident** | Same trigger on the events table |
| **Abandoned work is detected** even with many app instances | `pg_try_advisory_xact_lock` + `FOR UPDATE SKIP LOCKED` |
| **"Is this safe to resume?"** has a deterministic answer | A rule-based completeness evaluator — no LLM |

Every one of these is proven by a test in [`backend/tests/`](backend/tests), including a true
concurrency test where eight agents race for one workflow and exactly one wins.

---

## Try it now — live deployment & judge access

The service is deployed and running. No setup required to explore it.

| | |
| --- | --- |
| **Dashboard** | [nanda-recovery-beacon-dashboard-aqep.onrender.com](https://nanda-recovery-beacon-dashboard-aqep.onrender.com) |
| **API** | [nanda-recovery-beacon-api-aqep.onrender.com](https://nanda-recovery-beacon-api-aqep.onrender.com) |
| **Agent instructions** | [`/skill.md`](https://nanda-recovery-beacon-api-aqep.onrender.com/skill.md) |
| **API docs (Swagger UI)** | [`/docs`](https://nanda-recovery-beacon-api-aqep.onrender.com/docs) |

The deployed API requires an API key (`DEMO_MODE=false` — see [Design decisions](#design-decisions)).
A standard, non-admin key is provided below so judges can call the API directly without provisioning
one. It can create, checkpoint, claim, and complete workflows — the same permissions any real agent
has — but cannot mint keys or force an admin sweep.

```
Authorization: Bearer nrb_6S4kChasODLY-FbvyI-D7IiQPrKW4rDPRur5lKtNHu0
```

```bash
curl https://nanda-recovery-beacon-api-aqep.onrender.com/api/v1/workflows \
  -H "Authorization: Bearer nrb_6S4kChasODLY-FbvyI-D7IiQPrKW4rDPRur5lKtNHu0"
```

This is a scoped demo credential for the duration of judging, not a production secret. It is
revocable at any time with `python -m app.cli revoke-key --agent-id hackathon-judge` (see
[Local setup](#local-setup)). The free-tier instance sleeps after 15 minutes idle; the first request
after that takes ~30 seconds to wake it.

---

## Architecture

```mermaid
flowchart TB
    subgraph agents["AI agents (external)"]
        A1["Original agent<br/>creates &amp; checkpoints"]
        A2["Replacement agent<br/>discovers &amp; recovers"]
    end

    subgraph beacon["NANDA Recovery Beacon"]
        direction TB
        API["FastAPI service<br/>REST /api/v1 · /skill.md · /metrics"]
        SVC["Domain core<br/>state machine · claims · fencing<br/>checkpoints · context evaluator"]
        REAP["Failure detector<br/>heartbeat timeout · lease expiry<br/>advisory-lock guarded"]
        API --> SVC
        REAP --> SVC
    end

    DB[("PostgreSQL<br/>partial unique index<br/>append-only triggers<br/>advisory locks")]
    DASH["Next.js dashboard<br/>recovery control center"]

    A1 -- "create / heartbeat / checkpoint" --> API
    A2 -- "discover / claim / resume / complete" --> API
    SVC --> DB
    REAP --> DB
    DASH -- "server-side, read-only" --> API

    classDef beaconBox fill:#0d1117,stroke:#f97316,color:#f2f4f8
    classDef store fill:#0d1117,stroke:#38bdf8,color:#f2f4f8
    class API,SVC,REAP beaconBox
    class DB store
```

The **API is the product.** The dashboard is a read-only window onto it, useful for judging and
debugging; every number it shows comes from the live API. An agent never needs the dashboard — it
needs only [`/skill.md`](SKILL.md).

### The recovery lifecycle

```mermaid
sequenceDiagram
    participant A as Agent A
    participant B as Agent B (replacement)
    participant S as Recovery Beacon
    participant DB as PostgreSQL

    A->>S: POST /workflows (+ initial checkpoint)
    S->>DB: workflow=active, checkpoint v1
    A->>S: POST /checkpoints (v2)
    Note over A: Agent A crashes. No more heartbeats.
    S->>DB: heartbeat deadline passes → suspected_failed → recoverable
    B->>S: GET /recoverable-workflows?resumable_only=true
    B->>S: GET /recovery-package
    Note over B: reads context_evaluation, must_not_repeat
    B->>S: POST /claims  → lease_token + fencing_token=1
    S->>DB: one active claim (unique index)
    B->>S: POST /resume  → active, agent=B
    A-->>S: (zombie) POST /heartbeats → 403 NOT_LEASE_HOLDER
    B->>S: POST /checkpoints (v3, parent_version=2, lease_token)
    B->>S: POST /complete (Idempotency-Key)
    S->>DB: completed. Full audit trail preserved.
```

---

## Tech stack

| Layer | Choice | Why |
| --- | --- | --- |
| API | **FastAPI** + Pydantic v2 | Generates accurate OpenAPI; strict request validation. |
| Data | **PostgreSQL 16** via **SQLAlchemy 2.0** (sync) | Sync sessions make `SELECT … FOR UPDATE` and row locking explicit and correct. |
| Migrations | **Alembic** | Partial indexes and triggers live in versioned migrations. |
| Driver | **psycopg 3** | Modern, typed, fast. |
| Dashboard | **Next.js 15** (App Router) + **Tailwind** | Server components keep the API key off the browser. |
| Failure detection | in-process thread **or** standalone worker | Same code path; advisory-lock guarded either way. |
| Tests | **pytest** against real PostgreSQL | The guarantees don't exist in SQLite. |
| CI | **GitHub Actions** | Lint, type-check, migrations, tests, drift check, Docker build. |
| Deploy | **Render + Neon**, with Fly.io and Railway configs | Free, persistent, public HTTPS. |

No paid LLM API key is required anywhere. Context evaluation is a deterministic rule engine.

---

## Feature overview

- **20+ REST endpoints** under `/api/v1`, fully documented in OpenAPI, with real request/response
  examples on every route.
- **Immutable, versioned checkpoints** with optimistic concurrency.
- **Exclusive claim leases** with renewal, voluntary release, expiry, and **fencing tokens**.
- **Two failure-detection paths**: explicit `POST /fail` and heartbeat timeout, both distributed-safe.
- **Deterministic context evaluator** with a documented 16-rule scoring model.
- **One-call recovery package** giving a replacement agent everything it needs.
- **Artifact registration** with server-side SHA-256 verification and SSRF protection; a clean
  storage abstraction ready for any S3-compatible store.
- **API-key auth** storing only hashes, with constant-time comparison and a public demo mode.
- **Idempotency keys** making every mutation safe to retry, including completion.
- **Append-only audit trail** enforced by database triggers.
- **Structured JSON logs**, request IDs, and **Prometheus metrics**.
- **A recovery control-center dashboard**: overview, workflow explorer, deep workflow detail with
  checkpoint diffs, and a ranked recovery queue.

## Screenshots

The dashboard has five views. Run it locally (`make up`) or visit the deployment.

| View | What it shows |
| --- | --- |
| **Overview** (`/`) | Status tiles, average recovery time, context-score distribution, live event feed. |
| **Workflow explorer** (`/workflows`) | Searchable, filterable cards: status, priority, version, agent, heartbeat age, context score. |
| **Workflow detail** (`/workflows/{id}`) | Objective, progress timeline, checkpoint version history with diffs, decisions, artifacts with verification state, context report, audit trail, copyable curl. |
| **Recovery queue** (`/recoverable`) | Claimable work split into *safe to resume* and *has blocking issues*, ranked by priority and wait time. |
| **API & Skill** (`/skill`) | The live `SKILL.md`, the public base URL, and copyable curl commands. |

> Add PNGs to `docs/screenshots/` and link them here after your first deploy.

---

## Local setup

**Prerequisites:** Python 3.11+, Node 20+, and PostgreSQL — either a local install, Docker, or a
free [Neon](https://neon.tech) database (no credit card).

### Option A — Docker (everything, one command)

```bash
cp .env.example .env
docker compose up --build
# API        http://localhost:8000        (Swagger UI at /docs)
# Dashboard  http://localhost:3000
# The API starts in DEMO_MODE so you can curl it without a key.
```

### Option B — run the pieces directly

```bash
# 1. Backend dependencies
make install                     # creates backend/.venv, installs everything

# 2. Point at a database (local or Neon)
export DATABASE_URL='postgresql+psycopg://beacon:beacon@localhost:5432/beacon'
export TEST_DATABASE_URL="$DATABASE_URL"

# 3. Migrate and seed
make migrate
make seed                        # inserts realistic sample workflows + prints API keys

# 4. Run
make dev                         # API with autoreload on :8000
make worker                      # (optional) standalone failure detector
make dashboard                   # dashboard on :3000, in another terminal
```

Mint an API key for your own agent:

```bash
make create-key agent=my-agent
# or an admin key:
make create-admin-key agent=admin
```

## Environment variables

Full list with defaults in [`.env.example`](.env.example). The ones that matter:

| Variable | Default | Notes |
| --- | --- | --- |
| `DATABASE_URL` | local dsn | **Required in production.** `postgres://` URLs are normalised automatically. |
| `PUBLIC_BASE_URL` | `http://localhost:8000` | Substituted into `/skill.md`. Set to your deployed URL. |
| `DEMO_MODE` | `false` | `true` accepts unauthenticated calls (attributed to `X-Agent-Id`). Demo only. |
| `RUN_REAPER_IN_API` | `true` | `true` runs failure detection in a daemon thread; `false` if you deploy a separate worker. |
| `DEFAULT_HEARTBEAT_TIMEOUT_SECONDS` | `120` | Silence beyond this suspects failure. |
| `RESUMABLE_MIN_SCORE` | `50` | Context-score threshold for `resumable`. |
| `STORAGE_BACKEND` | `none` | `s3` enables pre-signed artifact uploads (needs `S3_*`). |
| `CORS_ALLOW_ORIGINS` / `TRUSTED_HOSTS` | `*` | Restrict in production. |
| `RATE_LIMIT_REQUESTS` | `120` | Per instance, per 60s window. |

Dashboard (in `frontend/.env.local`): `BEACON_API_URL` (server-side), optional `BEACON_API_KEY`,
and `NEXT_PUBLIC_BEACON_PUBLIC_URL` (shown to visitors).

## Database migrations

```bash
make migrate                     # alembic upgrade head
make migration m="add a column"  # autogenerate a new revision
make downgrade                   # roll back one
```

Migration `0001` creates the partial unique index and the append-only triggers. CI fails if the
models ever drift from the migrations.

## Testing

The suite runs against **real PostgreSQL** — the concurrency guarantees cannot be tested on SQLite.

```bash
export TEST_DATABASE_URL='postgresql+psycopg://user:pass@host/db?sslmode=require'

make test                # full suite
make test-concurrency    # only the race / fencing tests
make coverage            # with a coverage report
```

Tests create and drop an isolated schema (`beacon_test`), so pointing `TEST_DATABASE_URL` at the
same database that backs a deployment is safe.

What is covered: workflow creation, checkpoint immutability and version monotonicity, idempotent
duplicate requests, heartbeat timeout, explicit failure, **two agents racing to claim one workflow
(true concurrency)**, claim renewal, expired-claim takeover, **stale fencing-token rejection**,
old-checkpoint-version rejection, the missing-context evaluator, artifact checksum validation and
SSRF guards, completion rules, invalid state transitions, authentication, rate limiting, the error
envelope, and a **full end-to-end recovery lifecycle** with audit-trail verification.

## API usage

The complete, agent-facing guide is [`SKILL.md`](SKILL.md), served live at
[`/skill.md`](SKILL.md). The shortest possible tour:

```bash
BASE=http://localhost:8000
KEY=nrb_your_key   # or omit and rely on DEMO_MODE

# Create work
WF=$(curl -sX POST $BASE/api/v1/workflows -H "Authorization: Bearer $KEY" \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: demo-1' \
  -d '{"title":"Demo","objective":"Show recovery","heartbeat_timeout_seconds":60,
       "initial_checkpoint":{"objective":"Show recovery","completed_steps":["step 1"],
         "remaining_steps":["step 2"],"next_action":"do step 2",
         "context_summary":"One step done."}}' | jq -r .id)

# It fails, becomes recoverable, another agent takes over
curl -sX POST $BASE/api/v1/workflows/$WF/fail -H "Authorization: Bearer $KEY" \
  -H 'Content-Type: application/json' -d '{"reason":"demo"}'
curl -s "$BASE/api/v1/recoverable-workflows?resumable_only=true" -H "Authorization: Bearer $KEY"
```

A full failure-and-recovery walkthrough is in [`references/recovery-examples.md`](references/recovery-examples.md).

---

## Deployment (public HTTPS)

The service is built for a persistent public URL. It does **not** rely on localhost, ngrok, or a
tunnel. Blueprints for three platforms are included; Render + Neon is the documented path.

### Render + Neon (recommended, free)

1. **Database.** Create a free project at [neon.tech](https://neon.tech). Copy the connection string
   (it looks like `postgresql://user:pass@ep-xxx.aws.neon.tech/neondb?sslmode=require`).
2. **Push** this repository to GitHub.
3. **Blueprint.** In Render: *New → Blueprint →* select the repo. Render reads [`render.yaml`](render.yaml)
   and provisions the API and the dashboard.
4. **Set secrets** when prompted:
   - API service: `DATABASE_URL` = your Neon string.
5. **First deploy** runs `alembic upgrade head` automatically, then starts uvicorn.
6. **Wire the URLs.** Once the API has a URL (e.g. `https://nanda-recovery-beacon-api.onrender.com`),
   set on the API service `PUBLIC_BASE_URL` to it, and on the dashboard `BEACON_API_URL` +
   `NEXT_PUBLIC_BEACON_PUBLIC_URL` to it. Redeploy both.
7. **Verify** (see below).

Free instances sleep after 15 minutes idle; the first request wakes them in ~30s. Failure detection
survives sleep: a sweep runs on wake, and every read of `/recoverable-workflows` triggers an
advisory-lock-guarded sweep.

### Fly.io / Railway

`fly.toml` and `railway.json` are included with inline instructions. Both build from
`backend/Dockerfile`, attach a managed Postgres, and expose HTTPS.

### Verify a live deployment

```bash
./scripts/verify_deployment.sh https://your-api.onrender.com
```

It drives the entire lifecycle — create, checkpoint, fail, discover, claim, resume, checkpoint,
complete — against the live service, verifies the audit trail, checks the security headers, and
exits non-zero on any failure.

### Production readiness checklist

- [ ] `DATABASE_URL` points at a persistent, backed-up PostgreSQL.
- [ ] `PUBLIC_BASE_URL` is the real HTTPS URL (check `/skill.md` shows it).
- [ ] `DEMO_MODE=false`, and every agent has its own API key.
- [ ] `CORS_ALLOW_ORIGINS` and `TRUSTED_HOSTS` are restricted to your domains.
- [ ] Failure detection runs (`RUN_REAPER_IN_API=true`, or a `worker` process).
- [ ] `GET /ready` returns 200 (database reachable, migrations applied).
- [ ] `/metrics` is scraped, or at least reachable, by your monitoring.
- [ ] `verify_deployment.sh` passes against the live URL.

---

## Observability

- **Structured JSON logs** on stdout, one line per request, with `request_id` and `agent_id`.
  Secrets and lease tokens are redacted by a logging filter before they can reach a sink.
- **`GET /health`** — liveness. **`GET /ready`** — readiness (DB + migrations).
- **`GET /metrics`** — Prometheus: request counts and latency, plus domain counters for claims,
  conflicts, checkpoint creation, recoveries, failures detected, and the context-score histogram.

```bash
curl -s https://your-api.onrender.com/metrics | grep nrb_
```

---

## Design decisions

- **Synchronous SQLAlchemy, not async.** The correctness of claiming rests on `SELECT … FOR UPDATE`
  and row locks. Sync sessions make the locking explicit and the reasoning simple. The workload is
  IO-light and short; async would add complexity without a matching benefit.
- **Two enforcement layers for every guarantee.** Application logic *and* a database constraint. The
  partial unique index means that even a bug in the claim path cannot produce two active claims.
- **Fencing tokens over lock timeouts alone.** A lease expiring is not enough; a zombie agent could
  still submit stale work. The monotonic `lease_generation` makes such writes provably rejectable.
- **Append-only by trigger, not by convention.** A missing UPDATE endpoint is a promise. A trigger
  is a guarantee — it holds against migrations, scripts, and direct SQL.
- **Deterministic context scoring.** Using an LLM to judge resumability would make the answer
  non-reproducible and add a paid dependency. A transparent rule table is auditable and free.
- **The dashboard reads the API from the server.** The API key never reaches the browser; the
  dashboard is a client of the same public API an agent uses.

## Known limitations

- **Rate limiting is per instance.** With N instances the effective limit is N×. It protects an
  instance from abuse; it is not a global quota. All *correctness* coordination is in PostgreSQL.
- **Free-tier cold starts.** On Render's free plan the first request after 15 minutes idle takes
  ~30s. Failure detection catches up on wake.
- **Direct artifact upload is off by default.** `STORAGE_BACKEND=none` supports externally hosted
  artifacts by URL + checksum. The S3 path is implemented behind a clean abstraction but needs
  `STORAGE_BACKEND=s3` and credentials to enable.
- **Authentication is intentionally simple.** Hashed API keys, scoped to an agent id. It is modular
  so a richer identity system can replace it — see below.

## Future integrations

These are **not implemented**. They are where the service is designed to connect into the wider
[NANDA](https://nanda.media.mit.edu/) ecosystem, and the code is structured to accept them:

- **NANDA discovery / AgentFacts.** The API-key layer is deliberately isolated (`app/security.py`,
  `app/api/deps.py`). It could be replaced by verifying signed AgentFacts so agents authenticate
  with portable, verifiable identity instead of a shared secret.
- **MCP.** A thin Model Context Protocol server could expose these endpoints as MCP tools, letting
  MCP-native agents recover workflows without bespoke HTTP code.
- **A2A.** Recovery hand-offs are a natural fit for agent-to-agent negotiation protocols; the
  claim/lease model already encodes the ownership transfer such a protocol needs.

Each is labelled future work deliberately — the shipped service does not claim them.

---

## Repository layout

```
.
├── SKILL.md                     # Agent-facing instructions, served at /skill.md
├── references/                  # api-reference, error-codes, checkpoint-schema, recovery-examples
├── backend/
│   ├── app/
│   │   ├── main.py              # app factory, middleware, exception handlers, lifespan
│   │   ├── config.py            # env-driven settings
│   │   ├── models.py            # SQLAlchemy models + DB invariants
│   │   ├── state_machine.py     # validated workflow transitions
│   │   ├── context_eval.py      # deterministic completeness evaluator
│   │   ├── reaper.py            # distributed-safe failure detection
│   │   ├── security.py          # API keys & lease tokens (hashes only)
│   │   ├── artifact_verify.py   # SHA-256 verification + SSRF guard
│   │   ├── api/                 # routers: workflows, checkpoints, claims, artifacts, events, system
│   │   └── services/            # domain logic: workflows, claims, checkpoints, recovery, events
│   ├── alembic/                 # migrations (partial index + append-only triggers)
│   ├── tests/                   # pytest incl. true concurrency + E2E lifecycle
│   ├── seed.py                  # realistic sample data
│   └── Dockerfile
├── frontend/                    # Next.js recovery control center
├── docker-compose.yml           # PG + API + worker + dashboard
├── render.yaml · fly.toml · railway.json
├── scripts/verify_deployment.sh # live smoke test of the whole lifecycle
├── .github/workflows/ci.yml
└── Makefile
```

## License

MIT.
