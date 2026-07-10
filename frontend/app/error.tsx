"use client";

import { useEffect } from "react";

export default function Error({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    console.error(error);
  }, [error]);

  return (
    <div className="panel border-rose-500/40 bg-rose-950/20 p-6">
      <h1 className="text-lg font-semibold text-rose-200">Something broke while rendering this page</h1>
      <p className="mt-2 text-sm text-ink-300">{error.message}</p>
      {error.digest && (
        <p className="mt-1 font-mono text-xs text-ink-400">digest: {error.digest}</p>
      )}
      <button
        type="button"
        onClick={reset}
        className="mt-5 rounded-lg border border-base-700 px-4 py-2 text-sm text-ink-200 transition hover:border-base-600"
      >
        Try again
      </button>
    </div>
  );
}
