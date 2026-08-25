import type {
  ChatAttachmentMetadata,
  ChatCommandCapability,
  ChatToolResultMetadata,
  ConversationMessage,
} from "@/lib/chat-api";

/**
 * ローカル（未保存）メッセージ生成とツール結果メタデータ判定のユーティリティ。
 * `chat/page.tsx` から挙動不変で切り出した純粋関数群。
 */

export function isChatToolResultMetadata(
  value: unknown,
): value is ChatToolResultMetadata {
  if (!value || typeof value !== "object") return false;
  const result = value as ChatToolResultMetadata;
  return (
    typeof result.output === "string" ||
    (Array.isArray(result.urls) &&
      result.urls.every((url) => typeof url === "string"))
  );
}

export function createLocalAttachmentMetadata(
  files?: File[],
): ChatAttachmentMetadata[] | undefined {
  if (!files?.length) return undefined;
  return files.map((file) => ({
    name: file.name,
    size: file.size,
    mime_type: file.type || undefined,
  }));
}

/**
 * 保存済みメッセージのメタデータから添付一覧を取り出す。
 *
 * 再実行では元の添付を引き継ぐ必要があるが、保存済みメタデータには
 * バイナリ(`data_url`)が含まれない。バックエンドが保存済みのプロジェクト内
 * パスから実体を読み直すため、path 情報を落とさずに渡す。
 */
export function attachmentsFromMessageMetadata(
  metadata: unknown,
): ChatAttachmentMetadata[] {
  if (!metadata || typeof metadata !== "object") return [];
  const attachments = (metadata as { attachments?: unknown }).attachments;
  if (!Array.isArray(attachments)) return [];
  return attachments.filter(
    (attachment): attachment is ChatAttachmentMetadata =>
      attachment != null &&
      typeof attachment === "object" &&
      typeof (attachment as ChatAttachmentMetadata).name === "string",
  );
}

export function createLocalMessage(
  sessionId: string,
  role: "user" | "assistant",
  content: string,
  metadata: Record<string, unknown> = {},
): ConversationMessage {
  return {
    id: `temp-${role}-${Date.now()}-${Math.random().toString(36).slice(2)}`,
    session_id: sessionId,
    role,
    content,
    metadata,
    created_at: new Date().toISOString(),
    parent_message_id: null,
    branch_index: 0,
    is_active_branch: true,
  };
}

export function createLocalUserMessage(
  sessionId: string,
  content: string,
  clientMessageId: string,
  files?: File[],
  commandCapabilities?: ChatCommandCapability[],
  extraMetadata: Record<string, unknown> = {},
): ConversationMessage {
  const metadata: ConversationMessage["metadata"] = {
    client_message_id: clientMessageId,
    ...extraMetadata,
  };
  if (commandCapabilities?.length) {
    metadata.command_capabilities = commandCapabilities;
  }
  const attachments = createLocalAttachmentMetadata(files);
  if (attachments) {
    metadata.attachments = attachments;
  }
  return createLocalMessage(sessionId, "user", content, metadata);
}
