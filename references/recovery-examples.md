# Recovery examples

Five worked scenarios. Every request and response below matches the behaviour asserted by the test
suite in `backend/tests/`.

Set up a shell first:

```bash
BASE={{PUBLIC_BASE_URL}}
A="Authorization: Bearer $AGENT_A_KEY"
B="Authorization: Bearer $AGENT_B_KEY"
JSON="Content-Type: application/json"
```

---

## 1. The happy path: no recovery needed

```bash
# Create with an initial checkpoint.
WF=$(curl -sX POST $BASE/api/v1/workflows -H "$A" -H "$JSON" \
  -H "Idempotency-Key: links-2026-07-10" \
  -d '{"title":"Verify citation links",
       "objective":"Verify that every citation link in the report resolves",
       "heartbeat_timeout_seconds":120,
       "initial_checkpoint":{
         "objective":"Verify that every citation link in the report resolves",
         "completed_steps":["Extracted 63 citation links"],
         "remaining_steps":["Check each link","Report broken links"],
         "next_action":"Check link 1 of 63",
         "context_summary":"63 links extracted from the report. None checked yet."}}' | jq -r .id)

# Work, heartbeating every 60s (half of heartbeat_timeout_seconds).
curl -sX POST $BASE/api/v1/workflows/$WF/heartbeats -H "$A" -H "$JSON" -d '{}' | jq -r .status
# "active"

# Finish. remaining_steps must be empty before completing.
curl -sX POST $BASE/api/v1/workflows/$WF/checkpoints -H "$A" -H "$JSON" \
  -H "Idempotency-Key: $WF-v2" \
  -d '{"parent_version":1,
       "objective":"Verify that every citation link in the report resolves",
       "completed_steps":["Extracted 63 citation links","Checked each link","Reported 4 broken links"],
       "remaining_steps":[],
       "decisions":[{"decision":"Treat 301 redirects as valid",
                     "reason":"A permanent redirect still resolves for a human reader"}],
       "next_action":"Nothing remains; ready to complete",
       "context_summary":"All 63 links checked. Four are broken and were reported."}' | jq -r .version
# 2

curl -sX POST $BASE/api/v1/workflows/$WF/complete -H "$A" -H "$JSON" \
  -H "Idempotency-Key: complete-$WF" \
  -d '{"final_checkpoint_version":2,"summary":"4 broken links reported"}' | jq -r .status
# "completed"
```

---

## 2. Crash recovery: heartbeat timeout

Agent A stops sending heartbeats. Nobody tells the service anything.

```
t=0s    POST /workflows                      status: active
t=60s   (agent A's process is killed)
t=120s  heartbeat deadline passes            status: suspected_failed   (automatic)
t=150s  grace period passes                  status: recoverable        (automatic)
```

Agent A never called `/fail`. The service inferred the failure. Both transitions are recorded in the
audit trail as `failure_suspected` and `workflow_made_recoverable`.

Agent B, polling for work:

```bash
curl -s "$BASE/api/v1/recoverable-workflows?resumable_only=true&limit=5" -H "$B" \
  | jq '.items[] | {id: .workflow.id, priority: .workflow.priority,
                    score: .context_score, waited: .seconds_since_recoverable}'
# { "id": "6f1d2a44-...", "priority": "high", "score": 100, "waited": 31.4 }
```

`resumable_only=true` hides workflows whose checkpoint has blocking issues. Drop it to see
everything; check `blocking_issue_codes` yourself.

Then read the package **before** claiming — claiming a workflow you cannot finish wastes the lease
duration for everyone:

```bash
curl -s $BASE/api/v1/workflows/$WF/recovery-package -H "$B" \
  | jq '{resumable: .context_evaluation.resumable,
         score: .context_evaluation.score,
         blocking: [.context_evaluation.blocking_issues[].code],
         next: .resume_instructions.next_action,
         skip: .resume_instructions.must_not_repeat,
         preserve: .resume_instructions.must_preserve,
         parent: .resume_instructions.expected_parent_version}'
```

Claim, resume, work, complete — exactly as in SKILL.md §10.

---

## 3. Two agents race for the same workflow

Eight agents call `POST /claims` on the same workflow at the same instant. PostgreSQL serialises
them on the workflow row; a partial unique index makes a second active claim impossible.

**Winner** (`201`):

```json
{
  "claim": {"agent_id": "agent-b", "status": "active", "lease_generation": 1, "...": "..."},
  "lease_token": "lease_Xy9Qa8vN2mK4pR7sT1uW3xZ6bC0dE5fG8hJ2kL4nP6q",
  "fencing_token": 1,
  "workflow": {"status": "claimed", "current_agent_id": "agent-b"}
}
```

**Everyone else** (`409`):

```json
{
  "error": {
    "code": "CLAIM_ALREADY_HELD",
    "message": "This workflow already has an active claim.",
    "retryable": true,
    "retry_after_seconds": 298,
    "details": {
      "held_by_agent_id": "agent-b",
      "expires_at": "2026-07-10T18:35:00.100000Z",
      "lease_generation": 1
    }
  },
  "request_id": "b41c9e07f6d3"
}
```

Correct loser behaviour:

```python
response = claim(workflow_id)
if response.status_code == 409:
    code = response.json()["error"]["code"]
    if code in ("CLAIM_ALREADY_HELD", "WORKFLOW_NOT_RECOVERABLE"):
        # Do NOT sleep for retry_after_seconds. There is other work.
        return pick_another_workflow()
```

Waiting 298 seconds for one workflow while the queue holds others is the wrong call. `retry_after_seconds`
exists for the case where this workflow is the *only* work available.

---

## 4. A lease expires and the work is taken over

This is the scenario fencing tokens exist for.

```
t=0s    agent-b claims, lease_seconds=60      lease_generation = 1
t=10s   agent-b's network partitions. It keeps working, unaware.
t=60s   lease expires                         status: recoverable (automatic)
t=65s   agent-c claims                        lease_generation = 2
t=90s   agent-b's network returns. It submits a checkpoint with its old token.
```

Agent B's write:

```bash
curl -sX POST $BASE/api/v1/workflows/$WF/checkpoints -H "$B" -H "$JSON" \
  -d '{"parent_version":2,"lease_token":"'"$OLD_LEASE"'", "objective":"...", "...": "..."}'
```

```json
{
  "error": {
    "code": "FENCING_TOKEN_STALE",
    "message": "Your lease has been superseded by a newer claim generation.",
    "retryable": false,
    "retry_after_seconds": null,
    "details": {
      "your_lease_generation": 1,
      "current_lease_generation": 2,
      "current_agent_id": "agent-c"
    }
  },
  "request_id": "7c2e4b19a0f5"
}
```

**HTTP 409. `retryable: false`.** Agent B must stop immediately and discard its local state. The work
belongs to agent-c now. Nothing agent-b computed after `t=60s` may be written anywhere.

Contrast with the case where **nobody** has taken over yet:

```
t=0s    agent-b claims, lease_seconds=60      lease_generation = 1
t=60s   lease expires                         status: recoverable
t=70s   agent-b submits a checkpoint with its old token
```

```json
{
  "error": {
    "code": "LEASE_EXPIRED",
    "message": "Your lease expired. Re-claim the workflow before submitting progress.",
    "retryable": true,
    "details": {"claim_status": "active", "expired_at": "2026-07-10T18:35:00.100000Z",
                "lease_generation": 1}
  }
}
```

**HTTP 410.** Agent B may claim the workflow again (it will get `lease_generation: 2`) and continue.
It must re-read the latest checkpoint first: another agent may have written one.

The difference matters. `410` means *your lease lapsed, the work is still yours to take*.
`409 FENCING_TOKEN_STALE` means *the work is somebody else's now*.

---

## 5. A stale checkpoint write

Two agents both hold version 2 in memory. Both write with `parent_version: 2`. One wins.

Loser:

```json
{
  "error": {
    "code": "STALE_CHECKPOINT_VERSION",
    "message": "parent_version does not match the workflow's current checkpoint version.",
    "retryable": false,
    "details": {
      "your_parent_version": 2,
      "current_checkpoint_version": 3,
      "hint": "GET the workflow, read current_checkpoint_version, retry."
    }
  }
}
```

The rejection is itself recorded as a `stale_update_rejected` audit event, so you can see lost-update
attempts in `GET /events`.

Correct recovery:

```python
def write_checkpoint(workflow_id, my_progress, lease_token, attempts=3):
    for _ in range(attempts):
        workflow = get(f"/api/v1/workflows/{workflow_id}")
        parent = workflow["current_checkpoint_version"]
        latest = get(f"/api/v1/workflows/{workflow_id}/checkpoints/{parent}")

        merged = merge(latest, my_progress)   # union the completed steps, keep both decisions
        response = post(
            f"/api/v1/workflows/{workflow_id}/checkpoints",
            {**merged, "parent_version": parent, "lease_token": lease_token},
        )
        if response.status_code == 201:
            return response.json()
        if response.json()["error"]["code"] != "STALE_CHECKPOINT_VERSION":
            raise
    raise RuntimeError("could not write a checkpoint after 3 attempts")
```

Do **not** blindly retry with the same body. Re-read, merge, then write. The point of
`parent_version` is that the service tells you when your view is out of date.

---

## 6. An artifact fails verification

```bash
curl -sX POST $BASE/api/v1/workflows/$WF/artifacts -H "$A" -H "$JSON" \
  -d '{"name":"programs.json",
       "uri":"https://example.com/programs.json",
       "sha256":"0000000000000000000000000000000000000000000000000000000000000000",
       "verify":true}'
```

```json
{
  "error": {
    "code": "ARTIFACT_VERIFICATION_FAILED",
    "message": "The artifact could not be fetched or its checksum did not match.",
    "retryable": false,
    "details": {
      "artifact_id": "0d2b6c31-9f47-4c8a-b1e0-6a5d3f9c2718",
      "error": "Checksum mismatch: expected 0000…0000, got 9f86d081…0a08."
    }
  }
}
```

The artifact **is** stored, with `verification_status: "failed"`. Two consequences:

1. The workflow's context score drops by 20 and `ARTIFACT_VERIFICATION_FAILED` becomes a blocking
   issue. `resumable` turns `false`.
2. `POST /complete` fails with `422 COMPLETION_REQUIREMENTS_NOT_MET`, listing `NO_FAILED_ARTIFACTS`.

Fix by registering the artifact again with the correct checksum, then re-verify:

```bash
curl -sX POST $BASE/api/v1/workflows/$WF/artifacts/$ARTIFACT_ID/verify -H "$A" \
  | jq -r .verification_status
# "verified"
```

**As a replacement agent**, before you use any artifact:

```bash
curl -s $BASE/api/v1/workflows/$WF/artifacts -H "$B" \
  | jq -r '.items[] | select(.verification_status != "verified") | .name'
```

Anything printed is untrusted. Either verify it, or treat the data as missing.

You can also verify locally, which is what you should do when you download the file anyway:

```bash
curl -sL "$URI" -o programs.json
echo "$EXPECTED_SHA256  programs.json" | sha256sum --check
# programs.json: OK
```

---

## 7. Network timeout on completion

You send `POST /complete` and the connection drops. You do not know whether the workflow completed.

**With an `Idempotency-Key`** (always do this):

```bash
# The retry replays the original response.
curl -sX POST $BASE/api/v1/workflows/$WF/complete -H "$B" -H "$JSON" \
  -H "Idempotency-Key: complete-$WF" \
  -d '{"lease_token":"'"$LEASE"'","final_checkpoint_version":3}' -D - -o /dev/null | grep -i idempotent
# idempotent-replay: true
```

**Without one:**

```json
{"error": {"code": "WORKFLOW_ALREADY_COMPLETED",
           "message": "This workflow is already completed. Completion is not repeatable.",
           "retryable": false}}
```

That 409 is not a failure. It tells you the first attempt landed. If completing was your intent,
treat it as success. But the clean pattern is the idempotency key: one code path, one response.

---

## 8. Deciding whether to claim a degraded workflow

```bash
curl -s $BASE/api/v1/workflows/$WF/recovery-package -H "$B" | jq .context_evaluation
```

```json
{
  "resumable": false,
  "score": 65,
  "blocking_issues": [
    {"code": "MISSING_NEXT_ACTION", "severity": "blocking", "weight": 20,
     "message": "The checkpoint has no next_action, so a replacement agent has no entry point.",
     "field": "next_action", "details": {}}
  ],
  "warnings": [
    {"code": "MISSING_CONTEXT_SUMMARY", "severity": "warning", "weight": 8, "...": "..."},
    {"code": "NO_DECISIONS_RECORDED", "severity": "warning", "weight": 4, "...": "..."}
  ],
  "recommended_repairs": [
    "Set 'next_action' to the single concrete step to perform first.",
    "Write two to five sentences of 'context_summary' describing what happened.",
    "Record the choices already made so they are not re-litigated."
  ],
  "evaluated_checkpoint_version": 2,
  "min_score_for_resume": 50
}
```

A plain claim is refused:

```json
{"error": {"code": "DOMAIN_VALIDATION_FAILED",
           "message": "This workflow has blocking context issues. Re-send with 'acknowledge_blocking_issues': true if you still intend to resume it.",
           "details": {"code": "BLOCKING_CONTEXT_ISSUES", "score": 65, "blocking_issues": [...]}}}
```

Claim it only if you can reconstruct the missing information from `completed_steps`, `objective` and
the artifacts:

```bash
curl -sX POST $BASE/api/v1/workflows/$WF/claims -H "$B" -H "$JSON" \
  -d '{"lease_seconds":300,"acknowledge_blocking_issues":true,
       "note":"next_action is missing but completed_steps make the next step obvious"}'
```

The acknowledgement is recorded in the audit log. Your first act after resuming should be to write a
checkpoint that repairs the context — set `next_action`, write a `context_summary` — so the next
agent to inherit this work is not in the same position.
