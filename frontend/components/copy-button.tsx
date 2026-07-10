"use client";

import { useState } from "react";

export function CopyButton({ text, label = "copy" }: { text: string; label?: string }) {
  const [copied, setCopied] = useState(false);

  return (
    <button
      type="button"
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(text);
          setCopied(true);
          window.setTimeout(() => setCopied(false), 1600);
        } catch {
          setCopied(false);
        }
      }}
      className="rounded border border-base-700 bg-base-850 px-2 py-1 font-mono text-[11px] text-ink-400 transition hover:border-base-600 hover:text-ink-200"
      aria-live="polite"
    >
      {copied ? "copied" : label}
    </button>
  );
}

export function CodeBlock({ code, caption }: { code: string; caption?: string }) {
  return (
    <figure className="overflow-hidden rounded-lg border border-base-700 bg-base-950">
      <figcaption className="flex items-center justify-between border-b border-base-700 bg-base-900/60 px-3 py-2">
        <span className="font-mono text-[11px] text-ink-400">{caption ?? "shell"}</span>
        <CopyButton text={code} />
      </figcaption>
      <pre className="overflow-x-auto p-3 text-[12px] leading-relaxed text-ink-200">
        <code>{code}</code>
      </pre>
    </figure>
  );
}
