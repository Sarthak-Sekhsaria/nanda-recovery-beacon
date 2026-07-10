/**
 * Server-side API client.
 *
 * Every call runs on the Next.js server, never in the browser. The API key lives
 * in BEACON_API_KEY and is never sent to the client. Pages that render live data
 * declare `export const dynamic = "force-dynamic"` so nothing is cached at build.
 */
import "server-only";

import type {
  Artifact,
  Checkpoint,
  CheckpointDiff,
  Page,
  RecoverableItem,
  RecoveryEvent,
  RecoveryPackage,
  Stats,
  Workflow,
} from "./types";

export const API_URL = (process.env.BEACON_API_URL ?? "http://localhost:8000").replace(/\/$/, "");
const API_KEY = process.env.BEACON_API_KEY ?? "";

/** The URL agents should call. Safe to show in the browser. */
export const PUBLIC_API_URL = (
  process.env.NEXT_PUBLIC_BEACON_PUBLIC_URL ?? API_URL
).replace(/\/$/, "");

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

function headers(): HeadersInit {
  const base: Record<string, string> = { Accept: "application/json" };
  if (API_KEY) base.Authorization = `Bearer ${API_KEY}`;
  // When the API runs in DEMO_MODE it accepts unauthenticated reads and attributes
  // them to this agent id. Harmless when a key is present.
  base["X-Agent-Id"] = "beacon-dashboard";
  return base;
}

async function get<T>(path: string, params?: Record<string, string | number | boolean | undefined>) {
  const url = new URL(`${API_URL}${path}`);
  for (const [key, value] of Object.entries(params ?? {})) {
    if (value !== undefined && value !== "") url.searchParams.set(key, String(value));
  }

  let response: Response;
  try {
    response = await fetch(url, { headers: headers(), cache: "no-store" });
  } catch (cause) {
    const detail = cause instanceof Error ? cause.message : "unknown error";
    throw new ApiError(0, "NETWORK_ERROR", `Cannot reach the API at ${API_URL}: ${detail}`);
  }

  if (!response.ok) {
    let code = `HTTP_${response.status}`;
    let message = response.statusText;
    try {
      const body = await response.json();
      code = body?.error?.code ?? code;
      message = body?.error?.message ?? message;
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(response.status, code, message);
  }

  return (await response.json()) as T;
}

export const api = {
  health: () => get<{ status: string; version: string; environment: string }>("/health"),

  stats: () => get<Stats>("/api/v1/stats"),

  workflows: (params: {
    limit?: number;
    cursor?: string;
    status?: string;
    priority?: string;
    tag?: string;
    search?: string;
    agent_id?: string;
  }) => get<Page<Workflow>>("/api/v1/workflows", params),

  workflow: (id: string) => get<Workflow>(`/api/v1/workflows/${id}`),

  recoveryPackage: (id: string) => get<RecoveryPackage>(`/api/v1/workflows/${id}/recovery-package`),

  checkpoints: (id: string, limit = 50) =>
    get<Page<Checkpoint>>(`/api/v1/workflows/${id}/checkpoints`, { limit }),

  checkpointDiff: (id: string, version: number) =>
    get<CheckpointDiff>(`/api/v1/workflows/${id}/checkpoints/${version}/diff`),

  artifacts: (id: string) => get<Page<Artifact>>(`/api/v1/workflows/${id}/artifacts`),

  events: (id: string, limit = 100) =>
    get<Page<RecoveryEvent>>(`/api/v1/workflows/${id}/events`, { limit }),

  recentEvents: (limit = 30) => get<Page<RecoveryEvent>>("/api/v1/events", { limit }),

  recoverable: (params: {
    limit?: number;
    cursor?: string;
    priority?: string;
    tag?: string;
    resumable_only?: boolean;
    min_age_seconds?: number;
  }) =>
    get<{ items: RecoverableItem[]; next_cursor: string | null; has_more: boolean }>(
      "/api/v1/recoverable-workflows",
      params,
    ),

  skillMarkdown: async (): Promise<string> => {
    const response = await fetch(`${API_URL}/skill.md`, { cache: "no-store" });
    if (!response.ok) throw new ApiError(response.status, "SKILL_MD_UNAVAILABLE", "Cannot load SKILL.md");
    return response.text();
  },
};
