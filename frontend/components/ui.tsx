import Link from "next/link";
import type { ReactNode } from "react";

import {
  PRIORITY_CLASS,
  STATUS_CLASS,
  STATUS_LABEL,
  scoreBarClass,
  scoreClass,
} from "@/lib/format";
import type { Priority, VerificationStatus, WorkflowStatus } from "@/lib/types";

export function Panel({
  title,
  description,
  action,
  children,
  className = "",
}: {
  title?: string;
  description?: string;
  action?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`panel ${className}`}>
      {(title || action) && (
        <header className="flex items-start justify-between gap-4 border-b border-base-700/70 px-5 py-4">
          <div>
            {title && <h2 className="text-sm font-semibold text-ink-100">{title}</h2>}
            {description && <p className="mt-1 text-xs text-ink-400">{description}</p>}
          </div>
          {action}
        </header>
      )}
      <div className="p-5">{children}</div>
    </section>
  );
}

export function StatusBadge({ status }: { status: WorkflowStatus }) {
  return (
    <span className={`badge ${STATUS_CLASS[status]}`}>
      <span className="h-1.5 w-1.5 rounded-full bg-current" aria-hidden />
      {STATUS_LABEL[status]}
    </span>
  );
}

export function PriorityBadge({ priority }: { priority: Priority }) {
  return <span className={`badge ${PRIORITY_CLASS[priority]}`}>{priority}</span>;
}

export function VerificationBadge({ status }: { status: VerificationStatus }) {
  const map: Record<VerificationStatus, string> = {
    verified: "border-emerald-500/40 bg-emerald-500/10 text-emerald-300",
    failed: "border-rose-500/40 bg-rose-500/10 text-rose-300",
    unverified: "border-base-600 bg-base-800 text-ink-400",
  };
  const icon = { verified: "✓", failed: "✕", unverified: "?" }[status];
  return (
    <span className={`badge ${map[status]}`}>
      <span aria-hidden>{icon}</span>
      {status}
    </span>
  );
}

export function StatTile({
  label,
  value,
  hint,
  tone = "default",
  href,
}: {
  label: string;
  value: string | number;
  hint?: string;
  tone?: "default" | "attention" | "good" | "bad";
  href?: string;
}) {
  const toneClass = {
    default: "text-ink-100",
    attention: "text-orange-300",
    good: "text-emerald-300",
    bad: "text-rose-300",
  }[tone];

  const body = (
    <div className="panel h-full px-5 py-4 transition hover:border-base-600">
      <p className="label">{label}</p>
      <p className={`mt-2 font-mono text-3xl font-semibold tabular-nums ${toneClass}`}>{value}</p>
      {hint && <p className="mt-1 text-xs text-ink-400">{hint}</p>}
    </div>
  );

  return href ? (
    <Link href={href} className="block">
      {body}
    </Link>
  ) : (
    body
  );
}

export function ScoreBar({ score, showLabel = true }: { score: number; showLabel?: boolean }) {
  return (
    <div className="flex items-center gap-2">
      <div
        className="h-1.5 w-full overflow-hidden rounded-full bg-base-800"
        role="meter"
        aria-valuenow={score}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-label="Context completeness score"
      >
        <div className={`h-full rounded-full ${scoreBarClass(score)}`} style={{ width: `${score}%` }} />
      </div>
      {showLabel && (
        <span className={`w-10 shrink-0 text-right font-mono text-xs tabular-nums ${scoreClass(score)}`}>
          {score}
        </span>
      )}
    </div>
  );
}

export function Empty({ title, hint }: { title: string; hint?: string }) {
  return (
    <div className="flex flex-col items-center justify-center rounded-lg border border-dashed border-base-700 px-6 py-12 text-center">
      <p className="text-sm font-medium text-ink-300">{title}</p>
      {hint && <p className="mt-1 max-w-md text-xs text-ink-400">{hint}</p>}
    </div>
  );
}

export function ErrorPanel({ code, message }: { code: string; message: string }) {
  return (
    <div className="panel border-rose-500/40 bg-rose-950/20 p-6">
      <p className="text-sm font-semibold text-rose-200">Could not load data from the API</p>
      <p className="mt-2 font-mono text-xs text-rose-300/90">{code}</p>
      <p className="mt-1 text-sm text-ink-300">{message}</p>
      <ul className="mt-4 list-disc space-y-1 pl-5 text-xs text-ink-400">
        <li>
          Check that the API is running and that <code className="font-mono">BEACON_API_URL</code> points at it.
        </li>
        <li>
          If the API has <code className="font-mono">DEMO_MODE=false</code>, set{" "}
          <code className="font-mono">BEACON_API_KEY</code> for the dashboard.
        </li>
        <li>On a free hosting plan the API sleeps when idle; the first request can take ~30 seconds.</li>
      </ul>
    </div>
  );
}

export function Skeleton({ className = "" }: { className?: string }) {
  return <div className={`animate-pulse rounded bg-base-800 ${className}`} />;
}

export function Mono({ children, title }: { children: ReactNode; title?: string }) {
  return (
    <span className="font-mono text-xs text-ink-300" title={title}>
      {children}
    </span>
  );
}

export function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <p className="label">{label}</p>
      <div className="mt-1 text-sm text-ink-200">{children}</div>
    </div>
  );
}
