"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState, useTransition } from "react";

/**
 * Re-fetches the current server component tree on an interval.
 * The dashboard shows live operational state, so stale numbers are worse than a
 * small amount of traffic. Pauses while the tab is hidden.
 */
export function AutoRefresh({ seconds = 10 }: { seconds?: number }) {
  const router = useRouter();
  const [pending, startTransition] = useTransition();
  const [enabled, setEnabled] = useState(true);
  const [lastRefresh, setLastRefresh] = useState<Date | null>(null);

  useEffect(() => {
    if (!enabled) return;

    const tick = () => {
      if (document.visibilityState !== "visible") return;
      startTransition(() => {
        router.refresh();
        setLastRefresh(new Date());
      });
    };

    const id = window.setInterval(tick, seconds * 1000);
    return () => window.clearInterval(id);
  }, [enabled, router, seconds]);

  return (
    <div className="flex items-center gap-3 text-xs text-ink-400">
      <span className="flex items-center gap-1.5" aria-live="polite">
        <span
          className={`h-1.5 w-1.5 rounded-full ${
            pending ? "animate-pulse bg-sky-400" : enabled ? "bg-emerald-400" : "bg-base-500"
          }`}
          aria-hidden
        />
        {pending ? "refreshing" : lastRefresh ? `updated ${lastRefresh.toLocaleTimeString()}` : "live"}
      </span>
      <button
        type="button"
        onClick={() => setEnabled((value) => !value)}
        className="rounded border border-base-700 px-2 py-1 transition hover:border-base-600 hover:text-ink-200"
      >
        {enabled ? `auto ${seconds}s` : "paused"}
      </button>
      <button
        type="button"
        onClick={() => startTransition(() => router.refresh())}
        className="rounded border border-base-700 px-2 py-1 transition hover:border-base-600 hover:text-ink-200"
      >
        refresh
      </button>
    </div>
  );
}
