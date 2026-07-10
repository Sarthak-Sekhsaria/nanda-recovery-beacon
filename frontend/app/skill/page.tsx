import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { CodeBlock, CopyButton } from "@/components/copy-button";
import { ErrorPanel, Panel } from "@/components/ui";
import { ApiError, PUBLIC_API_URL, api } from "@/lib/api";

export const dynamic = "force-dynamic";
export const metadata = { title: "API & Skill" };

const QUICK_CALLS: { caption: string; code: string }[] = [
  {
    caption: "discover recoverable work",
    code: `curl "${PUBLIC_API_URL}/api/v1/recoverable-workflows?resumable_only=true" \\
  -H "Authorization: Bearer $BEACON_API_KEY"`,
  },
  {
    caption: "read the recovery package",
    code: `curl ${PUBLIC_API_URL}/api/v1/workflows/$WORKFLOW_ID/recovery-package \\
  -H "Authorization: Bearer $BEACON_API_KEY"`,
  },
  {
    caption: "claim it (returns the lease token once)",
    code: `curl -X POST ${PUBLIC_API_URL}/api/v1/workflows/$WORKFLOW_ID/claims \\
  -H "Authorization: Bearer $BEACON_API_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{"lease_seconds": 300}'`,
  },
  {
    caption: "resume, then keep the lease alive",
    code: `curl -X POST ${PUBLIC_API_URL}/api/v1/workflows/$WORKFLOW_ID/resume \\
  -H "Authorization: Bearer $BEACON_API_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{"lease_token": "'"$LEASE_TOKEN"'"}'`,
  },
];

export default async function SkillPage() {
  let markdown: string;
  try {
    markdown = await api.skillMarkdown();
  } catch (error) {
    const apiError = error instanceof ApiError ? error : new ApiError(0, "UNKNOWN", String(error));
    return <ErrorPanel code={apiError.code} message={apiError.message} />;
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight text-ink-100">API &amp; Skill</h1>
        <p className="mt-1 max-w-3xl text-sm text-ink-400">
          Everything an autonomous agent needs is in <code className="font-mono text-orange-200">SKILL.md</code>,
          served live at the URL below. No human explanation is required.
        </p>
      </div>

      <div className="grid gap-4 md:grid-cols-3">
        {[
          { label: "Public base URL", value: PUBLIC_API_URL },
          { label: "Agent instructions", value: `${PUBLIC_API_URL}/skill.md` },
          { label: "OpenAPI schema", value: `${PUBLIC_API_URL}/openapi.json` },
        ].map((item) => (
          <div key={item.label} className="panel p-4">
            <p className="label">{item.label}</p>
            <div className="mt-2 flex items-center gap-2">
              <a
                href={item.value}
                className="min-w-0 flex-1 truncate font-mono text-xs text-sky-300 hover:text-sky-200"
              >
                {item.value}
              </a>
              <CopyButton text={item.value} />
            </div>
          </div>
        ))}
      </div>

      <Panel
        title="The four calls that recover a workflow"
        description="Copy, paste, run. Set BEACON_API_KEY first (or omit it if the deployment runs in demo mode)."
      >
        <div className="grid gap-4 lg:grid-cols-2">
          {QUICK_CALLS.map((call) => (
            <CodeBlock key={call.caption} code={call.code} caption={call.caption} />
          ))}
        </div>
      </Panel>

      <Panel
        title="SKILL.md"
        description="Rendered from the live service. This is the exact document an agent reads."
        action={<CopyButton text={markdown} label="copy raw markdown" />}
      >
        <article className="markdown max-w-none">
          <Markdown remarkPlugins={[remarkGfm]}>{markdown}</Markdown>
        </article>
      </Panel>

      <Panel title="Reference documents" description="Linked from SKILL.md, kept in the repository.">
        <ul className="grid gap-3 sm:grid-cols-2">
          {[
            ["references/api-reference.md", "Every endpoint, parameter and field."],
            ["references/error-codes.md", "Every error code, its cause and its remedy."],
            ["references/checkpoint-schema.md", "Checkpoint fields and the full context-scoring rule table."],
            ["references/recovery-examples.md", "Worked examples: crash, race, stale write, artifact failure."],
          ].map(([path, description]) => (
            <li key={path} className="rounded-lg border border-base-700 bg-base-850/40 p-3">
              <p className="font-mono text-xs text-ink-100">{path}</p>
              <p className="mt-1 text-xs text-ink-400">{description}</p>
            </li>
          ))}
        </ul>
      </Panel>
    </div>
  );
}
