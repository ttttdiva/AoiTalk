import { withBasePath } from "@/lib/base-path";
import type { components, paths } from "@/lib/api-types.gen";

type Schemas = components["schemas"];

/** Canonical AoiDots/OpenAPI projections. Keep the backend DTO as the source. */
export type AoiDotsProfile = Schemas["AoiDotsProfile"];
export type AoiDotsWorkItem = Schemas["AoiDotsWorkItem"];
export type AoiDotsWorkResponse = Schemas["AoiDotsWorkList"];
export type AoiDotsDialogue = Schemas["AgentDialogueResponse"];
export type AoiDotsPendingInput = Schemas["AgentDialogueInputDTO"];
export type AoiDotsArtifact = Schemas["ArtifactSafeRef"];
export type AoiDotsReference = Schemas["AgentDialogueRefs"];
export type AoiDotsDialogueControlPayload = Schemas["DialogueControlRequest"];

export type AoiDotsDialogueInputPayload = Schemas["InputAnswerRequest"] & {
  input_id: string;
};

type DispatchRequest = Schemas["ConversationDispatchRequest"];
export type AoiDotsDispatchPayload = Omit<
  DispatchRequest,
  "cloud_advisor_explicit" | "skip_user_persistence"
> &
  Partial<Pick<DispatchRequest, "cloud_advisor_explicit" | "skip_user_persistence">>;

type DispatchOperation = paths["/api/conversations/{session_id}/dispatch"]["post"];
export type AoiDotsDispatchResponse =
  DispatchOperation["responses"][200]["content"]["application/json"];

class AoiDotsApiError extends Error {
  status: number;
  code?: string;

  constructor(message: string, status: number, code?: string) {
    super(message);
    this.name = "AoiDotsApiError";
    this.status = status;
    this.code = code;
  }
}

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function requiredString(value: unknown, field: string): string {
  if (typeof value !== "string" || !value.trim()) {
    throw new Error(`AoiDots ${field} が不正です`);
  }
  return value.trim();
}

function parseProfile(value: unknown): AoiDotsProfile {
  const source = record(value);
  requiredString(source.root_id, "root_id");
  requiredString(source.session_id, "session_id");
  requiredString(source.agent_id, "agent_id");
  return value as AoiDotsProfile;
}

function parseWork(value: unknown): AoiDotsWorkResponse {
  const source = record(value);
  if (!Array.isArray(source.items)) {
    throw new Error("AoiDots work projectionが不正です");
  }
  return value as AoiDotsWorkResponse;
}

function parseDialogue(value: unknown): AoiDotsDialogue {
  const source = record(value);
  requiredString(source.dialogue_id, "dialogue_id");
  requiredString(source.job_status, "job_status");
  if (!source.refs || typeof source.refs !== "object") {
    throw new Error("AoiDots dialogue refsが不正です");
  }
  return value as AoiDotsDialogue;
}

/** Exported for focused adapter tests; returns canonical generated DTOs unchanged. */
export const normalizeAoiDotsProfile = parseProfile;
export const normalizeAoiDotsWork = parseWork;
export const normalizeAoiDotsDialogue = parseDialogue;

async function requestJson<T>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const response = await fetch(withBasePath(`/api/python-proxy${path}`), {
    ...init,
    credentials: "include",
    headers: {
      Accept: "application/json",
      ...(init.body ? { "Content-Type": "application/json" } : {}),
      ...init.headers,
    },
    cache: "no-store",
  });
  const body = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = record(body);
    const nested = record(detail.detail);
    throw new AoiDotsApiError(
      (typeof nested.message === "string" && nested.message.trim()) ||
        (typeof detail.message === "string" && detail.message.trim()) ||
        (typeof detail.error === "string" && detail.error.trim()) ||
        `AoiDots API failed (${response.status})`,
      response.status,
      typeof nested.code === "string"
        ? nested.code
        : typeof detail.code === "string"
          ? detail.code
          : undefined,
    );
  }
  return body as T;
}

export async function bootstrapAoiDots(
  contextProjectHintId?: string | null,
): Promise<AoiDotsProfile> {
  return parseProfile(
    await requestJson<unknown>("/aoidots/bootstrap", {
      method: "POST",
      body: JSON.stringify({
        context_project_hint_id: contextProjectHintId?.trim() || null,
      }),
    }),
  );
}

export async function listAoiDotsWork(
  rootSessionId?: string | null,
): Promise<AoiDotsWorkResponse> {
  const query = rootSessionId?.trim()
    ? `?root_session_id=${encodeURIComponent(rootSessionId.trim())}`
    : "";
  return parseWork(await requestJson<unknown>(`/aoidots/work${query}`));
}

export async function getAgentDialogue(
  dialogueId: string,
): Promise<AoiDotsDialogue> {
  return parseDialogue(
    await requestJson<unknown>(
      `/agent-dialogues/${encodeURIComponent(dialogueId)}`,
    ),
  );
}

export async function answerAgentDialogueInput(
  dialogueId: string,
  payload: AoiDotsDialogueInputPayload,
): Promise<unknown> {
  return requestJson<unknown>(
    `/agent-dialogues/${encodeURIComponent(dialogueId)}/inputs/${encodeURIComponent(payload.input_id)}/answer`,
    {
      method: "POST",
      body: JSON.stringify({
        answer: payload.answer,
        client_message_id: payload.client_message_id,
        expected_control_revision: payload.expected_control_revision,
        expected_input_revision: payload.expected_input_revision,
      }),
    },
  );
}

export async function controlAgentDialogue(
  dialogueId: string,
  payload: AoiDotsDialogueControlPayload,
): Promise<unknown> {
  return requestJson<unknown>(
    `/agent-dialogues/${encodeURIComponent(dialogueId)}/control`,
    { method: "POST", body: JSON.stringify(payload) },
  );
}

export async function dispatchAoiDotsMessage(
  sessionId: string,
  payload: AoiDotsDispatchPayload,
): Promise<AoiDotsDispatchResponse> {
  return requestJson<AoiDotsDispatchResponse>(
    `/conversations/${encodeURIComponent(sessionId)}/dispatch`,
    { method: "POST", keepalive: true, body: JSON.stringify(payload) },
  );
}

export { AoiDotsApiError };
