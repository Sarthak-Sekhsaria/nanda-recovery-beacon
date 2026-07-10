import Link from "next/link";

export default function NotFound() {
  return (
    <div className="flex min-h-[50vh] flex-col items-center justify-center text-center">
      <p className="font-mono text-sm text-orange-300">404 · NOT_FOUND</p>
      <h1 className="mt-3 text-2xl font-semibold text-ink-100">That workflow does not exist</h1>
      <p className="mt-2 max-w-md text-sm text-ink-400">
        It may have been created against a different deployment, or the id is wrong. Workflows are never
        deleted — completed and cancelled ones stay readable forever.
      </p>
      <div className="mt-6 flex gap-3">
        <Link
          href="/workflows"
          className="rounded-lg border border-base-700 px-4 py-2 text-sm text-ink-200 transition hover:border-base-600"
        >
          Browse workflows
        </Link>
        <Link
          href="/"
          className="rounded-lg border border-sky-500/40 bg-sky-500/10 px-4 py-2 text-sm text-sky-200 transition hover:bg-sky-500/20"
        >
          Overview
        </Link>
      </div>
    </div>
  );
}
