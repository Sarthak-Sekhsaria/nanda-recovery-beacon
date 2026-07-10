import Link from "next/link";

import { EVENT_LABEL, EVENT_TONE, absoluteTime, relativeTime, shortId } from "@/lib/format";
import type { RecoveryEvent } from "@/lib/types";

import { Empty } from "./ui";

function summarise(event: RecoveryEvent): string | null {
  const meta = event.metadata ?? {};
  const parts: string[] = [];

  if (typeof meta.trigger === "string") parts.push(`trigger: ${meta.trigger}`);
  if (typeof meta.reason === "string") parts.push(meta.reason);
  if (typeof meta.context_score === "number") parts.push(`context score ${meta.context_score}`);
  if (typeof meta.submitted_parent_version === "number")
    parts.push(`submitted parent_version ${meta.submitted_parent_version}`);
  if (typeof meta.lease_seconds === "number") parts.push(`lease ${meta.lease_seconds}s`);
  if (typeof meta.remaining_steps === "number") parts.push(`${meta.remaining_steps} steps remaining`);
  if (typeof meta.artifact === "string") parts.push(meta.artifact);
  if (typeof meta.summary === "string" && meta.summary) parts.push(meta.summary);

  return parts.length ? parts.join(" · ") : null;
}

export function EventTimeline({
  events,
  showWorkflow = false,
}: {
  events: RecoveryEvent[];
  showWorkflow?: boolean;
}) {
  if (events.length === 0) {
    return <Empty title="No events yet" hint="Events appear as soon as an agent touches a workflow." />;
  }

  return (
    <ol className="relative space-y-0">
      {events.map((event, index) => (
        <li key={event.id} className="relative flex gap-3 pb-4 last:pb-0">
          <div className="flex flex-col items-center">
            <span
              className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${EVENT_TONE[event.event_type] ?? "bg-base-500"}`}
              aria-hidden
            />
            {index < events.length - 1 && <span className="mt-1 w-px flex-1 bg-base-700" aria-hidden />}
          </div>

          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
              <span className="text-sm text-ink-100">
                {EVENT_LABEL[event.event_type] ?? event.event_type}
              </span>
              {event.actor_agent_id && (
                <span className="font-mono text-xs text-ink-400">by {event.actor_agent_id}</span>
              )}
              {event.checkpoint_version !== null && (
                <span className="font-mono text-xs text-ink-400">v{event.checkpoint_version}</span>
              )}
              {event.lease_generation !== null && event.lease_generation > 0 && (
                <span className="font-mono text-xs text-ink-400">gen {event.lease_generation}</span>
              )}
              <time
                className="ml-auto shrink-0 font-mono text-[11px] text-ink-400"
                dateTime={event.created_at}
                title={absoluteTime(event.created_at)}
              >
                {relativeTime(event.created_at)}
              </time>
            </div>

            {summarise(event) && (
              <p className="mt-0.5 truncate text-xs text-ink-400">{summarise(event)}</p>
            )}

            {showWorkflow && (
              <Link
                href={`/workflows/${event.workflow_id}`}
                className="mt-0.5 inline-block font-mono text-[11px] text-sky-300/80 hover:text-sky-300"
              >
                {shortId(event.workflow_id)}
              </Link>
            )}
          </div>
        </li>
      ))}
    </ol>
  );
}
