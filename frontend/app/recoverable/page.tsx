import Link from "next/link";

import { AutoRefresh } from "@/components/auto-refresh";
import { Empty, ErrorPanel, Panel } from "@/components/ui";
import { WorkflowCard } from "@/components/workflow-card";
import { ApiError, PUBLIC_API_URL, api } from "@/lib/api";
import type { RecoverableItem } from "@/lib/types";

export const dynamic = "force-dynamic";
export const metadata = { title: "Recovery queue" };

type SearchParams = Promise<Record<string, string | string[] | undefined>>;

export default async function RecoverablePage({ searchParams }: { searchParams: SearchParams }) {
  const params = await searchParams;
  const resumableOnly = params.resumable_only === "true";

  let queue: { items: RecoverableItem[]; has_more: boolean };
  try {
    queue = await api.recoverable({ limit: 50, resumable_only: resumableOnly || undefined });
  } catch (error) {
    const apiError = error instanceof ApiError ? error : new ApiError(0, "UNKNOWN", String(error));
    return <ErrorPanel code={apiError.code} message={apiError.message} />;
  }

  const safe = queue.items.filter((item) => item.resumable);
  const unsafe = queue.items.filter((item) => !item.resumable);

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-ink-100">Recovery queue</h1>
          <p className="mt-1 text-sm text-ink-400">
            Unfinished work waiting for a replacement agent, ranked by priority then by how long it has
            waited.
          </p>
        </div>
        <AutoRefresh seconds={10} />
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <Link
          href="/recoverable"
          className={`rounded-lg border px-3 py-1.5 text-xs transition ${
            resumableOnly
              ? "border-base-700 text-ink-400 hover:text-ink-200"
              : "border-sky-500/40 bg-sky-500/10 text-sky-200"
          }`}
        >
          All ({queue.items.length})
        </Link>
        <Link
          href="/recoverable?resumable_only=true"
          className={`rounded-lg border px-3 py-1.5 text-xs transition ${
            resumableOnly
              ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-200"
              : "border-base-700 text-ink-400 hover:text-ink-200"
          }`}
        >
          Safe to resume only
        </Link>
        <span className="text-xs text-ink-400">
          Equivalent to{" "}
          <code className="rounded border border-base-700 bg-base-850 px-1.5 py-0.5 font-mono text-[11px] text-orange-200">
            GET /api/v1/recoverable-workflows{resumableOnly ? "?resumable_only=true" : ""}
          </code>
        </span>
      </div>

      {queue.items.length === 0 ? (
        <Empty
          title="Nothing is waiting for recovery"
          hint="A workflow lands here when an agent reports failure, or when its heartbeat deadline passes."
        />
      ) : (
        <div className="space-y-8">
          {safe.length > 0 && (
            <section className="space-y-3">
              <h2 className="flex items-center gap-2 text-sm font-semibold text-ink-100">
                <span className="h-1.5 w-1.5 rounded-full bg-emerald-400" aria-hidden />
                Safe to resume
                <span className="font-mono text-xs font-normal text-ink-400">{safe.length}</span>
              </h2>
              <p className="text-xs text-ink-400">
                The context evaluation reports no blocking issues. Claim, read the recovery package,
                resume.
              </p>
              <div className="grid gap-4 lg:grid-cols-2">
                {safe.map((item) => (
                  <WorkflowCard
                    key={item.workflow.id}
                    workflow={item.workflow}
                    contextScore={item.context_score}
                    resumable={item.resumable}
                    waitingSeconds={item.seconds_since_recoverable}
                  />
                ))}
              </div>
            </section>
          )}

          {unsafe.length > 0 && (
            <section className="space-y-3">
              <h2 className="flex items-center gap-2 text-sm font-semibold text-ink-100">
                <span className="h-1.5 w-1.5 rounded-full bg-rose-400" aria-hidden />
                Blocking issues — resume with care
                <span className="font-mono text-xs font-normal text-ink-400">{unsafe.length}</span>
              </h2>
              <p className="text-xs text-ink-400">
                A claim on these requires{" "}
                <code className="rounded border border-base-700 bg-base-850 px-1.5 py-0.5 font-mono text-[11px] text-orange-200">
                  &quot;acknowledge_blocking_issues&quot;: true
                </code>
                . The acknowledgement is written to the audit log.
              </p>
              <div className="grid gap-4 lg:grid-cols-2">
                {unsafe.map((item) => (
                  <WorkflowCard
                    key={item.workflow.id}
                    workflow={item.workflow}
                    contextScore={item.context_score}
                    resumable={item.resumable}
                    blockingIssues={item.blocking_issue_codes}
                    waitingSeconds={item.seconds_since_recoverable}
                  />
                ))}
              </div>
            </section>
          )}
        </div>
      )}

      <Panel title="How an agent takes this work" description="Four calls, in this order.">
        <ol className="space-y-2 text-sm text-ink-300">
          {[
            `GET  ${PUBLIC_API_URL}/api/v1/recoverable-workflows?resumable_only=true`,
            `GET  ${PUBLIC_API_URL}/api/v1/workflows/{id}/recovery-package`,
            `POST ${PUBLIC_API_URL}/api/v1/workflows/{id}/claims`,
            `POST ${PUBLIC_API_URL}/api/v1/workflows/{id}/resume`,
          ].map((line, index) => (
            <li key={line} className="flex gap-3">
              <span className="font-mono text-xs text-ink-400">{index + 1}.</span>
              <code className="min-w-0 flex-1 overflow-x-auto font-mono text-xs text-ink-200">{line}</code>
            </li>
          ))}
        </ol>
        <p className="mt-4 text-xs text-ink-400">
          Exactly one agent can hold a claim. The losers of a race receive{" "}
          <code className="font-mono text-rose-300">409 CLAIM_ALREADY_HELD</code> and should pick
          different work rather than wait.
        </p>
      </Panel>
    </div>
  );
}
