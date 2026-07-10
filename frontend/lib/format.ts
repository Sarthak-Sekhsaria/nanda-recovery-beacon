import type { Priority, WorkflowStatus } from "./types";

export function relativeTime(iso: string | null | undefined): string {
  if (!iso) return "never";
  const seconds = (Date.now() - new Date(iso).getTime()) / 1000;
  if (seconds < 0) return "in the future";
  return `${duration(seconds)} ago`;
}

export function duration(seconds: number): string {
  if (seconds < 1) return "just now";
  if (seconds < 60) return `${Math.floor(seconds)}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${Math.floor(seconds % 60)}s`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`;
  return `${Math.floor(seconds / 86400)}d ${Math.floor((seconds % 86400) / 3600)}h`;
}

export function absoluteTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  return new Date(iso).toISOString().replace("T", " ").replace(/\.\d+Z$/, "Z");
}

export const STATUS_LABEL: Record<WorkflowStatus, string> = {
  active: "Active",
  suspected_failed: "Suspected failed",
  recoverable: "Recoverable",
  claimed: "Claimed",
  completed: "Completed",
  cancelled: "Cancelled",
  dead_letter: "Dead letter",
};

/**
 * Status colours. Deliberately not red/green only: `recoverable` is the state that
 * demands attention, so it gets the warm accent.
 */
export const STATUS_CLASS: Record<WorkflowStatus, string> = {
  active: "border-sky-500/40 bg-sky-500/10 text-sky-300",
  suspected_failed: "border-amber-500/40 bg-amber-500/10 text-amber-300",
  recoverable: "border-orange-500/50 bg-orange-500/15 text-orange-300",
  claimed: "border-violet-500/40 bg-violet-500/10 text-violet-300",
  completed: "border-emerald-500/40 bg-emerald-500/10 text-emerald-300",
  cancelled: "border-slate-500/40 bg-slate-500/10 text-slate-300",
  dead_letter: "border-rose-500/40 bg-rose-500/10 text-rose-300",
};

export const PRIORITY_CLASS: Record<Priority, string> = {
  critical: "border-rose-500/40 bg-rose-500/10 text-rose-300",
  high: "border-orange-500/40 bg-orange-500/10 text-orange-300",
  normal: "border-base-600 bg-base-800 text-ink-300",
  low: "border-base-700 bg-base-850 text-ink-400",
};

export function scoreClass(score: number): string {
  if (score >= 80) return "text-emerald-300";
  if (score >= 50) return "text-amber-300";
  return "text-rose-300";
}

export function scoreBarClass(score: number): string {
  if (score >= 80) return "bg-emerald-400";
  if (score >= 50) return "bg-amber-400";
  return "bg-rose-400";
}

export function heartbeatHealth(
  ageSeconds: number,
  timeoutSeconds: number,
): { label: string; className: string } {
  const ratio = ageSeconds / Math.max(1, timeoutSeconds);
  if (ratio < 0.5) return { label: "healthy", className: "text-emerald-300" };
  if (ratio < 1) return { label: "late", className: "text-amber-300" };
  return { label: "overdue", className: "text-rose-300" };
}

export const EVENT_LABEL: Record<string, string> = {
  workflow_created: "Workflow created",
  heartbeat_received: "Heartbeat",
  checkpoint_created: "Checkpoint written",
  failure_suspected: "Failure suspected",
  workflow_made_recoverable: "Made recoverable",
  claim_acquired: "Claim acquired",
  claim_renewed: "Lease renewed",
  claim_expired: "Lease expired",
  claim_released: "Claim released",
  workflow_resumed: "Workflow resumed",
  stale_update_rejected: "Stale update rejected",
  workflow_completed: "Workflow completed",
  workflow_cancelled: "Workflow cancelled",
  workflow_dead_lettered: "Moved to dead letter",
  artifact_registered: "Artifact registered",
  artifact_verification_failed: "Artifact verification failed",
  explicit_failure_reported: "Failure reported",
};

export const EVENT_TONE: Record<string, string> = {
  workflow_created: "bg-sky-400",
  heartbeat_received: "bg-base-500",
  checkpoint_created: "bg-sky-400",
  failure_suspected: "bg-amber-400",
  workflow_made_recoverable: "bg-orange-400",
  claim_acquired: "bg-violet-400",
  claim_renewed: "bg-violet-300",
  claim_expired: "bg-rose-400",
  claim_released: "bg-amber-400",
  workflow_resumed: "bg-emerald-400",
  stale_update_rejected: "bg-rose-400",
  workflow_completed: "bg-emerald-400",
  workflow_cancelled: "bg-slate-400",
  workflow_dead_lettered: "bg-rose-500",
  artifact_registered: "bg-sky-300",
  artifact_verification_failed: "bg-rose-400",
  explicit_failure_reported: "bg-orange-400",
};

export function shortId(id: string): string {
  return id.slice(0, 8);
}

export function bytes(size: number | null): string {
  if (size === null) return "—";
  if (size < 1024) return `${size} B`;
  if (size < 1024 ** 2) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / 1024 ** 2).toFixed(1)} MB`;
}
