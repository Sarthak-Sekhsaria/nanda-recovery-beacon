import Link from "next/link";
import { notFound } from "next/navigation";

import { AutoRefresh } from "@/components/auto-refresh";
import { CodeBlock, CopyButton } from "@/components/copy-button";
import { EventTimeline } from "@/components/event-timeline";
import {
  Empty,
  ErrorPanel,
  Field,
  Panel,
  PriorityBadge,
  ScoreBar,
  StatusBadge,
  VerificationBadge,
} from "@/components/ui";
import { ApiError, PUBLIC_API_URL, api } from "@/lib/api";
import {
  absoluteTime,
  bytes,
  duration,
  heartbeatHealth,
  relativeTime,
  scoreClass,
} from "@/lib/format";
import type { Checkpoint, Issue, RecoveryEvent, RecoveryPackage } from "@/lib/types";

export const dynamic = "force-dynamic";

type Params = Promise<{ id: string }>;

export async function generateMetadata({ params }: { params: Params }) {
  const { id } = await params;
  try {
    const workflow = await api.workflow(id);
    return { title: workflow.title };
  } catch {
    return { title: "Workflow" };
  }
}

function IssueList({ issues, tone }: { issues: Issue[]; tone: "blocking" | "warning" }) {
  if (issues.length === 0) return null;
  const styles =
    tone === "blocking"
      ? "border-rose-500/30 bg-rose-950/20"
      : "border-amber-500/25 bg-amber-950/10";
  const dot = tone === "blocking" ? "bg-rose-400" : "bg-amber-400";

  return (
    <ul className={`space-y-2 rounded-lg border p-3 ${styles}`}>
      {issues.map((issue) => (
        <li key={`${issue.code}-${issue.field}`} className="flex gap-2.5">
          <span className={`mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full ${dot}`} aria-hidden />
          <div className="min-w-0">
            <p className="flex flex-wrap items-baseline gap-2">
              <code className="font-mono text-xs text-ink-100">{issue.code}</code>
              <span className="font-mono text-[11px] text-ink-400">−{issue.weight}</span>
              {issue.field && (
                <span className="font-mono text-[11px] text-ink-400">{issue.field}</span>
              )}
            </p>
            <p className="mt-0.5 text-xs text-ink-300">{issue.message}</p>
          </div>
        </li>
      ))}
    </ul>
  );
}

function CheckpointView({ checkpoint }: { checkpoint: Checkpoint }) {
  return (
    <div className="space-y-5">
      <Field label="Objective">{checkpoint.objective}</Field>

      {checkpoint.next_action && (
        <div className="rounded-lg border border-sky-500/30 bg-sky-950/20 p-3">
          <p className="label text-sky-300/80">Next action</p>
          <p className="mt-1 text-sm text-ink-100">{checkpoint.next_action}</p>
        </div>
      )}

      <div className="grid gap-5 md:grid-cols-2">
        <div>
          <p className="label">Completed — do not repeat ({checkpoint.completed_steps.length})</p>
          {checkpoint.completed_steps.length === 0 ? (
            <p className="mt-2 text-sm text-ink-400">Nothing recorded yet.</p>
          ) : (
            <ul className="mt-2 space-y-1.5">
              {checkpoint.completed_steps.map((step) => (
                <li key={step} className="flex gap-2 text-sm text-ink-300">
                  <span className="mt-0.5 text-emerald-400" aria-hidden>
                    ✓
                  </span>
                  <span className="line-through decoration-base-600">{step}</span>
                </li>
              ))}
            </ul>
          )}
        </div>

        <div>
          <p className="label">Remaining ({checkpoint.remaining_steps.length})</p>
          {checkpoint.remaining_steps.length === 0 ? (
            <p className="mt-2 text-sm text-emerald-300">
              None — completion requirements satisfied.
            </p>
          ) : (
            <ul className="mt-2 space-y-1.5">
              {checkpoint.remaining_steps.map((step) => (
                <li key={step} className="flex gap-2 text-sm text-ink-200">
                  <span className="mt-0.5 text-ink-400" aria-hidden>
                    ○
                  </span>
                  {step}
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>

      {checkpoint.context_summary && (
        <Field label="Context summary">
          <p className="text-sm leading-relaxed text-ink-300">{checkpoint.context_summary}</p>
        </Field>
      )}

      <div>
        <p className="label">Decisions ({checkpoint.decisions.length})</p>
        {checkpoint.decisions.length === 0 ? (
          <p className="mt-2 text-sm text-ink-400">
            None recorded. A replacement agent may re-litigate choices already made.
          </p>
        ) : (
          <ul className="mt-2 space-y-2">
            {checkpoint.decisions.map((decision) => (
              <li key={decision.decision} className="rounded-lg border border-base-700 bg-base-850/50 p-3">
                <p className="text-sm text-ink-100">{decision.decision}</p>
                {decision.reason ? (
                  <p className="mt-1 text-xs text-ink-400">
                    <span className="text-ink-300">because</span> {decision.reason}
                  </p>
                ) : (
                  <p className="mt-1 text-xs text-amber-300">No reason recorded.</p>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>

      {Object.keys(checkpoint.variables).length > 0 && (
        <Field label="Variables">
          <pre className="mt-1 overflow-x-auto rounded-lg border border-base-700 bg-base-950 p-3 font-mono text-xs text-ink-300">
            {JSON.stringify(checkpoint.variables, null, 2)}
          </pre>
        </Field>
      )}

      <div className="grid grid-cols-2 gap-4 border-t border-base-800 pt-4 md:grid-cols-4">
        <Field label="Version">
          <span className="font-mono">
            v{checkpoint.version}
            {checkpoint.parent_version !== null && (
              <span className="text-ink-400"> ← v{checkpoint.parent_version}</span>
            )}
          </span>
        </Field>
        <Field label="Written by">
          <span className="font-mono text-xs">{checkpoint.producing_agent_id}</span>
        </Field>
        <Field label="Created">
          <span className="font-mono text-xs">{relativeTime(checkpoint.created_at)}</span>
        </Field>
        <Field label="Checksum">
          <span className="font-mono text-xs" title={checkpoint.content_checksum}>
            {checkpoint.content_checksum.slice(0, 12)}…
          </span>
        </Field>
      </div>
    </div>
  );
}

export default async function WorkflowDetailPage({ params }: { params: Params }) {
  const { id } = await params;

  let pkg: RecoveryPackage;
  let checkpoints: Checkpoint[];
  let events: RecoveryEvent[];

  try {
    pkg = await api.recoveryPackage(id);
    const [checkpointPage, eventPage] = await Promise.all([api.checkpoints(id), api.events(id)]);
    checkpoints = checkpointPage.items;
    events = eventPage.items;
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) notFound();
    const apiError = error instanceof ApiError ? error : new ApiError(0, "UNKNOWN", String(error));
    return <ErrorPanel code={apiError.code} message={apiError.message} />;
  }

  const { workflow, latest_checkpoint, context_evaluation, artifacts, active_claim, resume_instructions } = pkg;
  const heartbeat = heartbeatHealth(workflow.heartbeat_age_seconds, workflow.heartbeat_timeout_seconds);
  const leaseExpiresIn = active_claim
    ? (new Date(active_claim.expires_at).getTime() - Date.now()) / 1000
    : null;

  const curlClaim = `curl -X POST ${PUBLIC_API_URL}/api/v1/workflows/${workflow.id}/claims \\
  -H "Authorization: Bearer $BEACON_API_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{"lease_seconds": 300}'`;

  const curlPackage = `curl ${PUBLIC_API_URL}/api/v1/workflows/${workflow.id}/recovery-package \\
  -H "Authorization: Bearer $BEACON_API_KEY"`;

  const curlCheckpoint = `curl -X POST ${PUBLIC_API_URL}/api/v1/workflows/${workflow.id}/checkpoints \\
  -H "Authorization: Bearer $BEACON_API_KEY" \\
  -H "Content-Type: application/json" \\
  -H "Idempotency-Key: ${workflow.id}-v${workflow.current_checkpoint_version + 1}" \\
  -d '{
    "parent_version": ${workflow.current_checkpoint_version},${active_claim ? '\n    "lease_token": "$LEASE_TOKEN",' : ""}
    "objective": "${workflow.objective.replace(/"/g, '\\"')}",
    "completed_steps": [],
    "remaining_steps": [],
    "next_action": "…",
    "context_summary": "…"
  }'`;

  return (
    <div className="space-y-6">
      <nav className="flex items-center gap-2 text-xs text-ink-400">
        <Link href="/workflows" className="hover:text-ink-200">
          Workflows
        </Link>
        <span aria-hidden>/</span>
        <span className="font-mono text-ink-300">{workflow.id.slice(0, 8)}</span>
      </nav>

      <header className="flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <StatusBadge status={workflow.status} />
            <PriorityBadge priority={workflow.priority} />
            {workflow.recovery_count > 0 && (
              <span className="badge border-base-600 bg-base-800 text-ink-300">
                recovered {workflow.recovery_count}×
              </span>
            )}
            {workflow.failure_policy === "dead_letter" && (
              <span className="badge border-rose-500/40 bg-rose-500/10 text-rose-300">
                dead-letter on failure
              </span>
            )}
          </div>
          <h1 className="mt-2 text-2xl font-semibold tracking-tight text-ink-100">{workflow.title}</h1>
          <p className="mt-1 max-w-3xl text-sm text-ink-400">{workflow.objective}</p>
        </div>
        <div className="flex items-center gap-3">
          <CopyButton text={workflow.id} label="copy id" />
          <AutoRefresh seconds={10} />
        </div>
      </header>

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <div className="panel p-4">
          <p className="label">Checkpoint version</p>
          <p className="mt-1 font-mono text-2xl text-ink-100">v{workflow.current_checkpoint_version}</p>
          <p className="mt-1 text-xs text-ink-400">{workflow.checkpoint_count} immutable versions</p>
        </div>
        <div className="panel p-4">
          <p className="label">Current agent</p>
          <p className="mt-1 truncate font-mono text-sm text-ink-100">
            {workflow.current_agent_id ?? "unassigned"}
          </p>
          <p className="mt-1 text-xs text-ink-400">created by {workflow.creator_agent_id}</p>
        </div>
        <div className="panel p-4">
          <p className="label">Heartbeat</p>
          <p className={`mt-1 font-mono text-sm ${heartbeat.className}`}>
            {duration(workflow.heartbeat_age_seconds)} · {heartbeat.label}
          </p>
          <p className="mt-1 text-xs text-ink-400">
            timeout {workflow.heartbeat_timeout_seconds}s
          </p>
        </div>
        <div className="panel p-4">
          <p className="label">Context score</p>
          <p className={`mt-1 font-mono text-2xl ${scoreClass(context_evaluation.score)}`}>
            {context_evaluation.score}
          </p>
          <p className="mt-1 text-xs text-ink-400">
            {context_evaluation.resumable ? "safe to resume" : "not safe to resume"}
          </p>
        </div>
      </div>

      <div className="grid gap-6 lg:grid-cols-3">
        <div className="space-y-6 lg:col-span-2">
          <Panel
            title={latest_checkpoint ? `Latest checkpoint (v${latest_checkpoint.version})` : "Checkpoint"}
            description="Immutable. A new version is appended for every update."
          >
            {latest_checkpoint ? (
              <CheckpointView checkpoint={latest_checkpoint} />
            ) : (
              <Empty
                title="No checkpoint written"
                hint="Without a checkpoint there is no progress to recover. Context score is 0."
              />
            )}
          </Panel>

          <Panel
            title="Checkpoint history"
            description={`${checkpoints.length} version${checkpoints.length === 1 ? "" : "s"}, newest first. Each row shows what changed from its parent.`}
          >
            {checkpoints.length === 0 ? (
              <Empty title="No versions yet" />
            ) : (
              <ol className="space-y-2">
                {checkpoints.map((checkpoint) => {
                  const parent = checkpoints.find((c) => c.version === checkpoint.version - 1);
                  const newlyCompleted = parent
                    ? checkpoint.completed_steps.filter((step) => !parent.completed_steps.includes(step))
                    : checkpoint.completed_steps;

                  return (
                    <li
                      key={checkpoint.id}
                      className="rounded-lg border border-base-700 bg-base-850/40 p-3"
                    >
                      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
                        <span className="font-mono text-sm text-ink-100">v{checkpoint.version}</span>
                        <span className="font-mono text-xs text-ink-400">
                          {checkpoint.producing_agent_id}
                        </span>
                        <span className="font-mono text-xs text-ink-400">
                          {checkpoint.remaining_steps.length} remaining
                        </span>
                        <time
                          className="ml-auto font-mono text-[11px] text-ink-400"
                          dateTime={checkpoint.created_at}
                          title={absoluteTime(checkpoint.created_at)}
                        >
                          {relativeTime(checkpoint.created_at)}
                        </time>
                      </div>
                      {newlyCompleted.length > 0 && (
                        <ul className="mt-2 space-y-0.5">
                          {newlyCompleted.map((step) => (
                            <li key={step} className="flex gap-2 text-xs text-emerald-300/90">
                              <span aria-hidden>+</span>
                              {step}
                            </li>
                          ))}
                        </ul>
                      )}
                      {parent && parent.next_action !== checkpoint.next_action && (
                        <p className="mt-1.5 text-xs text-ink-400">
                          next action → <span className="text-ink-300">{checkpoint.next_action}</span>
                        </p>
                      )}
                    </li>
                  );
                })}
              </ol>
            )}
          </Panel>

          <Panel title="Audit trail" description="Append-only. Enforced by a database trigger.">
            <div className="max-h-[28rem] overflow-y-auto pr-1">
              <EventTimeline events={events} />
            </div>
          </Panel>
        </div>

        <div className="space-y-6">
          <Panel
            title="Context completeness"
            description={`Deterministic. Threshold for resume: ${context_evaluation.min_score_for_resume}.`}
          >
            <div className="space-y-4">
              <ScoreBar score={context_evaluation.score} />

              {context_evaluation.blocking_issues.length === 0 &&
              context_evaluation.warnings.length === 0 ? (
                <p className="text-sm text-emerald-300">
                  No issues. Another agent can resume this work safely.
                </p>
              ) : (
                <div className="space-y-3">
                  <IssueList issues={context_evaluation.blocking_issues} tone="blocking" />
                  <IssueList issues={context_evaluation.warnings} tone="warning" />
                </div>
              )}

              {context_evaluation.recommended_repairs.length > 0 && (
                <div>
                  <p className="label">Recommended repairs</p>
                  <ul className="mt-1.5 space-y-1">
                    {context_evaluation.recommended_repairs.map((repair) => (
                      <li key={repair} className="flex gap-2 text-xs text-ink-400">
                        <span aria-hidden>→</span>
                        {repair}
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </div>
          </Panel>

          <Panel title="Claim / lease">
            {active_claim ? (
              <div className="space-y-3">
                <div className="flex items-center justify-between">
                  <span className="badge border-violet-500/40 bg-violet-500/10 text-violet-300">
                    active lease
                  </span>
                  <span
                    className={`font-mono text-xs ${
                      (leaseExpiresIn ?? 0) < 30 ? "text-rose-300" : "text-ink-300"
                    }`}
                  >
                    expires in {duration(Math.max(0, leaseExpiresIn ?? 0))}
                  </span>
                </div>
                <Field label="Holder">
                  <span className="font-mono text-xs">{active_claim.agent_id}</span>
                </Field>
                <Field label="Fencing token (lease generation)">
                  <span className="font-mono text-xs">{active_claim.lease_generation}</span>
                </Field>
                <Field label="Token prefix">
                  <span className="font-mono text-xs">{active_claim.lease_token_prefix}…</span>
                </Field>
                <Field label="Renewals">
                  <span className="font-mono text-xs">{active_claim.renewal_count}</span>
                </Field>
                <p className="border-t border-base-800 pt-3 text-xs text-ink-400">
                  The lease token itself is stored only as a SHA-256 hash and is never returned by any
                  endpoint after the claim response.
                </p>
              </div>
            ) : workflow.status === "recoverable" ? (
              <div className="space-y-3">
                <p className="text-sm text-orange-300">No active lease. This work can be claimed now.</p>
                <CodeBlock code={curlClaim} caption="claim it" />
              </div>
            ) : (
              <Empty title="No active claim" hint="Claims exist only while a replacement agent holds this work." />
            )}
          </Panel>

          <Panel title={`Artifacts (${artifacts.length})`} description="Verify before you trust.">
            {artifacts.length === 0 ? (
              <Empty title="No artifacts registered" />
            ) : (
              <ul className="space-y-3">
                {artifacts.map((artifact) => (
                  <li key={artifact.id} className="rounded-lg border border-base-700 bg-base-850/40 p-3">
                    <div className="flex items-start justify-between gap-2">
                      <span className="truncate font-mono text-sm text-ink-100">{artifact.name}</span>
                      <VerificationBadge status={artifact.verification_status} />
                    </div>
                    {artifact.description && (
                      <p className="mt-1 text-xs text-ink-400">{artifact.description}</p>
                    )}
                    {artifact.uri && (
                      <a
                        href={artifact.uri}
                        rel="noreferrer noopener"
                        target="_blank"
                        className="mt-1 block truncate font-mono text-[11px] text-sky-300/80 hover:text-sky-300"
                      >
                        {artifact.uri}
                      </a>
                    )}
                    <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-ink-400">
                      {artifact.sha256 && (
                        <span className="font-mono" title={artifact.sha256}>
                          sha256 {artifact.sha256.slice(0, 10)}…
                        </span>
                      )}
                      <span>{bytes(artifact.size_bytes)}</span>
                      {artifact.checkpoint_version && <span>v{artifact.checkpoint_version}</span>}
                    </div>
                    {artifact.verification_error && (
                      <p className="mt-1.5 text-xs text-rose-300">{artifact.verification_error}</p>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </Panel>

          <Panel title="Resume instructions" description="Exactly what the API returns to a replacement agent.">
            <div className="space-y-4">
              <Field label="Next action">
                {resume_instructions.next_action ?? (
                  <span className="text-rose-300">none — blocking</span>
                )}
              </Field>
              <Field label={`Must not repeat (${resume_instructions.must_not_repeat.length})`}>
                {resume_instructions.must_not_repeat.length === 0 ? (
                  <span className="text-ink-400">nothing completed yet</span>
                ) : (
                  <ul className="space-y-0.5 text-xs text-ink-400">
                    {resume_instructions.must_not_repeat.map((step) => (
                      <li key={step}>· {step}</li>
                    ))}
                  </ul>
                )}
              </Field>
              <Field label={`Must preserve (${resume_instructions.must_preserve.length})`}>
                {resume_instructions.must_preserve.length === 0 ? (
                  <span className="text-ink-400">no decisions or artifacts recorded</span>
                ) : (
                  <ul className="space-y-0.5 text-xs text-ink-400">
                    {resume_instructions.must_preserve.map((item) => (
                      <li key={item}>· {item}</li>
                    ))}
                  </ul>
                )}
              </Field>
              <Field label="Expected parent_version">
                <span className="font-mono">{resume_instructions.expected_parent_version}</span>
              </Field>
            </div>
          </Panel>

          <Panel title="Timestamps">
            <dl className="space-y-2 text-xs">
              {(
                [
                  ["Created", workflow.created_at],
                  ["Updated", workflow.updated_at],
                  ["Failed", workflow.failed_at],
                  ["Recovered", workflow.recovered_at],
                  ["Completed", workflow.completed_at],
                  ["Last heartbeat", workflow.last_heartbeat_at],
                ] as const
              ).map(([label, value]) => (
                <div key={label} className="flex justify-between gap-3">
                  <dt className="text-ink-400">{label}</dt>
                  <dd className="font-mono text-ink-300" title={absoluteTime(value)}>
                    {value ? relativeTime(value) : "—"}
                  </dd>
                </div>
              ))}
            </dl>
          </Panel>
        </div>
      </div>

      <Panel title="Copyable API calls" description="Everything on this page comes from these endpoints.">
        <div className="grid gap-4 lg:grid-cols-3">
          <CodeBlock code={curlPackage} caption="read the recovery package" />
          <CodeBlock code={curlClaim} caption="claim the workflow" />
          <CodeBlock code={curlCheckpoint} caption="write the next checkpoint" />
        </div>
      </Panel>
    </div>
  );
}
