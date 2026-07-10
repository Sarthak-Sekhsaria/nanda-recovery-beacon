#!/usr/bin/env bash
# Smoke-test a live NANDA Recovery Beacon deployment.
#
#   ./scripts/verify_deployment.sh https://nanda-recovery-beacon-api.onrender.com [API_KEY]
#
# Exercises the full recovery lifecycle against the real service: create -> checkpoint ->
# fail -> discover -> claim -> resume -> checkpoint -> complete -> audit.
# Exits non-zero on the first failure.

set -euo pipefail

BASE="${1:-}"
KEY="${2:-}"

if [[ -z "$BASE" ]]; then
  echo "usage: $0 <base-url> [api-key]" >&2
  exit 64
fi
BASE="${BASE%/}"

command -v jq >/dev/null || { echo "jq is required" >&2; exit 69; }

pass=0
fail=0

auth_a=()
auth_b=()
if [[ -n "$KEY" ]]; then
  auth_a=(-H "Authorization: Bearer $KEY")
  auth_b=(-H "Authorization: Bearer $KEY")
else
  echo "No API key supplied: assuming DEMO_MODE=true and using X-Agent-Id."
  auth_a=(-H "X-Agent-Id: verify-agent-a")
  auth_b=(-H "X-Agent-Id: verify-agent-b")
fi

check() {
  local label="$1" actual="$2" expected="$3"
  if [[ "$actual" == "$expected" ]]; then
    printf '  \033[32mPASS\033[0m  %-46s %s\n' "$label" "$actual"
    pass=$((pass + 1))
  else
    printf '  \033[31mFAIL\033[0m  %-46s got %-18s want %s\n' "$label" "$actual" "$expected"
    fail=$((fail + 1))
  fi
}

status_of() { curl -s -o /dev/null -w '%{http_code}' "$@"; }

echo
echo "Verifying $BASE"
echo "── Public endpoints ────────────────────────────────────────────────────"

check "GET /health"        "$(status_of "$BASE/health")" "200"
check "GET /ready"         "$(status_of "$BASE/ready")" "200"
check "GET /skill.md"      "$(status_of "$BASE/skill.md")" "200"
check "GET /openapi.json"  "$(status_of "$BASE/openapi.json")" "200"
check "GET /metrics"       "$(status_of "$BASE/metrics")" "200"
check "GET /docs"          "$(status_of "$BASE/docs")" "200"

skill=$(curl -s "$BASE/skill.md")
check "skill.md has frontmatter" "$(head -1 <<<"$skill")" "---"
if grep -q '{{PUBLIC_BASE_URL}}' <<<"$skill"; then
  printf '  \033[31mFAIL\033[0m  %-46s placeholder not substituted\n' "skill.md base URL"
  fail=$((fail + 1))
else
  printf '  \033[32mPASS\033[0m  %-46s substituted\n' "skill.md base URL"
  pass=$((pass + 1))
fi

echo
echo "── Recovery lifecycle ──────────────────────────────────────────────────"

WF=$(curl -s -X POST "$BASE/api/v1/workflows" "${auth_a[@]}" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: verify-$(date +%s)-$RANDOM" \
  -d '{"title":"Deployment smoke test",
       "objective":"Prove the recovery lifecycle works on this deployment",
       "priority":"low","heartbeat_timeout_seconds":30,"tags":["smoke-test"],
       "initial_checkpoint":{
         "objective":"Prove the recovery lifecycle works on this deployment",
         "completed_steps":["Created the workflow"],
         "remaining_steps":["Fail it","Recover it","Complete it"],
         "decisions":[{"decision":"Use a low priority","reason":"This is only a smoke test run"}],
         "next_action":"Report an explicit failure",
         "context_summary":"A synthetic workflow created by scripts/verify_deployment.sh."}}' \
  | jq -r '.id // empty')

if [[ -z "$WF" ]]; then
  echo "  FAIL  could not create a workflow (is the API key valid? is DEMO_MODE on?)" >&2
  exit 1
fi
printf '  workflow: %s\n' "$WF"

check "POST /workflows"    "$(curl -s "$BASE/api/v1/workflows/$WF" "${auth_a[@]}" | jq -r .status)" "active"
check "POST /heartbeats"   "$(status_of -X POST "$BASE/api/v1/workflows/$WF/heartbeats" "${auth_a[@]}" -H 'Content-Type: application/json' -d '{}')" "200"
check "POST /evaluate-context resumable" \
  "$(curl -s -X POST "$BASE/api/v1/workflows/$WF/evaluate-context" "${auth_a[@]}" -H 'Content-Type: application/json' -d '{}' | jq -r .resumable)" "true"

check "POST /fail -> recoverable" \
  "$(curl -s -X POST "$BASE/api/v1/workflows/$WF/fail" "${auth_a[@]}" -H 'Content-Type: application/json' \
     -d '{"reason":"Synthetic failure from the deployment smoke test"}' | jq -r .status)" "recoverable"

check "GET /recoverable-workflows finds it" \
  "$(curl -s "$BASE/api/v1/recoverable-workflows?tag=smoke-test&limit=100" "${auth_b[@]}" \
     | jq -r --arg id "$WF" '[.items[] | select(.workflow.id == $id)] | length')" "1"

check "GET /recovery-package resumable" \
  "$(curl -s "$BASE/api/v1/workflows/$WF/recovery-package" "${auth_b[@]}" | jq -r .context_evaluation.resumable)" "true"

CLAIM=$(curl -s -X POST "$BASE/api/v1/workflows/$WF/claims" "${auth_b[@]}" \
  -H 'Content-Type: application/json' -d '{"lease_seconds":120}')
LEASE=$(jq -r '.lease_token // empty' <<<"$CLAIM")
check "POST /claims returns a lease" "$([[ -n "$LEASE" ]] && echo yes || echo no)" "yes"
check "fencing token" "$(jq -r .fencing_token <<<"$CLAIM")" "1"

check "second claim conflicts" \
  "$(curl -s -X POST "$BASE/api/v1/workflows/$WF/claims" "${auth_a[@]}" -H 'Content-Type: application/json' \
     -d '{"lease_seconds":120}' | jq -r .error.code)" "CLAIM_ALREADY_HELD"

check "POST /resume" \
  "$(curl -s -X POST "$BASE/api/v1/workflows/$WF/resume" "${auth_b[@]}" -H 'Content-Type: application/json' \
     -d "{\"lease_token\":\"$LEASE\"}" | jq -r .status)" "active"

check "stale parent_version rejected" \
  "$(curl -s -X POST "$BASE/api/v1/workflows/$WF/checkpoints" "${auth_b[@]}" -H 'Content-Type: application/json' \
     -d "{\"parent_version\":0,\"lease_token\":\"$LEASE\",\"objective\":\"x\",\"remaining_steps\":[\"y\"],\"next_action\":\"z\"}" \
     | jq -r .error.code)" "STALE_CHECKPOINT_VERSION"

check "POST /checkpoints v2" \
  "$(curl -s -X POST "$BASE/api/v1/workflows/$WF/checkpoints" "${auth_b[@]}" -H 'Content-Type: application/json' \
     -d "{\"parent_version\":1,\"lease_token\":\"$LEASE\",
          \"objective\":\"Prove the recovery lifecycle works on this deployment\",
          \"completed_steps\":[\"Created the workflow\",\"Failed it\",\"Recovered it\"],
          \"remaining_steps\":[],
          \"decisions\":[{\"decision\":\"Complete the smoke test\",\"reason\":\"All checks so far have passed\"}],
          \"next_action\":\"Nothing remains; ready to complete\",
          \"context_summary\":\"The workflow was created, failed, discovered, claimed and resumed.\"}" \
     | jq -r .version)" "2"

check "POST /complete" \
  "$(curl -s -X POST "$BASE/api/v1/workflows/$WF/complete" "${auth_b[@]}" -H 'Content-Type: application/json' \
     -H "Idempotency-Key: complete-$WF" \
     -d "{\"lease_token\":\"$LEASE\",\"final_checkpoint_version\":2,\"summary\":\"smoke test\"}" \
     | jq -r .status)" "completed"

check "completion replays idempotently" \
  "$(curl -s -D - -o /dev/null -X POST "$BASE/api/v1/workflows/$WF/complete" "${auth_b[@]}" \
     -H 'Content-Type: application/json' -H "Idempotency-Key: complete-$WF" \
     -d "{\"lease_token\":\"$LEASE\",\"final_checkpoint_version\":2,\"summary\":\"smoke test\"}" \
     | tr -d '\r' | awk -F': ' 'tolower($1)=="idempotent-replay"{print $2}')" "true"

events=$(curl -s "$BASE/api/v1/workflows/$WF/events?limit=100" "${auth_b[@]}" | jq -r '.items[].event_type' | sort -u)
for expected in workflow_created checkpoint_created explicit_failure_reported workflow_made_recoverable \
                claim_acquired workflow_resumed stale_update_rejected workflow_completed; do
  check "audit event: $expected" "$(grep -cx "$expected" <<<"$events" || true)" "1"
done

check "immutable history intact" \
  "$(curl -s "$BASE/api/v1/workflows/$WF/checkpoints" "${auth_b[@]}" | jq -r '[.items[].version] | @csv')" '2,1'

echo
echo "── Security ────────────────────────────────────────────────────────────"
check "unknown route -> 404" "$(status_of "$BASE/api/v1/nope")" "404"
check "unknown workflow -> 404" "$(status_of "$BASE/api/v1/workflows/00000000-0000-0000-0000-000000000000" "${auth_a[@]}")" "404"
check "bad key -> 401" "$(status_of "$BASE/api/v1/workflows" -H 'Authorization: Bearer nrb_invalid')" "401"
check "security header" "$(curl -sI "$BASE/health" | tr -d '\r' | awk -F': ' 'tolower($1)=="x-content-type-options"{print $2}')" "nosniff"

echo
echo "────────────────────────────────────────────────────────────────────────"
printf 'passed: %d   failed: %d\n' "$pass" "$fail"
echo "workflow used: $BASE/api/v1/workflows/$WF"
echo

[[ $fail -eq 0 ]]
