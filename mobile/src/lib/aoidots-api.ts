/** AoiDots uses the server-owned ordinary Agent root and normal Chat session. */
import { CHAT_TIMEOUT } from "../constants/config";
import { fetchApi, fetchApiAtServerFingerprint, getConfiguredApiServerFingerprint } from "./api-client";
import type { AoiDotsCommand, AoiDotsDialogue, AoiDotsProfile, AoiDotsWorkList } from "../features/aoidots/types";
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
