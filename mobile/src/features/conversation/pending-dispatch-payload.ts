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
  app_id?: string;
  app_target_id?: string;
  include_project_context: boolean;
  agent_mode: string;
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
  appId?: string | null;
  appTargetId?: string | null;
  includeProjectContext?: boolean;
  agentMode?: string;
};

type PendingDispatchInput = {
  message: string;
  projectId?: string | null;
  appId?: string | null;
  appTargetId?: string | null;
  includeProjectContext?: boolean;
  agentMode?: string;
  editMessageId?: string;
  responseModel?: ChatResponseModelSelection;
  commandCapabilities?: ChatCommandCapability[];
  attachments?: Array<ChatAttachmentMetadata | Record<string, unknown>>;
};

function optionalString(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value.trim() : undefined;
}

function responseModelOf(value: unknown): ChatResponseModelSelection | undefined {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return undefined;
  }
  const provider = optionalString(
    (value as Record<string, unknown>).provider,
  );
  const model = optionalString((value as Record<string, unknown>).model);
  return provider && model ? { provider, model } : undefined;
}

function storedPayloadOf(
  value: unknown,
): StoredPendingDispatchPayload | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const record = value as Record<string, unknown>;
  if (typeof record.message !== "string" || !record.message.trim()) return null;
  const projectId = optionalString(record.project_id);
  const appId = optionalString(record.app_id);
  const appTargetId = optionalString(record.app_target_id);
  const editMessageId = optionalString(record.edit_message_id);
  const responseModel = responseModelOf(record.response_model);
  const commandCapabilities = sanitizeChatCommandCapabilities(
    record.command_capabilities,
  );
  return {
    message: record.message,
    ...(projectId ? { project_id: projectId } : {}),
    ...(appId ? { app_id: appId } : {}),
    ...(appTargetId ? { app_target_id: appTargetId } : {}),
    include_project_context:
      typeof record.include_project_context === "boolean"
        ? record.include_project_context
        : Boolean(projectId),
    agent_mode: optionalString(record.agent_mode) ?? "confirm",
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
    ...(optionalString(input.appId)
      ? { app_id: optionalString(input.appId) }
      : {}),
    ...(optionalString(input.appTargetId)
      ? { app_target_id: optionalString(input.appTargetId) }
      : {}),
    include_project_context:
      input.includeProjectContext ?? Boolean(optionalString(input.projectId)),
    agent_mode: optionalString(input.agentMode) ?? "confirm",
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
    include_project_context: payload.include_project_context,
    agent_mode: payload.agent_mode,
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
 * versioned snapshotを優先し、旧message metadataだけの場合は明示的に再構築する。
 */
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
  const appId = optionalString(metadata.app_id) ?? optionalString(fallback.appId);
  const appTargetId =
    optionalString(metadata.app_target_id) ?? optionalString(fallback.appTargetId);
  const editMessageId = optionalString(metadata.edit_message_id);
  const responseModel = responseModelOf(metadata.response_model);
  const commandCapabilities = sanitizeChatCommandCapabilities(
    metadata.command_capabilities,
  );
  return {
    message: message.content,
    ...(projectId ? { project_id: projectId } : {}),
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
    ...(editMessageId ? { edit_message_id: editMessageId } : {}),
    ...(responseModel ? { response_model: responseModel } : {}),
    ...(commandCapabilities.length
      ? { command_capabilities: commandCapabilities, tools_required: true }
      : {}),
    ...(sanitizeChatAttachmentMetadata(metadata.attachments).length
      ? { attachments: sanitizeChatAttachmentMetadata(metadata.attachments) }
      : {}),
    client_message_id: message.id,
  };
}
