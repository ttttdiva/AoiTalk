import type {
  ChatResponseModelSelection,
  ConversationMessage,
} from "../../types/api";
import {
  sanitizeChatAttachmentMetadata,
  type ChatAttachmentMetadata,
} from "../../lib/chat-api";
import {
  sanitizeChatCommandCapabilities,
  type ChatCommandCapability,
} from "./chat-commands";

export const PENDING_DISPATCH_METADATA_KEY = "pending_dispatch";
export const PENDING_DISPATCH_VERSION = 1 as const;

export type PendingDispatchPayload = {
  message: string;
  project_id?: string;
  context_project_hint_id?: string;
  app_id?: string;
  app_target_id?: string;
  include_project_context: boolean;
  agent_mode: string;
  /** Exact Agent binding observed when this input was queued. */
  expected_agent_id: string | null;
  edit_message_id?: string;
  response_model?: ChatResponseModelSelection;
  command_capabilities?: ChatCommandCapability[];
  tools_required?: boolean;
  attachments?: ChatAttachmentMetadata[];
  client_message_id: string;
};

type StoredPendingDispatchPayload = Omit<
  PendingDispatchPayload,
  "client_message_id"
>;

export type PendingDispatchSnapshot = {
  version: typeof PENDING_DISPATCH_VERSION;
  payload: StoredPendingDispatchPayload;
};

export type PendingDispatchFallback = {
  projectId?: string | null;
  contextProjectHintId?: string | null;
  appId?: string | null;
  appTargetId?: string | null;
  includeProjectContext?: boolean;
  agentMode?: string;
  expectedAgentId?: string | null;
};

type PendingDispatchInput = {
  message: string;
  projectId?: string | null;
  contextProjectHintId?: string | null;
  appId?: string | null;
  appTargetId?: string | null;
  includeProjectContext?: boolean;
  agentMode?: string;
  expectedAgentId?: string | null;
  editMessageId?: string;
  responseModel?: ChatResponseModelSelection;
  commandCapabilities?: ChatCommandCapability[];
  attachments?: Array<ChatAttachmentMetadata | Record<string, unknown>>;
};

function optionalString(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value.trim() : undefined;
}

function nullableString(value: unknown): string | null {
  return optionalString(value) ?? null;
}

function responseModelOf(value: unknown): ChatResponseModelSelection | undefined {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return undefined;
  }
  const provider = optionalString(
    (value as Record<string, unknown>).provider,
  );
  const model = optionalString((value as Record<string, unknown>).model);
  const effort = optionalString((value as Record<string, unknown>).reasoning_effort);
  return provider && model ? { provider, model, ...(effort ? { reasoning_effort: effort } : {}) } : undefined;
}

function storedPayloadOf(
  value: unknown,
): StoredPendingDispatchPayload | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const record = value as Record<string, unknown>;
  if (typeof record.message !== "string" || !record.message.trim()) return null;
  const projectId = optionalString(record.project_id);
  const contextProjectHintId = optionalString(record.context_project_hint_id);
  const appId = optionalString(record.app_id);
  const appTargetId = optionalString(record.app_target_id);
  const expectedAgentId = nullableString(record.expected_agent_id);
  const editMessageId = optionalString(record.edit_message_id);
  const responseModel = responseModelOf(record.response_model);
  const commandCapabilities = sanitizeChatCommandCapabilities(
    record.command_capabilities,
  );
  return {
    message: record.message,
    ...(projectId ? { project_id: projectId } : {}),
    ...(contextProjectHintId ? { context_project_hint_id: contextProjectHintId } : {}),
    ...(appId ? { app_id: appId } : {}),
    ...(appTargetId ? { app_target_id: appTargetId } : {}),
    include_project_context:
      typeof record.include_project_context === "boolean"
        ? record.include_project_context
        : Boolean(projectId),
    agent_mode: optionalString(record.agent_mode) ?? "confirm",
    expected_agent_id: expectedAgentId,
    ...(editMessageId ? { edit_message_id: editMessageId } : {}),
    ...(responseModel ? { response_model: responseModel } : {}),
    ...(commandCapabilities.length
      ? { command_capabilities: commandCapabilities }
      : {}),
    ...(typeof record.tools_required === "boolean"
      ? { tools_required: record.tools_required }
      : {}),
    ...(sanitizeChatAttachmentMetadata(record.attachments).length
      ? { attachments: sanitizeChatAttachmentMetadata(record.attachments) }
      : {}),
  };
}

function freezePayload(
  payload: StoredPendingDispatchPayload,
): StoredPendingDispatchPayload {
  if (payload.response_model) Object.freeze(payload.response_model);
  if (payload.command_capabilities) Object.freeze(payload.command_capabilities);
  return Object.freeze(payload);
}

/**
 * 送信開始時のPOST fieldを一度だけ正規化する。
 *
 * client_message_idはappendLocalMessageが同じtransactionで採番する
 * ConversationMessage.idをdecoder側で注入する。metadataとmessage.idが同時に
 * 永続化されるため、append直後にbackground flushが先行しても同じ値になる。
 */
export function buildPendingDispatchMetadata(
  input: PendingDispatchInput,
): Record<string, unknown> {
  const payload = freezePayload({
    message: input.message,
    ...(optionalString(input.projectId)
      ? { project_id: optionalString(input.projectId) }
      : {}),
    ...(optionalString(input.contextProjectHintId)
      ? { context_project_hint_id: optionalString(input.contextProjectHintId) }
      : {}),
    ...(optionalString(input.appId)
      ? { app_id: optionalString(input.appId) }
      : {}),
    ...(optionalString(input.appTargetId)
      ? { app_target_id: optionalString(input.appTargetId) }
      : {}),
    include_project_context:
      input.includeProjectContext ?? Boolean(optionalString(input.projectId)),
    agent_mode: optionalString(input.agentMode) ?? "confirm",
    expected_agent_id: nullableString(input.expectedAgentId),
    ...(optionalString(input.editMessageId)
      ? { edit_message_id: optionalString(input.editMessageId) }
      : {}),
    ...(responseModelOf(input.responseModel)
      ? { response_model: responseModelOf(input.responseModel) }
      : {}),
    ...(sanitizeChatCommandCapabilities(input.commandCapabilities).length
      ? {
          command_capabilities: sanitizeChatCommandCapabilities(
            input.commandCapabilities,
          ),
          tools_required: true,
        }
      : {}),
    ...(sanitizeChatAttachmentMetadata(input.attachments).length
      ? { attachments: sanitizeChatAttachmentMetadata(input.attachments) }
      : {}),
  });
  const snapshot = Object.freeze({
    version: PENDING_DISPATCH_VERSION,
    payload,
  });

  // Flat fields are retained while older repository builds still decode them.
  return {
    [PENDING_DISPATCH_METADATA_KEY]: snapshot,
    ...(payload.project_id ? { project_id: payload.project_id } : {}),
    ...(payload.context_project_hint_id
      ? { context_project_hint_id: payload.context_project_hint_id }
      : {}),
    include_project_context: payload.include_project_context,
    agent_mode: payload.agent_mode,
    expected_agent_id: payload.expected_agent_id,
    ...(payload.edit_message_id
      ? { edit_message_id: payload.edit_message_id }
      : {}),
    ...(payload.response_model
      ? { response_model: { ...payload.response_model } }
      : {}),
    ...(payload.command_capabilities
      ? { command_capabilities: [...payload.command_capabilities] }
      : {}),
    ...(typeof payload.tools_required === "boolean"
      ? { tools_required: payload.tools_required }
      : {}),
    ...(payload.attachments?.length
      ? { attachments: sanitizeChatAttachmentMetadata(payload.attachments) }
      : {}),
  };
}

function snapshotPayload(message: ConversationMessage) {
  const snapshot = message.metadata?.[PENDING_DISPATCH_METADATA_KEY];
  if (!snapshot || typeof snapshot !== "object" || Array.isArray(snapshot)) {
    return null;
  }
  const record = snapshot as Record<string, unknown>;
  if (record.version !== PENDING_DISPATCH_VERSION) return null;
  return storedPayloadOf(record.payload);
}

/**
 * 保留中のローカル入力を編集したとき、本文と再送用snapshotを同時に更新する。
 * 送信経路・モデル・添付など、送信開始時に固定した他のfieldは保持する。
 */
export function replacePendingDispatchMessage(
  message: ConversationMessage,
  content: string,
): Record<string, unknown> {
  const normalized = content.trim();
  if (!normalized) throw new Error("メッセージ本文を入力してください。");
  const payload = pendingDispatchPayload(message);
  return {
    ...(message.metadata ?? {}),
    ...buildPendingDispatchMetadata({
      message: normalized,
      projectId: payload.project_id,
      contextProjectHintId: payload.context_project_hint_id,
      appId: payload.app_id,
      appTargetId: payload.app_target_id,
      includeProjectContext: payload.include_project_context,
      agentMode: payload.agent_mode,
      expectedAgentId: payload.expected_agent_id,
      editMessageId: payload.edit_message_id,
      responseModel: payload.response_model,
      commandCapabilities: payload.command_capabilities,
      attachments: payload.attachments,
    }),
    direct_error: null,
    delivery_error: null,
  };
}
export function pendingDispatchPayload(
  message: ConversationMessage,
  fallback: PendingDispatchFallback = {},
): PendingDispatchPayload {
  const stored = snapshotPayload(message);
  if (stored) {
    return {
      ...stored,
      response_model: stored.response_model
        ? { ...stored.response_model }
        : undefined,
      command_capabilities: stored.command_capabilities
        ? [...stored.command_capabilities]
        : undefined,
      tools_required: stored.tools_required,
      client_message_id: message.id,
    };
  }

  const metadata = message.metadata ?? {};
  const projectId =
    optionalString(metadata.project_id) ?? optionalString(fallback.projectId);
  const contextProjectHintId =
    optionalString(metadata.context_project_hint_id) ??
    optionalString(fallback.contextProjectHintId);
  const appId = optionalString(metadata.app_id) ?? optionalString(fallback.appId);
  const appTargetId =
    optionalString(metadata.app_target_id) ?? optionalString(fallback.appTargetId);
  const expectedAgentId = Object.prototype.hasOwnProperty.call(
    metadata,
    "expected_agent_id",
  )
    ? nullableString(metadata.expected_agent_id)
    : fallback.expectedAgentId ?? null;
  const editMessageId = optionalString(metadata.edit_message_id);
  const responseModel = responseModelOf(metadata.response_model);
  const commandCapabilities = sanitizeChatCommandCapabilities(
    metadata.command_capabilities,
  );
  const attachments =
    metadata.attachments == null
      ? []
      : sanitizeChatAttachmentMetadata(metadata.attachments);
  return {
    message: message.content,
    ...(projectId ? { project_id: projectId } : {}),
    ...(contextProjectHintId
      ? { context_project_hint_id: contextProjectHintId }
      : {}),
    ...(appId ? { app_id: appId } : {}),
    ...(appTargetId ? { app_target_id: appTargetId } : {}),
    include_project_context:
      typeof metadata.include_project_context === "boolean"
        ? metadata.include_project_context
        : fallback.includeProjectContext ?? Boolean(projectId),
    agent_mode:
      optionalString(metadata.agent_mode) ??
      optionalString(fallback.agentMode) ??
      "confirm",
    expected_agent_id: expectedAgentId,
    ...(editMessageId ? { edit_message_id: editMessageId } : {}),
    ...(responseModel ? { response_model: responseModel } : {}),
    ...(commandCapabilities.length
      ? { command_capabilities: commandCapabilities, tools_required: true }
      : {}),
    ...(attachments.length ? { attachments } : {}),
    client_message_id: message.id,
  };
}
