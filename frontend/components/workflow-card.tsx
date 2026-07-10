import Link from "next/link";

import { duration, heartbeatHealth, relativeTime, shortId } from "@/lib/format";
import type { Workflow } from "@/lib/types";

import { PriorityBadge, ScoreBar, StatusBadge } from "./ui";

export function WorkflowCard({
  workflow,
  contextScore,
  resumable,
  blockingIssues,
  waitingSeconds,
}: {
  workflow: Workflow;
  contextScore?: number;
  resumable?: boolean;
  blockingIssues?: string[];
  waitingSeconds?: number;
}) {
  const heartbeat = heartbeatHealth(workflow.heartbeat_age_seconds, workflow.heartbeat_timeout_seconds);

  return (
    <Link
      href={`/workflows/${workflow.id}`}
      className="panel group block p-4 transition hover:border-base-600 hover:bg-base-900"
    >
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <h3 className="truncate text-sm font-semibold text-ink-100 group-hover:text-white">
            {workflow.title}
          </h3>
          <p className="mt-0.5 line-clamp-2 text-xs text-ink-400">{workflow.objective}</p>
        </div>
        <div className="flex shrink-0 items-center gap-1.5">
          <PriorityBadge priority={workflow.priority} />
          <StatusBadge status={workflow.status} />
        </div>
      </div>

      <dl className="mt-4 grid grid-cols-2 gap-x-4 gap-y-2 text-xs sm:grid-cols-4">
        <div>
          <dt className="label">Version</dt>
          <dd className="mt-0.5 font-mono text-ink-200">v{workflow.current_checkpoint_version}</dd>
        </div>
        <div>
          <dt className="label">Agent</dt>
          <dd className="mt-0.5 truncate font-mono text-ink-200">
            {workflow.current_agent_id ?? "—"}
          </dd>
        </div>
        <div>
          <dt className="label">Heartbeat</dt>
          <dd className={`mt-0.5 font-mono ${heartbeat.className}`}>
            {duration(workflow.heartbeat_age_seconds)} · {heartbeat.label}
          </dd>
        </div>
        <div>
          <dt className="label">{waitingSeconds !== undefined ? "Waiting" : "Updated"}</dt>
          <dd className="mt-0.5 font-mono text-ink-300">
            {waitingSeconds !== undefined
              ? duration(waitingSeconds)
              : relativeTime(workflow.updated_at)}
          </dd>
        </div>
      </dl>

      {contextScore !== undefined && (
        <div className="mt-4 space-y-1.5">
          <div className="flex items-center justify-between text-xs">
            <span className="label">Context completeness</span>
            {resumable !== undefined && (
              <span className={resumable ? "text-emerald-300" : "text-rose-300"}>
                {resumable ? "safe to resume" : "blocking issues"}
              </span>
            )}
          </div>
          <ScoreBar score={contextScore} />
          {blockingIssues && blockingIssues.length > 0 && (
            <p className="pt-0.5 font-mono text-[11px] text-rose-300/90">
              {blockingIssues.join(", ")}
            </p>
          )}
        </div>
      )}

      <div className="mt-4 flex items-center justify-between border-t border-base-800 pt-3">
        <div className="flex flex-wrap gap-1">
          {workflow.tags.slice(0, 4).map((tag) => (
            <span key={tag} className="rounded border border-base-700 bg-base-850 px-1.5 py-0.5 text-[11px] text-ink-400">
              {tag}
            </span>
          ))}
        </div>
        <span className="font-mono text-[11px] text-ink-400">{shortId(workflow.id)}</span>
      </div>
    </Link>
  );
}
