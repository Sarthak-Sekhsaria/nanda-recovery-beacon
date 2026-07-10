import Link from "next/link";

import { AutoRefresh } from "@/components/auto-refresh";
import { Empty, ErrorPanel } from "@/components/ui";
import { WorkflowCard } from "@/components/workflow-card";
import { ApiError, api } from "@/lib/api";
import type { Page as ApiPage, Workflow } from "@/lib/types";

export const dynamic = "force-dynamic";
export const metadata = { title: "Workflows" };

const STATUSES = [
  "",
  "active",
  "suspected_failed",
  "recoverable",
  "claimed",
  "completed",
  "cancelled",
  "dead_letter",
] as const;

const PRIORITIES = ["", "critical", "high", "normal", "low"] as const;

type SearchParams = Promise<Record<string, string | string[] | undefined>>;

function one(value: string | string[] | undefined): string {
  return Array.isArray(value) ? (value[0] ?? "") : (value ?? "");
}

function buildQuery(base: Record<string, string>, overrides: Record<string, string>): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries({ ...base, ...overrides })) {
    if (value) params.set(key, value);
  }
  const query = params.toString();
  return query ? `?${query}` : "";
}

export default async function WorkflowsPage({ searchParams }: { searchParams: SearchParams }) {
  const params = await searchParams;
  const filters = {
    status: one(params.status),
    priority: one(params.priority),
    tag: one(params.tag),
    search: one(params.search),
    cursor: one(params.cursor),
  };

  let page: ApiPage<Workflow>;
  try {
    page = await api.workflows({
      limit: 24,
      status: filters.status || undefined,
      priority: filters.priority || undefined,
      tag: filters.tag || undefined,
      search: filters.search || undefined,
      cursor: filters.cursor || undefined,
    });
  } catch (error) {
    const apiError = error instanceof ApiError ? error : new ApiError(0, "UNKNOWN", String(error));
    return <ErrorPanel code={apiError.code} message={apiError.message} />;
  }

  const baseFilters = { ...filters, cursor: "" };

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-ink-100">Workflow explorer</h1>
          <p className="mt-1 text-sm text-ink-400">
            {page.items.length} shown{page.has_more ? ", more available" : ""}
          </p>
        </div>
        <AutoRefresh seconds={15} />
      </div>

      <form method="get" className="panel flex flex-wrap items-end gap-3 p-4">
        <div className="min-w-[16rem] flex-1">
          <label htmlFor="search" className="label">
            Search title or objective
          </label>
          <input
            id="search"
            name="search"
            defaultValue={filters.search}
            placeholder="scholarship, ingest, citation…"
            className="mt-1 w-full rounded-lg border border-base-700 bg-base-950 px-3 py-2 text-sm text-ink-100 placeholder:text-ink-400 focus:border-sky-500"
          />
        </div>

        <div>
          <label htmlFor="status" className="label">
            Status
          </label>
          <select
            id="status"
            name="status"
            defaultValue={filters.status}
            className="mt-1 rounded-lg border border-base-700 bg-base-950 px-3 py-2 text-sm text-ink-100 focus:border-sky-500"
          >
            {STATUSES.map((status) => (
              <option key={status} value={status}>
                {status || "any status"}
              </option>
            ))}
          </select>
        </div>

        <div>
          <label htmlFor="priority" className="label">
            Priority
          </label>
          <select
            id="priority"
            name="priority"
            defaultValue={filters.priority}
            className="mt-1 rounded-lg border border-base-700 bg-base-950 px-3 py-2 text-sm text-ink-100 focus:border-sky-500"
          >
            {PRIORITIES.map((priority) => (
              <option key={priority} value={priority}>
                {priority || "any priority"}
              </option>
            ))}
          </select>
        </div>

        <div>
          <label htmlFor="tag" className="label">
            Tag
          </label>
          <input
            id="tag"
            name="tag"
            defaultValue={filters.tag}
            placeholder="research"
            className="mt-1 w-32 rounded-lg border border-base-700 bg-base-950 px-3 py-2 text-sm text-ink-100 placeholder:text-ink-400 focus:border-sky-500"
          />
        </div>

        <button
          type="submit"
          className="rounded-lg border border-sky-500/40 bg-sky-500/10 px-4 py-2 text-sm font-medium text-sky-200 transition hover:bg-sky-500/20"
        >
          Apply
        </button>
        {(filters.status || filters.priority || filters.tag || filters.search) && (
          <Link href="/workflows" className="px-2 py-2 text-sm text-ink-400 hover:text-ink-200">
            clear
          </Link>
        )}
      </form>

      {page.items.length === 0 ? (
        <Empty
          title="No workflows match these filters"
          hint="Run `make seed` to insert realistic sample workflows, or create one with POST /api/v1/workflows."
        />
      ) : (
        <div className="grid gap-4 lg:grid-cols-2 2xl:grid-cols-3">
          {page.items.map((workflow) => (
            <WorkflowCard key={workflow.id} workflow={workflow} />
          ))}
        </div>
      )}

      {page.has_more && page.next_cursor && (
        <div className="flex justify-center pt-2">
          <Link
            href={`/workflows${buildQuery(baseFilters, { cursor: page.next_cursor })}`}
            className="rounded-lg border border-base-700 px-4 py-2 text-sm text-ink-300 transition hover:border-base-600 hover:text-ink-100"
          >
            Next page →
          </Link>
        </div>
      )}
    </div>
  );
}
