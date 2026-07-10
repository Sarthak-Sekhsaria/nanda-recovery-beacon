export type WorkflowStatus =
  | "active"
  | "suspected_failed"
  | "recoverable"
  | "claimed"
  | "completed"
  | "cancelled"
  | "dead_letter";

export type Priority = "low" | "normal" | "high" | "critical";
export type VerificationStatus = "unverified" | "verified" | "failed";

export interface Workflow {
  id: string;
  title: string;
  objective: string;
  status: WorkflowStatus;
  priority: Priority;
  failure_policy: "recover" | "dead_letter";
  creator_agent_id: string;
  current_agent_id: string | null;
  heartbeat_timeout_seconds: number;
  last_heartbeat_at: string;
  heartbeat_age_seconds: number;
  checkpoint_count: number;
  current_checkpoint_version: number;
  latest_checkpoint_id: string | null;
  lease_generation: number;
  recovery_count: number;
  max_recoveries: number;
  tags: string[];
  metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
  failed_at: string | null;
  recovered_at: string | null;
  completed_at: string | null;
}

export interface Decision {
  decision: string;
  reason?: string | null;
  made_at?: string | null;
}

export interface Checkpoint {
  id: string;
  workflow_id: string;
  version: number;
  parent_version: number | null;
  objective: string;
  completed_steps: string[];
  remaining_steps: string[];
  decisions: Decision[];
  next_action: string | null;
  context_summary: string | null;
  variables: Record<string, unknown>;
  producing_agent_id: string;
  lease_generation: number;
  schema_version: string;
  content_checksum: string;
  created_at: string;
}

export interface Artifact {
  id: string;
  workflow_id: string;
  name: string;
  uri: string | null;
  storage_key: string | null;
  sha256: string | null;
  content_type: string | null;
  size_bytes: number | null;
  description: string | null;
  checkpoint_version: number | null;
  verification_status: VerificationStatus;
  verification_error: string | null;
  verified_at: string | null;
  produced_by_agent_id: string;
  created_at: string;
}

export interface Claim {
  id: string;
  workflow_id: string;
  agent_id: string;
  status: "active" | "released" | "expired" | "completed";
  lease_generation: number;
  created_at: string;
  expires_at: string;
  last_renewed_at: string | null;
  released_at: string | null;
  release_reason: string | null;
  renewal_count: number;
  lease_token_prefix: string;
}

export interface Issue {
  code: string;
  severity: "blocking" | "warning";
  message: string;
  weight: number;
  field: string | null;
  details: Record<string, unknown>;
}

export interface ContextEvaluation {
  resumable: boolean;
  score: number;
  blocking_issues: Issue[];
  warnings: Issue[];
  recommended_repairs: string[];
  evaluated_checkpoint_version: number | null;
  min_score_for_resume: number;
}

export interface RecoveryEvent {
  id: string;
  workflow_id: string;
  event_type: string;
  actor_agent_id: string | null;
  request_id: string | null;
  checkpoint_version: number | null;
  lease_generation: number | null;
  metadata: Record<string, unknown>;
  created_at: string;
}

export interface RecoveryPackage {
  workflow: Workflow;
  latest_checkpoint: Checkpoint | null;
  context_evaluation: ContextEvaluation;
  artifacts: Artifact[];
  active_claim: Claim | null;
  resume_instructions: {
    next_action: string | null;
    must_preserve: string[];
    must_not_repeat: string[];
    completion_requirements: string[];
    claim_first: boolean;
    expected_parent_version: number;
  };
  checkpoint_history: number[];
  recent_events: RecoveryEvent[];
}

export interface RecoverableItem {
  workflow: Workflow;
  context_score: number;
  resumable: boolean;
  blocking_issue_codes: string[];
  seconds_since_recoverable: number;
  latest_checkpoint_version: number;
}

export interface Stats {
  status_counts: Record<WorkflowStatus, number>;
  total_workflows: number;
  expired_claims: number;
  active_claims: number;
  average_recovery_seconds: number | null;
  context_score_distribution: Record<string, number>;
  checkpoints_total: number;
  events_total: number;
  recent_events: RecoveryEvent[];
  generated_at: string;
}

export interface Page<T> {
  items: T[];
  next_cursor: string | null;
  has_more: boolean;
}

export interface CheckpointDiff {
  workflow_id: string;
  version: number;
  parent_version: number | null;
  diff: {
    steps_completed_since_parent: string[];
    steps_removed_from_remaining: string[];
    decisions_added: Decision[];
    next_action_changed: boolean;
    objective_changed: boolean;
  };
}
