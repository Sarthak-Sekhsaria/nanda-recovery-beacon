# Checkpoint schema and context scoring

A checkpoint is an **immutable, versioned snapshot** of a workflow's progress. Version *N* answers
one question: *if the current agent vanished right now, what would a replacement need to know?*

Checkpoints are never updated. `UPDATE` and `DELETE` on the `checkpoints` table are blocked by a
PostgreSQL trigger, not merely by the absence of an endpoint.

---

## 1. Field reference

Current `schema_version`: **`1.0`**. Sending an unknown value returns
`422 UNSUPPORTED_SCHEMA_VERSION`.

| Field | Type | Required | Semantics |
| --- | --- | --- | --- |
| `parent_version` | integer ≥ 0 | **yes** (writes only) | The version you read before writing. `0` for the first checkpoint. If it does not equal the workflow's `current_checkpoint_version`, the write is rejected with `409 STALE_CHECKPOINT_VERSION`. |
| `objective` | string | yes | The goal of the whole workflow, in one sentence. Restate it in every checkpoint; a replacement agent may never see version 1. |
| `completed_steps` | string[] | yes (may be `[]`) | **Cumulative.** Everything finished so far, not just since the last version. A replacement agent treats this as "do not repeat". |
| `remaining_steps` | string[] | yes (may be `[]`) | Everything still to do. Empty means the work is finished and the workflow may be completed. |
| `decisions` | object[] | no | `[{"decision": "...", "reason": "...", "made_at": "..."}]`. Prevents a replacement agent from re-litigating a choice. A `reason` shorter than 12 characters counts as missing. |
| `next_action` | string | in practice yes | The **single** concrete step to perform first. Omitting it is a blocking issue: a replacement agent has no entry point. |
| `context_summary` | string | strongly recommended | Two to five sentences of narrative. What happened, what was learned, what surprised you. |
| `variables` | object | no | Structured state a replacement needs: counters, cursors, IDs, partial results. Keep it small. |
| `schema_version` | string | no | Defaults to `"1.0"`. |
| `lease_token` | string | conditional | Required on writes while an active claim exists on the workflow. |

Server-assigned, read-only fields:

| Field | Meaning |
| --- | --- |
| `id` | Checkpoint UUID. |
| `version` | Monotonic integer, starting at 1. Assigned by the service as `parent_version + 1`. |
| `producing_agent_id` | The agent that wrote this version. |
| `lease_generation` | The fencing token in force when the version was written. |
| `content_checksum` | SHA-256 of the canonical JSON of the content fields plus the version. Identical content at the same version always yields the same checksum. |
| `created_at` | UTC timestamp. |

### Never put in a checkpoint

- API keys, lease tokens, passwords, bearer tokens, connection strings.
- Personal data you would not want another agent to read.
- Large blobs. Register those as **artifacts** and reference them by URL and SHA-256.

---

## 2. Writing a good checkpoint

Bad:

```json
{
  "objective": "do the thing",
  "completed_steps": ["step 1", "step 2"],
  "remaining_steps": ["step 3"],
  "next_action": "continue"
}
```

Nothing here survives the loss of the original agent. `"continue"` is not an action, `"step 1"` is
not a fact, and no reader learns what "the thing" is.

Good:

```json
{
  "parent_version": 2,
  "objective": "Compare five scholarship programs and recommend one to the user",
  "completed_steps": [
    "Retrieved the five programs from https://example.com/programs",
    "Collected eligibility requirements for all five into programs.json",
    "Filtered out two programs closed to international students"
  ],
  "remaining_steps": [
    "Compare the application deadlines of the three remaining programs",
    "Write a one-paragraph recommendation naming exactly one program"
  ],
  "decisions": [
    {
      "decision": "Only include programs open to international students",
      "reason": "The user's original request said they hold a student visa"
    },
    {
      "decision": "Treat a rolling deadline as 'no deadline' rather than 'today'",
      "reason": "Rolling admissions do not impose a cutoff, so ranking by date would mislead"
    }
  ],
  "next_action": "Read the deadline field for each of the three remaining programs in programs.json",
  "context_summary": "Five programs were found in the official directory. Eligibility data for all five is stored in the programs.json artifact (sha256 verified). Two programs are closed to international students and were excluded; three remain. Deadlines have not been read yet.",
  "variables": {
    "programs_total": 5,
    "programs_remaining": 3,
    "excluded_program_ids": ["p2", "p4"]
  }
}
```

Rules of thumb:

1. **`completed_steps` is a fact log, not a plan.** Each entry should be verifiable by reading an
   artifact or re-running a query.
2. **`next_action` is one step.** If it contains "and", split it.
3. **`context_summary` explains the *why*.** The step lists explain the *what*.
4. **Every judgement call becomes a decision.** If you chose between two reasonable options, record
   the choice and the reason. This is the single highest-value field for recovery.
5. **Write a checkpoint after every step whose repetition would be expensive.**

---

## 3. Context completeness scoring

`POST /api/v1/workflows/{workflow_id}/evaluate-context` runs a **deterministic** evaluator.
No language model is involved. The same checkpoint always produces the same score.

### Algorithm

1. Start at `100`.
2. Evaluate every rule below. Each rule that fires subtracts its `weight`.
3. `score = max(0, 100 - sum(weights of fired rules))`.
4. `resumable = (no blocking rule fired) AND (score >= min_score_for_resume)`.

`min_score_for_resume` defaults to `50` and is returned in every evaluation response.

A **blocking** issue means a replacement agent cannot safely continue. A **warning** means the work
can continue but something is degraded. Warnings alone never set `resumable: false` unless their
combined weight drops the score below the threshold.

### Rule table

This table is generated from `backend/app/context_eval.py`. It cannot drift from the code.

| Code | Severity | Weight | Meaning | Repair |
| --- | --- | --- | --- | --- |
| `NO_CHECKPOINT` | blocking | 100 | The workflow has no checkpoint, so there is no progress to resume from. | Create a checkpoint with POST /api/v1/workflows/{workflow_id}/checkpoints. |
| `UNSUPPORTED_SCHEMA_VERSION` | blocking | 30 | The checkpoint uses a schema_version this service cannot interpret. | Re-submit the checkpoint using a supported schema_version. |
| `MISSING_OBJECTIVE` | blocking | 25 | The checkpoint has no objective, so the goal of the work is unknown. | Set 'objective' to a one-sentence statement of the overall goal. |
| `MISSING_NEXT_ACTION` | blocking | 20 | The checkpoint has no next_action, so a replacement agent has no entry point. | Set 'next_action' to the single concrete step to perform first. |
| `COMPLETE_WITH_REMAINING_STEPS` | blocking | 20 | The workflow is marked complete while remaining_steps is not empty. | Clear 'remaining_steps' in a new checkpoint before completing. |
| `ARTIFACT_VERIFICATION_FAILED` | blocking | 20 | An artifact failed checksum verification and must not be trusted. | Re-upload the artifact and register it with a correct sha256. |
| `NO_REMAINING_STEPS_BUT_INCOMPLETE` | blocking | 15 | The workflow is not complete but remaining_steps is empty. | List the outstanding steps in 'remaining_steps', or complete the workflow. |
| `CONTRADICTORY_STEPS` | blocking | 15 | One or more steps appear in both completed_steps and remaining_steps. | Remove the duplicated steps from one of the two lists. |
| `ARTIFACT_MISSING_LOCATION` | blocking | 15 | An artifact has neither a uri nor a storage_key, so it cannot be retrieved. | Provide 'uri' or 'storage_key' for every artifact. |
| `CHECKPOINT_VERSION_GAP` | warning | 10 | parent_version is not exactly one less than version; history has a gap. | Always set 'parent_version' to the version you read before writing. |
| `ARTIFACT_MISSING_CHECKSUM` | warning | 8 | An artifact has no sha256, so its integrity cannot be verified. | Register the artifact's sha256 so a replacement agent can verify it. |
| `MISSING_CONTEXT_SUMMARY` | warning | 8 | The checkpoint has no context_summary explaining the situation so far. | Write two to five sentences of 'context_summary' describing what happened. |
| `DECISION_WITHOUT_REASON` | warning | 6 | A decision was recorded without an explanation of why it was made. | Give every decision a 'reason' of at least 12 characters. |
| `ARTIFACT_UNVERIFIED` | warning | 5 | An artifact has a checksum that has never been verified against its content. | Call POST /api/v1/workflows/{workflow_id}/artifacts/{artifact_id}/verify. |
| `MISSING_PARENT_VERSION` | warning | 5 | A checkpoint after version 1 does not record its parent_version. | Set 'parent_version' on every checkpoint after the first. |
| `NO_DECISIONS_RECORDED` | warning | 4 | No decisions were recorded, so prior judgement calls may be repeated. | Record the choices already made so they are not re-litigated. |

### Notes on individual rules

- **`CONTRADICTORY_STEPS`** compares steps after normalising whitespace and case. `"Compare
  deadlines"` and `"compare  DEADLINES "` are the same step.
- **`NO_DECISIONS_RECORDED`** only fires when `completed_steps` is non-empty. A workflow that has
  done nothing yet has made no decisions to record.
- **`DECISION_WITHOUT_REASON`** fires at most once, even if several decisions lack reasons. The
  repair applies to all of them.
- **`ARTIFACT_*`** rules fire at most once each, no matter how many artifacts are affected. The
  offending artifact's name is in `issue.field`.
- **`CHECKPOINT_VERSION_GAP`** and **`MISSING_PARENT_VERSION`** cannot be produced through the API,
  because the service assigns `version = parent_version + 1` and rejects a mismatched
  `parent_version`. They exist as defence against data imported by other means.

### Worked scoring examples

| Checkpoint | Rules fired | Score | Resumable |
| --- | --- | --- | --- |
| Complete, with reasons and a summary | none | 100 | yes |
| No `context_summary` | `MISSING_CONTEXT_SUMMARY` (8) | 92 | yes |
| No `next_action` | `MISSING_NEXT_ACTION` (20) | 80 | **no** — blocking |
| No `next_action`, no summary | `MISSING_NEXT_ACTION` (20), `MISSING_CONTEXT_SUMMARY` (8) | 72 | **no** — blocking |
| Only warnings totalling 51 | several | 49 | **no** — below threshold |
| No checkpoint at all | `NO_CHECKPOINT` (100) | 0 | **no** |

A workflow with blocking issues can still be claimed, but only if the claiming agent sends
`"acknowledge_blocking_issues": true`. That flag is recorded in the audit log.
