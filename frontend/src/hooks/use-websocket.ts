"use client";

import { useEffect, useRef, useCallback, useState } from "react";
import type {
  ChatAttachmentKind,
  ChatAttachmentMetadata,
  ChatCommandCapability,
  ChatResponseModelSelection,
} from "@/lib/chat-api";
import { isWebSocketMessageForSession } from "@/lib/chat-websocket-events";

type WSMessage = {
  type: string;
  content?: string;
  session_id?: string;
  agent_run_id?: string;
  tool?: string;
  message?: string;
  status?: string;
  [key: string]: unknown;
};

type UploadedProjectAttachment = {
  name: string;
  path: string;
  kind: ChatAttachmentKind;
  registered: boolean;
  size?: number;
};

function inferProjectAttachmentKind(
  message: string,
  file: File,
): ChatAttachmentKind {
  const haystack = `${message} ${file.name}`.toLowerCase();
  if (/(^|[^a-z])wbs([^a-z]|$)|ｗｂｓ|工程表|作業分解/.test(haystack)) {
    return "wbs";
  }
  if (/課題|issue/.test(haystack)) return "issue";
  if (/リスク|risk/.test(haystack)) return "risk";
  if (/議事録|確認事項|要確認|依頼事項|qa|q&a/.test(haystack)) {
    return "request";
  }
  return "attachment";
}

async function uploadProjectAttachment(
  projectId: string,
  file: File,
  kind: ChatAttachmentKind,
): Promise<UploadedProjectAttachment> {
  const formData = new FormData();
  formData.append("file", file);
  formData.append("kind", kind);
  formData.append("directory", kind === "attachment" ? "attachments" : "management");

  const res = await fetch(`/api/projects/${projectId}/management/files`, {
    method: "POST",
    credentials: "include",
    body: formData,
  });
  if (!res.ok) {
    const detail = await res
      .json()
      .catch(() => ({ detail: `Upload failed: ${res.status}` }));
    throw new Error(detail.detail || `Upload failed: ${res.status}`);
  }
  const data = (await res.json()) as UploadedProjectAttachment;
  return {
    name: data.name,
    path: data.path,
    kind: data.kind,
    registered: data.registered,
    size: data.size,
  };
}

async function fileToImagePayload(file: File) {
  const dataUrl = await new Promise<string>((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result as string);
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });
  return { data: dataUrl, mimeType: file.type, name: file.name };
}

const MAX_ATTACHMENT_CONTEXT_LENGTH = 10000;

const TOOL_LABELS: Record<string, string> = {
  web_search: "Web検索",
  search_web: "Web検索",
  deep_research: "Deep Research",
  search_files: "ファイル検索",
  read_file: "ファイル読み取り",
  write_file: "ファイル書き込み",
  execute_command: "コマンド実行",
  execute_code: "コード実行",
  generate_image: "画像生成",
};

function getToolLabel(toolName: string): string {
  const normalized = toolName.trim();
  if (!normalized) return "ツール";
  if (TOOL_LABELS[normalized]) return TOOL_LABELS[normalized];
  if (/search/i.test(normalized)) {
    return `${normalized.replace(/_/g, " ")} 検索`;
  }
  if (/agent|delegate|assistant/i.test(normalized)) {
    return `${normalized.replace(/_/g, " ")} への委譲`;
  }
  return normalized.replace(/_/g, " ");
}

function extractActivityMessage(data: WSMessage): string | null {
  const nestedData =
    data.data && typeof data.data === "object"
      ? (data.data as Record<string, unknown>)
      : null;
  const message =
    typeof data.message === "string"
      ? data.message
      : typeof nestedData?.message === "string"
        ? nestedData.message
        : typeof data.content === "string"
          ? data.content
          : null;
  return message && message.trim().length > 0 ? message : null;
}

function extractAgentRunId(data: WSMessage): string | null {
  const nestedData =
    data.data && typeof data.data === "object"
      ? (data.data as Record<string, unknown>)
      : null;
  const value =
    typeof data.agent_run_id === "string"
      ? data.agent_run_id
      : typeof nestedData?.agent_run_id === "string"
        ? nestedData.agent_run_id
        : null;
  return value && value.trim().length > 0 ? value : null;
}

function createBaseAttachment(file: File): ChatAttachmentMetadata {
  return {
    name: file.name,
    size: file.size,
    mime_type: file.type || undefined,
  };
}

function isLikelyTextFile(file: File): boolean {
  if (file.type.startsWith("text/")) return true;
  return /\.(txt|md|markdown|csv|tsv|json|jsonl|ya?ml|xml|html?|css|scss|sass|js|jsx|ts|tsx|py|rb|go|rs|java|c|cc|cpp|h|hpp|cs|php|sql|log|ini|toml|env)$/i.test(
    file.name,
  );
}

async function fileToAttachmentContext(file: File): Promise<string> {
  if (file.type.startsWith("image/")) {
    return `[添付画像: ${file.name}]`;
  }
  if (!isLikelyTextFile(file)) {
    return `[添付ファイル: ${file.name}]`;
  }

  try {
    const text = await file.text();
    const truncated =
      text.length > MAX_ATTACHMENT_CONTEXT_LENGTH
        ? `${text.slice(0, MAX_ATTACHMENT_CONTEXT_LENGTH)}\n...(省略)`
        : text;
    return `[添付ファイル: ${file.name}]\n\`\`\`\n${truncated}\n\`\`\``;
  } catch {
    return `[添付ファイル: ${file.name}]`;
  }
}

function projectAttachmentToContext(attachment: ChatAttachmentMetadata): string {
  if (attachment.upload_failed) {
    return `[添付アップロード失敗: ${attachment.name}] ${attachment.error ?? ""}`.trim();
  }
  const location = attachment.project_relative_path ?? attachment.path ?? "";
  return `[プロジェクト添付: ${attachment.name}]${location ? ` ${location}` : ""}`;
}

export function useWebSocket(sessionId: string | null) {
  const wsRef = useRef<WebSocket | null>(null);
  const sessionIdRef = useRef<string | null>(sessionId);
  const reconnectTimerRef = useRef<number | null>(null);
  const [isConnected, setIsConnected] = useState(false);
  const [lastMessage, setLastMessage] = useState<WSMessage | null>(null);
  const [isStreaming, setIsStreaming] = useState(false);
  const [activeTool, setActiveTool] = useState<string | null>(null);
  const [activityMessage, setActivityMessage] = useState<string | null>(null);
  const [activeAgentRunId, setActiveAgentRunId] = useState<string | null>(null);
  const streamBufferRef = useRef("");

  useEffect(() => {
    const currentSessionId = sessionId;
    let resetCancelled = false;

    sessionIdRef.current = currentSessionId;
    streamBufferRef.current = "";

    queueMicrotask(() => {
      if (resetCancelled || sessionIdRef.current !== currentSessionId) return;
      setLastMessage(null);
      setIsConnected(false);
      setIsStreaming(false);
      setActiveTool(null);
      setActivityMessage(null);
      setActiveAgentRunId(null);
    });

    if (!sessionId) {
      return () => {
        resetCancelled = true;
      };
    }

    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const wsUrl = `${protocol}//${window.location.hostname}:3000/ws?session_id=${sessionId}`;
    let cancelled = false;
    let reconnectAttempt = 0;

    const clearReconnectTimer = () => {
      if (reconnectTimerRef.current) {
        window.clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }
    };

    const connect = () => {
      if (cancelled) return;

      const ws = new WebSocket(wsUrl);
      wsRef.current = ws;

      ws.onopen = () => {
        if (cancelled || wsRef.current !== ws) return;
        reconnectAttempt = 0;
        setIsConnected(true);
      };
      ws.onclose = () => {
        if (cancelled || wsRef.current !== ws) return;
        setIsConnected(false);
        setIsStreaming(false);
        setActiveTool(null);
        setActivityMessage(null);
        setActiveAgentRunId(null);
        if (!cancelled) {
          const delay = Math.min(1000 * 2 ** reconnectAttempt, 5000);
          reconnectAttempt += 1;
          clearReconnectTimer();
          reconnectTimerRef.current = window.setTimeout(connect, delay);
        }
      };
      ws.onmessage = (event) => {
        try {
          if (cancelled || wsRef.current !== ws) return;
          const data = JSON.parse(event.data) as WSMessage;
          if (!isWebSocketMessageForSession(data, sessionIdRef.current)) {
            return;
          }

          setLastMessage(data);
          const nextAgentRunId = extractAgentRunId(data);
          if (nextAgentRunId) {
            setActiveAgentRunId(nextAgentRunId);
          }
          if (data.type === "stream_start") {
            setIsStreaming(true);
            setActiveTool(null);
            setActivityMessage(
              extractActivityMessage(data) ?? "応答を生成しています...",
            );
            streamBufferRef.current = "";
          }
          if (data.type === "stream_token") {
            streamBufferRef.current += data.content || "";
          }
          if (data.type === "tool_start") {
            const toolName =
              typeof data.tool === "string" && data.tool ? data.tool : "tool";
            setActiveTool(toolName);
            setActivityMessage(
              extractActivityMessage(data) ??
                `${getToolLabel(toolName)} を実行しています...`,
            );
          }
          if (data.type === "tool_end") {
            setActiveTool(null);
            setActivityMessage(
              extractActivityMessage(data) ?? "ツール実行が完了しました。",
            );
          }
          if (data.type === "steering_update") {
            setActivityMessage(
              extractActivityMessage(data) ?? "追加指示を受け取りました。",
            );
          }
          if (
            data.type === "reasoning_progress" ||
            data.type === "status_update"
          ) {
            const message = extractActivityMessage(data);
            if (message) {
              setActivityMessage(message);
            }
          }
          if (
            data.type === "stream_end" ||
            data.type === "response" ||
            data.type === "stream_cancelled"
          ) {
            setIsStreaming(false);
            setActiveTool(null);
            setActivityMessage(null);
            setActiveAgentRunId(null);
          }
        } catch {
          /* JSON以外のメッセージは無視 */
        }
      };
      ws.onerror = () => {
        if (!cancelled && wsRef.current === ws) {
          setIsConnected(false);
        }
      };
    };

    connect();

    return () => {
      resetCancelled = true;
      cancelled = true;
      clearReconnectTimer();
      wsRef.current?.close();
      wsRef.current = null;
    };
  }, [sessionId]);

  const sendMessage = useCallback(
    (
      content: string,
      projectId?: string,
      files?: File[],
      mentions?: { type: string; id: string; name: string }[],
      generationProfile?: string,
      includeProjectContext?: boolean,
      editMessageId?: string,
      responseModel?: ChatResponseModelSelection,
      targetSessionId?: string | null,
      clientMessageId?: string,
      commandCapabilities?: ChatCommandCapability[],
    ) => {
      if (wsRef.current?.readyState !== WebSocket.OPEN) return;
      setActiveAgentRunId(null);

      const buildPayload = async () => {
        const data: Record<string, unknown> = { message: content };
        if (clientMessageId) data.client_message_id = clientMessageId;
        if (commandCapabilities && commandCapabilities.length > 0) {
          data.command_capabilities = commandCapabilities;
        }
        if (projectId) data.project_id = projectId;
        data.include_project_context = includeProjectContext === true;
        const payloadSessionId = targetSessionId ?? sessionIdRef.current;
        if (payloadSessionId) data.session_id = payloadSessionId;

        if (files && files.length > 0) {
          const attachments: ChatAttachmentMetadata[] = files.map((file) =>
            createBaseAttachment(file),
          );
          const firstImage = files.find((file) => file.type.startsWith("image/"));
          if (firstImage) {
            data.image = await fileToImagePayload(firstImage);
          }

          if (projectId) {
            for (const [index, file] of files.entries()) {
              const baseAttachment = attachments[index] ?? createBaseAttachment(file);
              const kind = inferProjectAttachmentKind(content, file);
              try {
                const uploaded = await uploadProjectAttachment(projectId, file, kind);
                attachments[index] = {
                  ...baseAttachment,
                  name: uploaded.name,
                  path: `_projects/project_${projectId}/${uploaded.path}`,
                  project_relative_path: uploaded.path,
                  kind: uploaded.kind,
                  registered: uploaded.registered,
                  size: uploaded.size ?? baseAttachment.size,
                };
              } catch (error) {
                const detail = error instanceof Error ? error.message : "unknown error";
                attachments[index] = {
                  ...baseAttachment,
                  kind,
                  upload_failed: true,
                  error: detail,
                };
              }
            }
            data.attachments = attachments;
            data.attachment_context = attachments
              .map((attachment) => projectAttachmentToContext(attachment))
              .join("\n");
          } else {
            data.attachments = attachments;
            data.attachment_context = (
              await Promise.all(files.map((file) => fileToAttachmentContext(file)))
            ).join("\n\n");
          }
        }

        if (mentions && mentions.length > 0) {
          data.mentions = mentions;
        }

        if (generationProfile) {
          data.generation_profile = generationProfile;
        }
        if (editMessageId) {
          data.edit_message_id = editMessageId;
        }
        if (responseModel) {
          data.response_model = responseModel;
        }

        wsRef.current?.send(JSON.stringify({ type: "user_message", data }));
      };

      buildPayload().catch(console.error);
    },
    []
  );

  const sendPermissionResponse = useCallback(
    (requestId: string, approved: boolean) => {
      if (wsRef.current?.readyState !== WebSocket.OPEN) return;
      wsRef.current.send(
        JSON.stringify({
          type: "external_llm_permission_response",
          data: {
            request_id: requestId,
            approved,
          },
        }),
      );
    },
    [],
  );

  const sendExternalModelPromptResponse = useCallback(
    (requestId: string, approved: boolean, prompt: string) => {
      if (wsRef.current?.readyState !== WebSocket.OPEN) return;
      wsRef.current.send(
        JSON.stringify({
          type: "external_model_prompt_response",
          data: {
            request_id: requestId,
            approved,
            prompt,
          },
        }),
      );
    },
    [],
  );

  const stopGeneration = useCallback((targetSessionId?: string | null) => {
    if (wsRef.current?.readyState !== WebSocket.OPEN) return false;
    wsRef.current.send(
      JSON.stringify({
        type: "stop_generation",
        data: {
          session_id: targetSessionId ?? sessionIdRef.current,
        },
      }),
    );
    return true;
  }, []);

  const sendSteering = useCallback(
    (content: string, targetSessionId?: string | null) => {
      if (wsRef.current?.readyState !== WebSocket.OPEN) return false;
      wsRef.current.send(
        JSON.stringify({
          type: "steer_generation",
          data: {
            session_id: targetSessionId ?? sessionIdRef.current,
            message: content,
          },
        }),
      );
      return true;
    },
    [],
  );

  return {
    isConnected,
    lastMessage,
    isStreaming,
    activeTool,
    activityMessage,
    activeAgentRunId,
    streamBuffer: streamBufferRef,
    sendMessage,
    sendPermissionResponse,
    sendExternalModelPromptResponse,
    stopGeneration,
    sendSteering,
  };
}
