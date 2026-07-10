import Link from "next/link";

import { AutoRefresh } from "@/components/auto-refresh";
import { EventTimeline } from "@/components/event-timeline";
import { Empty, ErrorPanel, Panel, StatTile } from "@/components/ui";
import { ApiError, api } from "@/lib/api";
import { duration, scoreBarClass } from "@/lib/format";
import type { Stats } from "@/lib/types";

export const dynamic = "force-dynamic";

const BUCKETS = ["0-19", "20-39", "40-59", "60-79", "80-100"] as const;

function ScoreDistribution({ distribution }: { distribution: Record<string, number> }) {
  const total = Object.values(distribution).reduce((sum, count) => sum + count, 0);
  if (total === 0) {
    return <Empty title="No checkpoints scored yet" hint="Create a workflow to see its context score." />;
  }

  const max = Math.max(...Object.values(distribution), 1);

  return (
    <div className="space-y-3">
      {BUCKETS.map((bucket) => {
        const count = distribution[bucket] ?? 0;
        const midpoint = Number(bucket.split("-")[0]) + 10;
        return (
          <div key={bucket} className="flex items-center gap-3">
            <span className="w-16 shrink-0 font-mono text-xs text-ink-400">{bucket}</span>
            <div className="h-5 flex-1 overflow-hidden rounded bg-base-850">
              <div
                className={`h-full rounded ${scoreBarClass(midpoint)} opacity-80`}
                style={{ width: `${(count / max) * 100}%` }}
              />
            </div>
            <span className="w-8 shrink-0 text-right font-mono text-xs tabular-nums text-ink-300">
              {count}
            </span>
          </div>
        );
      })}
      <p className="pt-1 text-xs text-ink-400">
        Deterministic completeness score of each workflow&apos;s latest checkpoint. Below 50, or with any
        blocking issue, a workflow is not safe to resume.
      </p>
    </div>
  );
}

function StatusBreakdown({ counts }: { counts: Stats["status_counts"] }) {
  const rows = [
    { key: "active", label: "Active", tone: "bg-sky-400" },
    { key: "suspected_failed", label: "Suspected failed", tone: "bg-amber-400" },
    { key: "recoverable", label: "Recoverable", tone: "bg-orange-400" },
    { key: "claimed", label: "Claimed", tone: "bg-violet-400" },
    { key: "completed", label: "Completed", tone: "bg-emerald-400" },
    { key: "cancelled", label: "Cancelled", tone: "bg-slate-400" },
    { key: "dead_letter", label: "Dead letter", tone: "bg-rose-500" },
  ] as const;

  const total = Object.values(counts).reduce((sum, count) => sum + count, 0) || 1;

  return (
    <div className="space-y-2.5">
      <div className="flex h-2 overflow-hidden rounded-full bg-base-850">
        {rows.map(
          (row) =>
            counts[row.key] > 0 && (
              <div
                key={row.key}
                className={row.tone}
                style={{ width: `${(counts[row.key] / total) * 100}%` }}
                title={`${row.label}: ${counts[row.key]}`}
              />
            ),
        )}
      </div>
      <ul className="space-y-1.5 pt-1">
        {rows.map((row) => (
          <li key={row.key} className="flex items-center gap-2 text-sm">
            <span className={`h-2 w-2 rounded-full ${row.tone}`} aria-hidden />
            <span className="text-ink-300">{row.label}</span>
            <span className="ml-auto font-mono tabular-nums text-ink-200">{counts[row.key]}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

export default async function OverviewPage() {
  let stats: Stats;
  try {
    stats = await api.stats();
  } catch (error) {
    const apiError = error instanceof ApiError ? error : new ApiError(0, "UNKNOWN", String(error));
    return <ErrorPanel code={apiError.code} message={apiError.message} />;
  }

  const counts = stats.status_counts;
  const needsAttention = counts.recoverable + counts.suspected_failed;

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-ink-100">Recovery control center</h1>
          <p className="mt-1 text-sm text-ink-400">
            {stats.total_workflows} workflows · {stats.checkpoints_total} checkpoints ·{" "}
            {stats.events_total} audit events
          </p>
        </div>
        <AutoRefresh seconds={10} />
      </div>

      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <StatTile label="Active" value={counts.active} hint="An agent is working" href="/workflows?status=active" />
        <StatTile
          label="Recoverable"
          value={counts.recoverable}
          hint={needsAttention > 0 ? "waiting for an agent" : "queue is empty"}
          tone={counts.recoverable > 0 ? "attention" : "default"}
          href="/recoverable"
        />
        <StatTile
          label="Claimed"
          value={counts.claimed}
          hint={`${stats.active_claims} active leases`}
          href="/workflows?status=claimed"
        />
        <StatTile
          label="Completed"
          value={counts.completed}
          tone="good"
          hint="finished, terminal"
          href="/workflows?status=completed"
        />
      </div>

      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <StatTile
          label="Suspected failed"
          value={counts.suspected_failed}
          hint="heartbeat overdue"
          tone={counts.suspected_failed > 0 ? "attention" : "default"}
        />
        <StatTile
          label="Expired claims"
          value={stats.expired_claims}
          hint="leases reclaimed by the reaper"
          tone={stats.expired_claims > 0 ? "attention" : "default"}
        />
        <StatTile
          label="Dead letter"
          value={counts.dead_letter}
          hint="not offered for recovery"
          tone={counts.dead_letter > 0 ? "bad" : "default"}
        />
        <StatTile
          label="Avg. recovery time"
          value={
            stats.average_recovery_seconds === null
              ? "—"
              : duration(stats.average_recovery_seconds)
          }
          hint="failure → resumed"
        />
      </div>

      <div className="grid gap-6 lg:grid-cols-3">
        <Panel
          title="Recent recovery events"
          description="Append-only audit trail across every workflow."
          className="lg:col-span-2"
          action={
            <Link href="/workflows" className="text-xs text-ink-400 hover:text-ink-200">
              all workflows →
            </Link>
          }
        >
          <div className="max-h-[30rem] overflow-y-auto pr-1">
            <EventTimeline events={stats.recent_events} showWorkflow />
          </div>
        </Panel>

        <div className="space-y-6">
          <Panel title="Workflows by status">
            <StatusBreakdown counts={counts} />
          </Panel>

          <Panel title="Context completeness" description="Latest checkpoint of each workflow.">
            <ScoreDistribution distribution={stats.context_score_distribution} />
          </Panel>
        </div>
      </div>

      {counts.recoverable > 0 && (
        <Panel
          title="Work is waiting for a replacement agent"
          description={`${counts.recoverable} workflow${counts.recoverable === 1 ? "" : "s"} can be claimed right now.`}
          action={
            <Link
              href="/recoverable"
              className="rounded-lg border border-orange-500/40 bg-orange-500/10 px-3 py-1.5 text-xs font-medium text-orange-200 transition hover:bg-orange-500/20"
            >
              Open the recovery queue →
            </Link>
          }
        >
          <p className="text-sm text-ink-400">
            An agent discovers this work with{" "}
            <code className="rounded border border-base-700 bg-base-850 px-1.5 py-0.5 font-mono text-xs text-orange-200">
              GET /api/v1/recoverable-workflows?resumable_only=true
            </code>
            , reads the recovery package, then claims it.
          </p>
        </Panel>
      )}
    </div>
  );
}
