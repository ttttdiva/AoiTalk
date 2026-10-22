import type {
  AoiDotsArtifact,
  AoiDotsBudget,
  AoiDotsDialogue,
  AoiDotsInput,
  AoiDotsProfile,
  AoiDotsRefs,
  AoiDotsWorkItem,
  AoiDotsWorkList,
} from "./types";
import type { ConversationMessage } from "../../types/api";

function record(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function requiredId(value: unknown, field: string): string {
  if (typeof value !== "string" || !value.trim()) {
    throw new Error(`AoiDots ${field} が不正です`);
  }
  return value.trim();
}

function optionalText(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

function nonNegativeInteger(value: unknown, field: string): number {
  if (typeof value !== "number" || !Number.isInteger(value) || value < 0) {
    throw new Error(`AoiDots ${field} が不正です`);
  }
  return value;
}

function positiveInteger(value: unknown, field: string): number {
  if (typeof value !== "number" || !Number.isInteger(value) || value < 1) {
    throw new Error(`AoiDots ${field} が不正です`);
  }
  return value;
}

function refs(value: unknown): AoiDotsRefs {
  const row = record(value);
  const result: AoiDotsRefs = {};
  const fields = [
    "root_session_id",
    "worker_session_id",
    "source_message_id",
    "source_human_message_id",
    "source_run_id",
    "source_human_run_id",
    "work_item_id",
    "source_human_work_item_id",
    "task_id",
    "project_id",
    "root_agent_id",
    "root_agent_revision_id",
    "worker_agent_id",
    "worker_agent_revision_id",
    "current_run_id",
    "current_work_item_id",
    "current_delivery_id",
  ] as const;
  for (const field of fields) {
    const value = optionalText(row[field]);
    if (value) result[field] = value;
  }
  return result;
}

type AoiDotsPhase = NonNullable<AoiDotsWorkItem["phase"]>;
type AoiDotsStatusSource = NonNullable<AoiDotsWorkItem["status_source"]>;
type AoiDotsUsageState = AoiDotsBudget["usage_state"];

const PHASES: ReadonlySet<AoiDotsPhase> = new Set([
  "queued",
  "running",
  "receiving_result",
  "waiting_for_input",
  "blocked",
  "completed",
  "succeeded",
  "failed",
  "cancelled",
  "expired",
] as const);

const STATUS_SOURCES: ReadonlySet<AoiDotsStatusSource> = new Set(["dialogue", "worker"]);
const USAGE_STATES: ReadonlySet<AoiDotsUsageState> = new Set([
  "known",
  "unknown",
  "cost_unknown",
  "token_unknown",
] as const);

function optionalNonNegativeInteger(value: unknown, field: string): number | null {
  if (value === null || value === undefined) return null;
  return nonNegativeInteger(value, field);
}

function budget(value: unknown): AoiDotsBudget | null {
  if (value === null || value === undefined) return null;
  const row = record(value);
  const usageState = optionalText(row.usage_state) as AoiDotsUsageState | null;
  if (!usageState || !USAGE_STATES.has(usageState)) {
    throw new Error("AoiDots budget.usage_state が不正です");
  }
  return {
    max_turns: positiveInteger(row.max_turns, "budget.max_turns"),
    turn_count: nonNegativeInteger(row.turn_count, "budget.turn_count"),
    max_depth: nonNegativeInteger(row.max_depth, "budget.max_depth"),
    max_attempts: positiveInteger(row.max_attempts, "budget.max_attempts"),
    attempt_count: nonNegativeInteger(row.attempt_count, "budget.attempt_count"),
    max_tokens: optionalNonNegativeInteger(row.max_tokens, "budget.max_tokens"),
    total_tokens: nonNegativeInteger(row.total_tokens, "budget.total_tokens"),
    reserved_tokens: nonNegativeInteger(row.reserved_tokens, "budget.reserved_tokens"),
    remaining_tokens: optionalNonNegativeInteger(row.remaining_tokens, "budget.remaining_tokens"),
    max_cost_micros: optionalNonNegativeInteger(row.max_cost_micros, "budget.max_cost_micros"),
    cost_micros: optionalNonNegativeInteger(row.cost_micros, "budget.cost_micros"),
    reserved_cost_micros: nonNegativeInteger(row.reserved_cost_micros, "budget.reserved_cost_micros"),
    cost_currency: optionalText(row.cost_currency),
    usage_state: usageState,
  };
}

function optionalPhase(value: unknown): AoiDotsWorkItem["phase"] {
  if (value === null || value === undefined) return null;
  if (typeof value !== "string" || !PHASES.has(value as AoiDotsPhase)) {
    throw new Error("AoiDots phase が不正です");
  }
  return value as AoiDotsWorkItem["phase"];
}

function optionalStatusSource(value: unknown): AoiDotsWorkItem["status_source"] {
  if (value === null || value === undefined) return null;
  if (typeof value !== "string" || !STATUS_SOURCES.has(value as AoiDotsStatusSource)) {
    throw new Error("AoiDots status_source が不正です");
  }
  return value as AoiDotsWorkItem["status_source"];
}

function input(value: unknown, fallbackDialogueId?: string | null): AoiDotsInput {
  const row = record(value);
  const id = requiredId(row.id, "input.id");
  const state = row.state;
  if (state !== "pending" && state !== "answered" && state !== "invalidated" && state !== "blocked" && state !== "cancelled" && state !== "expired") {
    throw new Error("AoiDots input.state が不正です");
  }
  return {
    id,
    dialogue_id: optionalText(row.dialogue_id) ?? fallbackDialogueId,
    question_message_id: optionalText(row.question_message_id),
    source_run_id: optionalText(row.source_run_id),
    state,
    revision: nonNegativeInteger(row.revision, "input.revision"),
    expected_input_revision: positiveInteger(row.expected_input_revision, "input.expected_input_revision"),
    expected_control_revision: nonNegativeInteger(row.expected_control_revision, "input.expected_control_revision"),
    question: optionalText(row.question),
    choices: Array.isArray(row.choices)
      ? row.choices.filter((choice): choice is string => typeof choice === "string")
      : [],
    input_type: optionalText(row.input_type),
    allow_multiple: row.allow_multiple === true,
    allow_free_text: row.allow_free_text !== false,
    answer_message_id: optionalText(row.answer_message_id),
    deadline: optionalText(row.deadline),
  };
}

function artifact(value: unknown): AoiDotsArtifact {
  const row = record(value);
  return {
    id: optionalText(row.id),
    artifact_ref: optionalText(row.artifact_ref),
    phase: optionalText(row.phase),
    delivery_id: optionalText(row.delivery_id),
    message_id: optionalText(row.message_id),
    created_at: optionalText(row.created_at),
  };
}

function workItem(value: unknown): AoiDotsWorkItem {
  const row = record(value);
  const dialogueId = requiredId(row.dialogue_id, "dialogue_id");
  if (!Array.isArray(row.pending_inputs) || !Array.isArray(row.artifacts)) {
    throw new Error("AoiDots work projection is incomplete");
  }
  const rawInputs = row.pending_inputs;
  return {
    dialogue_id: dialogueId,
    job_status: requiredId(row.job_status, "job_status"),
    human_status: optionalText(row.human_status),
    title: optionalText(row.title),
    label: optionalText(row.label),
    root_agent_name: optionalText(row.root_agent_name),
    worker_agent_name: optionalText(row.worker_agent_name),
    status_source: optionalStatusSource(row.status_source),
    current_run_status: optionalText(row.current_run_status),
    current_work_status: optionalText(row.current_work_status),
    current_delivery_state: optionalText(row.current_delivery_state),
    phase: optionalPhase(row.phase),
    budget: budget(row.budget),
    allowed_actions: Array.isArray(row.allowed_actions)
      ? row.allowed_actions.filter(
          (action): action is "stop" | "resume" | "rework" | "answer" =>
            action === "stop" || action === "resume" || action === "rework" || action === "answer",
        )
      : [],
    refs: refs(row.refs),
    control_revision: nonNegativeInteger(row.control_revision, "control_revision"),
    pending_inputs: rawInputs.map((item) => input(item, dialogueId)),
    artifacts: row.artifacts.map(artifact),
    safe_error: optionalText(row.safe_error),
    deadline: optionalText(row.deadline),
    created_at: optionalText(row.created_at),
    updated_at: optionalText(row.updated_at),
  };
}

export function parseAoiDotsProfile(value: unknown): AoiDotsProfile {
  const row = record(value);
  return {
    root_id: requiredId(row.root_id ?? row.rootId, "root_id"),
    session_id: requiredId(row.session_id ?? row.sessionId, "session_id"),
    agent_id: requiredId(row.agent_id ?? row.agentId, "agent_id"),
    agent_name: optionalText(row.agent_name ?? row.agentName) ?? "AoiDots",
    state: optionalText(row.state) ?? "active",
    pending_input_count:
      typeof row.pending_input_count === "number" && row.pending_input_count >= 0
        ? Math.floor(row.pending_input_count)
        : 0,
  };
}

export function parseAoiDotsWorkList(value: unknown): AoiDotsWorkList {
  const row = record(value);
  if (!Array.isArray(row.items)) throw new Error("AoiDots work list is unavailable");
  return {
    items: row.items.map(workItem),
  };
}

export function parseAoiDotsDialogue(value: unknown): AoiDotsDialogue {
  const row = record(value);
  const projection = workItem(row);
  return {
    ...projection,
    messages: Array.isArray(row.messages) ? (row.messages as ConversationMessage[]) : [],
  };
}
