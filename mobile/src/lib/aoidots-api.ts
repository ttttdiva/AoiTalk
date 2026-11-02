/** AoiDots uses the server-owned ordinary Agent root and normal Chat session. */
import { CHAT_TIMEOUT } from "../constants/config";
import { fetchApi, fetchApiAtServerFingerprint, getConfiguredApiServerFingerprint } from "./api-client";
import type { AoiDotsCommand, AoiDotsDialogue, AoiDotsHistoryFenceCandidate, AoiDotsHistoryFenceReview, AoiDotsProfile, AoiDotsWorkList } from "../features/aoidots/types";
import { parseAoiDotsDialogue, parseAoiDotsProfile, parseAoiDotsWorkList } from "../features/aoidots/validation";
import { writeAoiDotsRootPointer } from "../features/aoidots/root-pointer";
import { getToken, getTokenAuthScope } from "./auth";
import type { paths } from "../types/api-types.gen";

type BootstrapOperation = paths["/api/aoidots/bootstrap"]["post"];
type BootstrapRequest = NonNullable<BootstrapOperation["requestBody"]>["content"]["application/json"];
type BootstrapResponse = BootstrapOperation["responses"][200]["content"]["application/json"];
type WorkOperation = paths["/api/aoidots/work"]["get"];
type WorkResponse = WorkOperation["responses"][200]["content"]["application/json"];
type DialogueOperation = paths["/api/agent-dialogues/{dialogue_id}"]["get"];
type DialogueResponse = DialogueOperation["responses"][200]["content"]["application/json"];
type AnswerOperation = paths["/api/agent-dialogues/{dialogue_id}/inputs/{input_id}/answer"]["post"];
type AnswerRequest = NonNullable<AnswerOperation["requestBody"]>["content"]["application/json"];
type ControlOperation = paths["/api/agent-dialogues/{dialogue_id}/control"]["post"];
type ControlRequest = NonNullable<ControlOperation["requestBody"]>["content"]["application/json"];
type HistoryFenceOperation = paths["/api/agent-work/{work_item_id}/history-fence"]["post"];
type HistoryFenceRequest = NonNullable<HistoryFenceOperation["requestBody"]>["content"]["application/json"];

function record(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function errorStatus(error: unknown): number | null {
  if (!error || typeof error !== "object" || !("status" in error)) return null;
  const status = Number((error as { status?: unknown }).status);
  return Number.isFinite(status) ? status : null;
}

export function aoiDotsErrorCode(error: unknown): string | null {
  if (typeof error === "string" && /^[a-z0-9_]+$/.test(error)) return error;
  if (error && typeof error === "object") {
    const source = record(error);
    for (const value of [source.code, source.error_code, source.error, source.detail]) {
      if (typeof value === "string" && /^[a-z0-9_]+$/.test(value)) return value;
    }
    if (typeof source.responseBody === "string") {
      try {
        const body = record(JSON.parse(source.responseBody));
        const detail = body.detail;
        const nested = record(detail);
        for (const value of [nested.code, nested.error, detail, body.code, body.error]) {
          if (typeof value === "string" && /^[a-z0-9_]+$/.test(value)) return value;
        }
      } catch {
        // Some API wrappers retain the bounded response body in Error.message.
      }
    }
  }
  if (error instanceof Error) {
    const bodyStart = error.message.indexOf("{");
    if (bodyStart >= 0) {
      try {
        return aoiDotsErrorCode(JSON.parse(error.message.slice(bodyStart)));
      } catch {
        return null;
      }
    }
  }
  return null;
}

export function aoiDotsHistoryFenceErrorMessage(
  error: unknown,
  fallback = "安全確認に失敗しました。",
): string {
  const code = aoiDotsErrorCode(error);
  const status = errorStatus(error);
  const known: Record<string, string> = {
    interactive_session_busy: "前回の実行結果を確認できないため、この相談は停止中です。前回の依頼は再送していません。",
    history_fence_ack_required: "確認内容が一致しません。画面を更新してやり直してください。",
    history_fence_already_applied: "このWorkの履歴区切りは既に適用されています。最新状態を確認してください。",
    history_fence_already_releasable: "このWorkは既に安全な再開条件を満たしています。最新状態を確認してください。",
    history_fence_auth_required: "ログイン状態を確認してから再度お試しください。",
    history_fence_binding_mismatch: "Workと相談の結び付きが確認できません。送信は停止したままです。",
    history_fence_busy: "状態が同時に更新されました。自動再試行せず、最新状態を確認してください。",
    history_fence_child_pending: "未完了の子Workがあるため履歴を区切れません。子Workの状態を確認してください。",
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
  if (status === 503) return "安全確認サービスが一時的に利用できません。時間をおいて再確認してください。";
  if (status === 409) return "状態が変わったか実行が継続中です。自動再試行せず、最新状態を再確認してください。";
  if (status === 403 || status === 404) return "対象Workを確認できません。所有者と最新状態を確認してください。";
  return fallback;
}

function historyFenceRequest(sessionId: string, workItemId: string, dryRun: boolean, digest?: string): HistoryFenceRequest {
  return {
    session_id: sessionId,
    idempotency_key: `aoidots-history-fence:${workItemId}`,
    acknowledgment: "provider_outcome_unknown_no_replay",
    dry_run: dryRun,
    ...(digest ? { expected_state_digest: digest } : {}),
  };
}

function parseHistoryFenceReview(
  candidate: AoiDotsHistoryFenceCandidate,
  value: unknown,
): AoiDotsHistoryFenceReview {
  const response = record(value);
  if (response.status !== "dry_run_ok" && response.status !== "already_fenced") {
    throw new Error("履歴区切りの確認結果が不正です。");
  }
  const evidence = record(response.evidence);
  const snapshot = record(evidence.snapshot);
  const run = record(snapshot.run);
  const summary = record(evidence.child_summary);
  const children = Array.isArray(summary.work_items) ? summary.work_items : [];
  return {
    workItemId: candidate.workItemId,
    safeErrorCode: candidate.safeErrorCode,
    status: response.status,
    stateDigest: typeof response.state_digest === "string" && /^[0-9a-f]{64}$/.test(response.state_digest)
      ? response.state_digest
      : null,
    runStatus: typeof run.status === "string" ? run.status : null,
    childWorkStates: children.flatMap((child) => {
      const state = record(child).state;
      return typeof state === "string" ? [state] : [];
    }),
    errorCode: null,
    errorStatus: null,
  };
}

export const aoidotsApi = {
  async bootstrap(projectId?: string | null): Promise<AoiDotsProfile> {
    const request: BootstrapRequest = {
      context_project_hint_id: projectId?.trim() || null,
    };
    // Keep the pointer bound to the identity that started this request.  The
    // bootstrap response can arrive after logout/login or a server switch;
    // reading the scope only after POST would then store the old root under
    // the new owner's key.
    const [requestToken, requestServerFingerprint] = await Promise.all([
      getToken(),
      getConfiguredApiServerFingerprint(),
    ]);
    const requestAuthScope = getTokenAuthScope(requestToken);
    // Do not let route preflight select a different network endpoint after
    // this request's server identity was captured.  The helper still performs
    // its normal SDK auth/route guards; this boundary adds the owner guard
    // that bootstrap needs before and after the durable request.
    if (getTokenAuthScope(await getToken()) !== requestAuthScope) {
      throw new Error("AoiDots bootstrapのアカウントが変更されました。");
    }
    const data = await fetchApiAtServerFingerprint<BootstrapResponse>(
      requestServerFingerprint,
      "/api/aoidots/bootstrap",
      {
        method: "POST",
        // Project is a context hint only. The server owns the projectless root
        // and ordinary Agent binding; never send project_id here.
        body: JSON.stringify(request),
      },
      CHAT_TIMEOUT,
    );
    const profile = parseAoiDotsProfile(data);
    const [currentToken, currentServerFingerprint] = await Promise.all([
      getToken(),
      getConfiguredApiServerFingerprint(),
    ]);
    // A token refresh for the same owner is valid and should retain the
    // stable root.  A principal/server change makes this response stale, so
    // reject it before the caller can display or persist the old profile.
    if (
      getTokenAuthScope(currentToken) !== requestAuthScope
      || currentServerFingerprint !== requestServerFingerprint
    ) {
      throw new Error("AoiDots bootstrapの接続先またはアカウントが変更されました。");
    }
    await writeAoiDotsRootPointer(
      requestAuthScope,
      requestServerFingerprint,
      profile,
    );
    return profile;
  },

  async listWork(options?: {
    rootSessionId?: string | null;
    state?: string | null;
    limit?: number;
  }): Promise<AoiDotsWorkList> {
    const params = new URLSearchParams();
    if (options?.rootSessionId?.trim()) params.set("root_session_id", options.rootSessionId.trim());
    if (options?.state?.trim()) params.set("state", options.state.trim());
    if (options?.limit != null) params.set("limit", String(Math.max(1, Math.min(100, Math.floor(options.limit)))));
    const suffix = params.toString() ? `?${params.toString()}` : "";
    return parseAoiDotsWorkList(
      await fetchApi<WorkResponse>(`/api/aoidots/work${suffix}`, {}, CHAT_TIMEOUT),
    );
  },

  async listUncertainInteractiveWork(
    rootSessionId: string,
    agentId?: string | null,
  ): Promise<AoiDotsHistoryFenceCandidate[]> {
    const params = new URLSearchParams({ state: "uncertain", limit: "500" });
    if (agentId?.trim()) params.set("agent_id", agentId.trim());
    const payload = record(await fetchApi<unknown>(
      `/api/agent-work?${params.toString()}`,
      {},
      CHAT_TIMEOUT,
    ));
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
      ) return [];
      return [{
        workItemId: item.id.trim(),
        safeErrorCode: typeof item.safe_error_code === "string" ? item.safe_error_code : null,
      }];
    });
  },

  async previewHistoryFence(
    candidate: AoiDotsHistoryFenceCandidate,
    sessionId: string,
  ): Promise<AoiDotsHistoryFenceReview> {
    const id = candidate.workItemId.trim();
    if (!id) throw new Error("Workの識別情報がありません。");
    const request = historyFenceRequest(sessionId, id, true);
    return parseHistoryFenceReview(
      candidate,
      await fetchApi<HistoryFenceOperation["responses"][200]["content"]["application/json"]>(
        `/api/agent-work/${encodeURIComponent(id)}/history-fence`,
        { method: "POST", body: JSON.stringify(request) },
        CHAT_TIMEOUT,
      ),
    );
  },

  async applyHistoryFence(
    review: AoiDotsHistoryFenceReview,
    sessionId: string,
  ): Promise<{ status: string }> {
    if (review.status !== "dry_run_ok" || !review.stateDigest) {
      throw new Error("最新の確認結果がないため適用できません。");
    }
    const id = review.workItemId.trim();
    const request = historyFenceRequest(sessionId, id, false, review.stateDigest);
    const result = record(await fetchApi<unknown>(
      `/api/agent-work/${encodeURIComponent(id)}/history-fence`,
      { method: "POST", body: JSON.stringify(request) },
      CHAT_TIMEOUT,
    ));
    if (typeof result.status !== "string") {
      throw new Error("履歴区切りの結果を確認できませんでした。");
    }
    return { status: result.status };
  },

  async getDialogue(dialogueId: string): Promise<AoiDotsDialogue> {
    const id = dialogueId.trim();
    if (!id) throw new Error("AoiDots dialogue id が空です");
    return parseAoiDotsDialogue(
      await fetchApi<DialogueResponse>(`/api/agent-dialogues/${encodeURIComponent(id)}`, {}, CHAT_TIMEOUT),
    );
  },

  async answerInput(
    dialogueId: string,
    inputId: string,
    answer: string,
    revisions: {
      expectedInputRevision: number;
      expectedControlRevision: number;
      clientMessageId?: string;
    },
  ): Promise<unknown> {
    const content = answer.trim();
    if (!content) throw new Error("回答を入力してください。");
    if (!Number.isInteger(revisions.expectedInputRevision) || revisions.expectedInputRevision < 1) {
      throw new Error("回答の入力revisionが不正です。");
    }
    if (!Number.isInteger(revisions.expectedControlRevision) || revisions.expectedControlRevision < 0) {
      throw new Error("回答のcontrol revisionが不正です。");
    }
    const clientMessageId = revisions.clientMessageId?.trim() || `aoidots-answer:${dialogueId}:${inputId}:${revisions.expectedInputRevision}`;
    const request: AnswerRequest = {
      answer: content,
      client_message_id: clientMessageId,
      expected_input_revision: revisions.expectedInputRevision,
      expected_control_revision: revisions.expectedControlRevision,
    };
    return fetchApi<AnswerOperation["responses"][200]["content"]["application/json"]>(
      `/api/agent-dialogues/${encodeURIComponent(dialogueId)}/inputs/${encodeURIComponent(inputId)}/answer`,
      {
        method: "POST",
        body: JSON.stringify(request),
      },
      CHAT_TIMEOUT,
    );
  },

  async control(
    dialogueId: string,
    action: Extract<AoiDotsCommand, { kind: "control" }>["action"],
    expectedControlRevision: number,
    instruction?: string,
  ): Promise<unknown> {
    if (!Number.isInteger(expectedControlRevision) || expectedControlRevision < 0) {
      throw new Error("control revisionが不正です。");
    }
    const request: ControlRequest = {
      action,
      expected_control_revision: expectedControlRevision,
      ...(action === "rework" && instruction?.trim()
        ? { instruction: instruction.trim() }
        : {}),
    };
    return fetchApi<ControlOperation["responses"][200]["content"]["application/json"]>(
      `/api/agent-dialogues/${encodeURIComponent(dialogueId)}/control`,
      {
        method: "POST",
        body: JSON.stringify(request),
      },
      CHAT_TIMEOUT,
    );
  },
};

export async function sendAoiDotsCommand(dialogueId: string, command: AoiDotsCommand): Promise<unknown> {
  return command.kind === "answer"
    ? aoidotsApi.answerInput(dialogueId, command.input_id, command.answer, {
        expectedInputRevision: command.expected_input_revision,
        expectedControlRevision: command.expected_control_revision,
      })
    : command.action === "rework"
      ? aoidotsApi.control(
          dialogueId,
          command.action,
          command.expected_control_revision,
          command.instruction,
        )
      : aoidotsApi.control(dialogueId, command.action, command.expected_control_revision);
}
