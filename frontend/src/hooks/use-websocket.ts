"use client";

import { useEffect, useRef, useCallback, useState } from "react";
import type {
  ChatAttachmentKind,
  ChatAttachmentMetadata,
  ChatCommandCapability,
  ChatResponseModelSelection,
} from "@/lib/chat-api";
import type { MentionItem } from "@/components/chat/mention-menu";
import { isWebSocketMessageForSession } from "@/lib/chat-websocket-events";
import { isOversizedMailAttachment } from "@/lib/chat-attachment-validation";
import { buildWebSocketUrl } from "@/lib/websocket-url";
import { toast } from "sonner";

type WSMessage = {
  type: string;
  content?: string;
  session_id?: string;
  agent_run_id?: string;
  tool?: string;
  message?: string;
  status?: string;
  /** frontend-local connection scope; never sent back to the server */
  __connection_generation?: number;
  __connection_session_id?: string;
  [key: string]: unknown;
};

type UploadedProjectAttachment = {
  name: string;
  path: string;
  kind: ChatAttachmentKind;
  registered: boolean;
  size?: number;
};

type UploadedUserAttachment = {
  name: string;
  path: string;
  size?: number;
};

class UploadHttpError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "UploadHttpError";
  }
}

async function readUploadError(response: Response): Promise<string> {
  const body = await response.json().catch(() => null);
  if (
    body &&
    typeof body === "object" &&
    (typeof (body as { detail?: unknown }).detail === "string" ||
      typeof (body as { error?: unknown }).error === "string")
  ) {
    const detail =
      (body as { detail?: string; error?: string }).detail ||
      (body as { detail?: string; error?: string }).error;
    if (detail?.trim()) return detail;
  }
  return `HTTP ${response.status}`;
}

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
    throw new UploadHttpError(await readUploadError(res), res.status);
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

async function uploadUserAttachment(file: File): Promise<UploadedUserAttachment> {
  const formData = new FormData();
  formData.append("file", file);

  const res = await fetch("/api/user/attachments", {
    method: "POST",
    credentials: "include",
    body: formData,
  });
  if (!res.ok) {
    throw new UploadHttpError(await readUploadError(res), res.status);
  }
  const data = (await res.json()) as UploadedUserAttachment;
  return { name: data.name, path: data.path, size: data.size };
}

async function fileToMediaPayload(file: File) {
  const dataUrl = await new Promise<string>((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result as string);
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });
  return { data: dataUrl, mimeType: file.type, name: file.name };
}

function createBaseAttachment(file: File): ChatAttachmentMetadata {
  return {
    name: file.name,
    size: file.size,
    mime_type: file.type || undefined,
  };
}

function isImageFile(file: File): boolean {
  return file.type.startsWith("image/") || /\.(png|jpe?g|gif|webp|bmp|avif)$/i.test(file.name);
}

function isVideoFile(file: File): boolean {
  return (
    file.type.startsWith("video/") ||
    /\.(mp4|mov|mkv)$/i.test(file.name) ||
    (/\.webm$/i.test(file.name) && !file.type.startsWith("audio/"))
  );
}

function isAudioFile(file: File): boolean {
  return (
    !isVideoFile(file) &&
    (file.type.startsWith("audio/") || /\.(wav|mp3|m4a|flac|ogg|webm)$/i.test(file.name))
  );
}

function isMailFile(file: File): boolean {
  return /\.(msg|eml)$/i.test(file.name);
}

/**
 * 添付は本文を展開せず workspaces ルート基準のパス参照だけを渡す。
 * 実体はバックエンドの read_file が読む。
 */
function attachmentToContext(
  attachment: ChatAttachmentMetadata,
  file: File,
): string {
  if (attachment.upload_failed) {
    return `[添付アップロード失敗: ${attachment.name}] ${attachment.error ?? ""}`.trim();
  }
  const label = isImageFile(file)
    ? "添付画像"
    : isVideoFile(file)
      ? "添付動画"
      : isAudioFile(file)
      ? "添付音声"
      : "添付ファイル";
  const location = attachment.path ?? "";
  return `[${label}: ${attachment.name}]${location ? ` ${location}` : ""}`;
}

export function useWebSocket(
  sessionId: string | null,
  generationAgentRunId: string | null = null,
) {
  const wsRef = useRef<WebSocket | null>(null);
  const sessionIdRef = useRef<string | null>(sessionId);
  const reconnectTimerRef = useRef<number | null>(null);
  const connectionGenerationRef = useRef(0);
  const [connectionGeneration, setConnectionGeneration] = useState(0);
  const [isConnected, setIsConnected] = useState(false);
  const [lastMessage, setLastMessage] = useState<WSMessage | null>(null);
  const streamBufferRef = useRef("");

  useEffect(() => {
    const currentSessionId = sessionId;
    const currentConnectionGeneration = connectionGenerationRef.current + 1;
    connectionGenerationRef.current = currentConnectionGeneration;
    setConnectionGeneration(currentConnectionGeneration);
    let resetCancelled = false;

    sessionIdRef.current = currentSessionId;
    streamBufferRef.current = "";

    // セッション切替直後に旧接続の onclose が新接続の状態を上書きしないよう、
    // 現在の session ref を確認してから表示状態をリセットする。
    queueMicrotask(() => {
      if (resetCancelled || sessionIdRef.current !== currentSessionId) return;
      setLastMessage(null);
      setIsConnected(false);
    });

    if (!sessionId) {
      return () => {
        resetCancelled = true;
      };
    }

    const wsUrl = buildWebSocketUrl(sessionId);
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

          if (
            connectionGenerationRef.current !== currentConnectionGeneration ||
            sessionIdRef.current !== currentSessionId
          ) {
            return;
          }
          setLastMessage({
            ...data,
            __connection_generation: currentConnectionGeneration,
            __connection_session_id: currentSessionId ?? undefined,
          });
          if (data.type === "stream_start") {
            streamBufferRef.current = "";
          }
          if (data.type === "stream_token") {
            streamBufferRef.current += data.content || "";
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
      mentions?: MentionItem[],
      generationProfile?: string,
      planningPolicy?: string,
      includeProjectContext?: boolean,
      editMessageId?: string,
      responseModel?: ChatResponseModelSelection,
      targetSessionId?: string | null,
      clientMessageId?: string,
      commandCapabilities?: ChatCommandCapability[],
      toolsRequired?: boolean,
      appContext?: { appId: string; targetId: string } | null,
    ) => {
      const ws = wsRef.current;
      if (ws?.readyState !== WebSocket.OPEN) return Promise.resolve(false);
      if (files?.some(isOversizedMailAttachment)) {
        toast.error("メールファイルは 10 MB までです");
        return Promise.resolve(false);
      }
      const buildPayload = async () => {
        const data: Record<string, unknown> = { message: content };
        if (clientMessageId) data.client_message_id = clientMessageId;
        if (commandCapabilities && commandCapabilities.length > 0) {
          data.command_capabilities = commandCapabilities;
        }
        if (typeof toolsRequired === "boolean") {
          data.tools_required = toolsRequired;
        }
        if (projectId) data.project_id = projectId;
        data.include_project_context = includeProjectContext === true;
        // Include explicit nulls so removing/changing the chip can update the
        // durable ConversationSession scope on the server.
        if (appContext !== undefined) {
          data.app_id = appContext?.appId ?? null;
          data.app_target_id = appContext?.targetId ?? null;
        }
        const payloadSessionId = targetSessionId ?? sessionIdRef.current;
        if (payloadSessionId) data.session_id = payloadSessionId;

        if (files && files.length > 0) {
          const attachments: ChatAttachmentMetadata[] = files.map((file) =>
            createBaseAttachment(file),
          );
          const imageFiles = files.filter(isImageFile);
          if (imageFiles.length > 0) {
            data.images = await Promise.all(
              imageFiles.map((file) => fileToMediaPayload(file)),
            );
          }
          const audioFile = files.find(isAudioFile);
          if (audioFile) {
            data.audio = await fileToMediaPayload(audioFile);
          }
          const videoFile = files.find(isVideoFile);
          const mailPayloads = await Promise.all(
            files.map(async (file, index) =>
              isMailFile(file) ? { index, payload: await fileToMediaPayload(file) } : null,
            ),
          );
          for (const item of mailPayloads) {
            if (item) attachments[item.index].data_url = item.payload.data;
          }

          // 形式を問わず常時サーバー保存し、LLM へはパス参照だけを渡す
          for (const [index, file] of files.entries()) {
            const baseAttachment = attachments[index] ?? createBaseAttachment(file);
            const kind: ChatAttachmentKind = projectId
              ? inferProjectAttachmentKind(content, file)
              : "attachment";
            try {
              if (projectId) {
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
              } else {
                const uploaded = await uploadUserAttachment(file);
                attachments[index] = {
                  ...baseAttachment,
                  name: uploaded.name,
                  path: uploaded.path,
                  kind,
                  registered: false,
                  size: uploaded.size ?? baseAttachment.size,
                };
              }
            } catch (error) {
              const detail =
                error instanceof UploadHttpError
                  ? /\bHTTP \d{3}\b/.test(error.message)
                    ? error.message
                    : `${error.message} (HTTP ${error.status})`
                  : error instanceof Error
                    ? error.message
                    : "unknown error";
              attachments[index] = {
                ...baseAttachment,
                kind,
                upload_failed: true,
                error: detail,
              };
            }
          }
          if (videoFile) {
            const videoIndex = files.indexOf(videoFile);
            const videoAttachment = attachments[videoIndex];
            if (videoAttachment?.path && !videoAttachment.upload_failed) {
              data.video = {
                path: videoAttachment.path,
                mimeType: videoAttachment.mime_type || videoFile.type || undefined,
                name: videoAttachment.name || videoFile.name,
                size: videoAttachment.size ?? videoFile.size,
              };
            }
          }
          data.attachments = attachments;
          data.attachment_context = attachments
            .map((attachment, index) => attachmentToContext(attachment, files[index]))
            .join("\n");
        }

        if (mentions && mentions.length > 0) {
          data.mentions = mentions;
        }

        if (generationProfile) {
          data.generation_profile = generationProfile;
        }
        if (planningPolicy) {
          data.planning_policy = planningPolicy;
        }
        if (editMessageId) {
          data.edit_message_id = editMessageId;
        }
        if (responseModel) {
          data.response_model = responseModel;
        }

        if (wsRef.current !== ws || ws.readyState !== WebSocket.OPEN) {
          return false;
        }
        ws.send(JSON.stringify({ type: "user_message", data }));
        return true;
      };

      return buildPayload().catch((error) => {
        console.error(error);
        return false;
      });
    },
    []
  );

  const sendPermissionResponse = useCallback(
    (
      requestId: string,
      approved: boolean,
      scope: "once" | "session" = "once",
      targetSessionId?: string | null,
    ) => {
      if (wsRef.current?.readyState !== WebSocket.OPEN) return;
      const responseSessionId = targetSessionId ?? sessionIdRef.current;
      if (!responseSessionId || responseSessionId !== sessionIdRef.current) return;
      wsRef.current.send(
        JSON.stringify({
          type: "external_llm_permission_response",
          data: {
            request_id: requestId,
            approved,
            scope,
            session_id: responseSessionId,
          },
        }),
      );
    },
    [],
  );

  const sendExternalModelPromptResponse = useCallback(
    (
      requestId: string,
      approved: boolean,
      prompt: string,
      targetSessionId?: string | null,
    ) => {
      if (wsRef.current?.readyState !== WebSocket.OPEN) return;
      const responseSessionId = targetSessionId ?? sessionIdRef.current;
      if (!responseSessionId || responseSessionId !== sessionIdRef.current) return;
      wsRef.current.send(
        JSON.stringify({
          type: "external_model_prompt_response",
          data: {
            request_id: requestId,
            approved,
            prompt,
            session_id: responseSessionId,
          },
        }),
      );
    },
    [],
  );

  const stopGeneration = useCallback((targetSessionId?: string | null) => {
    if (wsRef.current?.readyState !== WebSocket.OPEN) return false;
    const effectiveSessionId = targetSessionId ?? sessionIdRef.current;
    wsRef.current.send(
      JSON.stringify({
        type: "stop_generation",
        data: {
          session_id: effectiveSessionId,
          agent_run_id:
            effectiveSessionId === sessionIdRef.current
              ? generationAgentRunId
              : null,
        },
      }),
    );
    return true;
  }, [generationAgentRunId]);

  const sendSteering = useCallback(
    (
      content: string,
      targetSessionId?: string | null,
      clientMessageId?: string,
    ) => {
      if (wsRef.current?.readyState !== WebSocket.OPEN) return false;
      wsRef.current.send(
        JSON.stringify({
          type: "steer_generation",
          data: {
            session_id: targetSessionId ?? sessionIdRef.current,
            agent_run_id:
              (targetSessionId ?? sessionIdRef.current) === sessionIdRef.current
                ? generationAgentRunId
                : null,
            message: content,
            client_message_id: clientMessageId,
          },
        }),
      );
      return true;
    },
    [generationAgentRunId],
  );

  const sendHumanInteractionResponse = useCallback(
    (
      requestId: string,
      payload: Record<string, unknown>,
      targetSessionId?: string | null,
      messageType:
        | "human_interaction_response"
        | "ask_user_question_response"
        | "plan_approval_response" = "human_interaction_response",
    ) => {
      if (wsRef.current?.readyState !== WebSocket.OPEN) return;
      const responseSessionId = targetSessionId ?? sessionIdRef.current;
      if (!responseSessionId || responseSessionId !== sessionIdRef.current) return;
      wsRef.current.send(
        JSON.stringify({
          type: messageType,
          data: {
            request_id: requestId,
            session_id: responseSessionId,
            ...payload,
          },
        }),
      );
    },
    [],
  );

  return {
    isConnected,
    lastMessage,
    streamBuffer: streamBufferRef,
    sendMessage,
    sendPermissionResponse,
    sendExternalModelPromptResponse,
    sendHumanInteractionResponse,
    stopGeneration,
    sendSteering,
    connectionGeneration,
  };
}
