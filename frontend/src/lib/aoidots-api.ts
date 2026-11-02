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
export type AoiDotsStaleControlDeliveryTerminalizationRequest =
  Schemas["StaleControlDeliveryTerminalizationRequest"];
export type AoiDotsStaleControlDeliveryTerminalizationResponse =
  Schemas["StaleControlDeliveryTerminalizationResponse"];

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
type HistoryFenceRequest = Schemas["InteractiveHistoryFenceRequest"];

export type AoiDotsHistoryFenceCandidate = {
  workItemId: string;
  safeErrorCode: string | null;
};

export type AoiDotsHistoryFenceDeliveryBlocker = {
  id: string;
  kind: string;
  state: string;
};

export type AoiDotsHistoryFenceReview = {
  workItemId: string;
  safeErrorCode: string | null;
  status: "dry_run_ok" | "already_fenced" | "already_releasable" | "unavailable";
  stateDigest: string | null;
  runStatus: string | null;
  childWorkStates: string[];
  deliveryBlockers?: AoiDotsHistoryFenceDeliveryBlocker[];
  errorCode: string | null;
  errorStatus: number | null;
};

class AoiDotsApiError extends Error {
  status: number;
  code?: string;
  details?: Record<string, unknown>;

  constructor(
    message: string,
    status: number,
    code?: string,
    details?: Record<string, unknown>,
  ) {
    super(message);
    this.name = "AoiDotsApiError";
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

function errorCodeFromBody(body: unknown): string | undefined {
  const source = record(body);
  const detail = source.detail;
  const detailRecord = record(detail);
  for (const candidate of [
    detailRecord.code,
    detailRecord.error,
    detail,
    source.code,
    source.error,
  ]) {
    if (typeof candidate === "string" && /^[a-z0-9_]+$/.test(candidate)) {
      return candidate;
    }
  }
  return undefined;
}

export function aoiDotsErrorCode(error: unknown): string | null {
  if (error instanceof AoiDotsApiError && error.code) return error.code;
  if (typeof error === "string" && /^[a-z0-9_]+$/.test(error)) return error;
  if (error && typeof error === "object") {
    const source = record(error);
    const nested = [source.data, source.payload, source.error, source.detail]
      .map(record);
    for (const candidate of [source.code, source.error_code, source.error, source.detail, source.message,
      ...nested.flatMap((item) => [
        item.code, item.error_code, item.error, item.detail, item.message,
      ])]) {
      if (typeof candidate === "string" && /^[a-z0-9_]+$/.test(candidate)) {
        return candidate;
      }
    }
  }
  if (!(error instanceof Error)) return null;
  const bodyStart = error.message.indexOf("{");
  if (bodyStart < 0) return null;
  try {
    return errorCodeFromBody(JSON.parse(error.message.slice(bodyStart))) ?? null;
  } catch {
    return null;
  }
}

export function aoiDotsHistoryFenceDeliveryBlockers(
  error: unknown,
): AoiDotsHistoryFenceDeliveryBlocker[] {
  let details: Record<string, unknown> = {};
  if (error instanceof AoiDotsApiError) {
    details = error.details ?? {};
  } else if (error && typeof error === "object") {
    const source = record(error);
    details = record(source.details);
    if (Object.keys(details).length === 0) details = record(source.detail);
    if (Object.keys(details).length === 0) {
      details = record(record(source.data).detail);
    }
  }
  const raw = details.delivery_blockers;
  if (!Array.isArray(raw)) return [];
  return raw.flatMap((item): AoiDotsHistoryFenceDeliveryBlocker[] => {
    const blocker = record(item);
    if (
      typeof blocker.id !== "string" || !blocker.id.trim() ||
      typeof blocker.kind !== "string" || !blocker.kind.trim() ||
      typeof blocker.state !== "string" || !blocker.state.trim()
    ) {
      return [];
    }
    return [{
      id: blocker.id.trim().slice(0, 64),
      kind: blocker.kind.trim().slice(0, 32),
      state: blocker.state.trim().slice(0, 32),
    }];
  }).slice(0, 50);
}

export function aoiDotsHistoryFenceErrorMessage(
  error: unknown,
  fallback = "安全確認に失敗しました。",
): string {
  const status =
    error && typeof error === "object" && "status" in error
      ? Number((error as { status?: unknown }).status)
      : null;
  const code = aoiDotsErrorCode(error);
  const known: Record<string, string> = {
    interactive_session_busy: "前回の実行結果を確認できないため、この相談は停止中です。前回の依頼は再送していません。",
    history_fence_auth_required: "ログイン状態を確認してから再度お試しください。",
    history_fence_ack_required: "確認内容が一致しません。画面を更新してやり直してください。",
    history_fence_already_applied: "このWorkの履歴区切りは既に適用されています。最新状態を確認してください。",
    history_fence_already_releasable: "このWorkは既に安全な再開条件を満たしています。最新状態を確認してください。",
    history_fence_binding_mismatch: "Workと相談の結び付きが確認できません。送信は停止したままです。",
    history_fence_busy: "状態が同時に更新されました。自動再試行せず、最新状態を確認してください。",
    history_fence_child_pending: "未完了の内部処理があるため履歴を区切れません。表示された delivery／Work の状態を確認してください。",
    history_fence_forbidden: "このWorkを確認する権限がありません。",
    history_fence_idempotency_conflict: "確認キーと保存済み状態が一致しません。最新状態を確認してください。",
    history_fence_idempotency_required: "確認キーを作成できませんでした。画面を更新してください。",
    history_fence_interaction_pending: "保留中の操作があるため履歴を区切れません。操作の状態を確認してください。",
    history_fence_invalid_work_item_id: "Workの識別情報が不正です。画面を更新してください。",
    history_fence_live_lease: "実行または実行リースが残っています。完了後に再確認してください。",
    history_fence_live_state_unavailable: "実行中かどうかを確認できません。安全確認サービスの一時障害です。時間をおいて再確認してください。",
    history_fence_not_found: "対象Workを確認できません。最新状態を読み直してください。",
    history_fence_not_uncertain: "対象Workは不明状態ではありません。最新状態を読み直してください。",
    history_fence_run_not_terminal: "Runの終了を確認できません。完了後に再確認してください。",
    history_fence_snapshot_changed: "確認後に状態が変わりました。自動再試行せず、要約を読み直してください。",
    history_fence_state_digest_required: "最新の確認結果が必要です。要約を読み直してください。",
    history_fence_transport_unknown: "送信経路の終了を確認できません。履歴は区切れません。",
    history_fence_unsupported_origin: "このWorkは安全な履歴区切りの対象ではありません。追加確認が必要です。",
  };
  if (code && known[code]) return known[code];
  if (status === 503) {
    return "安全確認サービスが一時的に利用できません。時間をおいて再確認してください。";
  }
  if (status === 409) {
    return "状態が変わったか実行が継続中です。自動再試行せず、最新状態を再確認してください。";
  }
  if (status === 403 || status === 404) {
    return "対象Workを確認できません。所有者と最新状態を確認してください。";
  }
  return fallback;
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
      errorCodeFromBody(body),
      nested,
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

export async function listAoiDotsUncertainInteractiveWork(
  rootSessionId: string,
  agentId?: string | null,
): Promise<AoiDotsHistoryFenceCandidate[]> {
  const query = new URLSearchParams({ state: "uncertain", limit: "500" });
  if (agentId?.trim()) query.set("agent_id", agentId.trim());
  const payload = record(
    await requestJson<unknown>(`/agent-work?${query.toString()}`),
  );
  if (!Array.isArray(payload.items)) {
    throw new Error("不明状態のWork一覧を読み取れませんでした。");
  }
  return payload.items.flatMap((value): AoiDotsHistoryFenceCandidate[] => {
    const item = record(value);
    if (
      item.state !== "uncertain" ||
      item.conversation_session_id !== rootSessionId ||
      typeof item.id !== "string" ||
      !item.id.trim()
    ) {
      return [];
    }
    return [{
      workItemId: item.id.trim(),
      safeErrorCode:
        typeof item.safe_error_code === "string" ? item.safe_error_code : null,
    }];
  });
}

function parseHistoryFenceReview(
  workItemId: string,
  safeErrorCode: string | null,
  value: unknown,
): AoiDotsHistoryFenceReview {
  const response = record(value);
  const status = response.status;
  if (status !== "dry_run_ok" && status !== "already_fenced") {
    throw new Error("履歴区切りの確認結果が不正です。");
  }
  const evidence = record(response.evidence);
  const snapshot = record(evidence.snapshot);
  const run = record(snapshot.run);
  const summary = record(evidence.child_summary);
  const children = Array.isArray(summary.work_items) ? summary.work_items : [];
  return {
    workItemId,
    safeErrorCode,
    status,
    stateDigest:
      typeof response.state_digest === "string" &&
      /^[0-9a-f]{64}$/.test(response.state_digest)
        ? response.state_digest
        : null,
    runStatus: typeof run.status === "string" ? run.status : null,
    childWorkStates: children.flatMap((child) => {
      const state = record(child).state;
      return typeof state === "string" ? [state] : [];
    }),
    deliveryBlockers: [],
    errorCode: null,
    errorStatus: null,
  };
}

function historyFencePayload(
  sessionId: string,
  workItemId: string,
  dryRun: boolean,
  expectedStateDigest?: string,
): HistoryFenceRequest {
  return {
    session_id: sessionId,
    idempotency_key: `aoidots-history-fence:${workItemId}`,
    acknowledgment: "provider_outcome_unknown_no_replay",
    dry_run: dryRun,
    ...(expectedStateDigest ? { expected_state_digest: expectedStateDigest } : {}),
  };
}

export async function previewAoiDotsHistoryFence(
  candidate: AoiDotsHistoryFenceCandidate,
  sessionId: string,
): Promise<AoiDotsHistoryFenceReview> {
  const payload = historyFencePayload(sessionId, candidate.workItemId, true);
  const value = await requestJson<unknown>(
    `/agent-work/${encodeURIComponent(candidate.workItemId)}/history-fence`,
    { method: "POST", body: JSON.stringify(payload) },
  );
  return parseHistoryFenceReview(candidate.workItemId, candidate.safeErrorCode, value);
}

export async function applyAoiDotsHistoryFence(
  review: AoiDotsHistoryFenceReview,
  sessionId: string,
): Promise<{ status: string }> {
  if (review.status !== "dry_run_ok" || !review.stateDigest) {
    throw new Error("最新の確認結果がないため適用できません。");
  }
  const payload = historyFencePayload(
    sessionId,
    review.workItemId,
    false,
    review.stateDigest,
  );
  const result = record(await requestJson<unknown>(
    `/agent-work/${encodeURIComponent(review.workItemId)}/history-fence`,
    { method: "POST", body: JSON.stringify(payload) },
  ));
  if (typeof result.status !== "string") {
    throw new Error("履歴区切りの結果を確認できませんでした。");
  }
  return { status: result.status };
}

export async function terminalizeAoiDotsStaleControlDelivery(
  dialogueId: string,
  deliveryId: string,
  payload: AoiDotsStaleControlDeliveryTerminalizationRequest,
): Promise<AoiDotsStaleControlDeliveryTerminalizationResponse> {
  const result = await requestJson<AoiDotsStaleControlDeliveryTerminalizationResponse>(
    `/agent-dialogues/${encodeURIComponent(dialogueId)}/deliveries/${encodeURIComponent(deliveryId)}/terminalize-stale-control`,
    { method: "POST", body: JSON.stringify(payload) },
  );
  return result;
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
