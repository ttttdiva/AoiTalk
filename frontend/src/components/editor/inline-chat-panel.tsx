"use client";

import { useState, useRef, useEffect, useCallback } from "react";
import { Send, X, Quote } from "lucide-react";
import { Button } from "@/components/ui/button";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { useChatSessions } from "@/contexts/chat-session-context";
import { chatApi } from "@/lib/chat-api";
import { buildWebSocketUrl } from "@/lib/websocket-url";

interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  timestamp: string;
}

interface InlineChatPanelProps {
  filePath: string;
  selectedText?: string;
  onInsertText?: (text: string) => void;
  onClose: () => void;
}

export function InlineChatPanel({
  filePath,
  selectedText,
  onInsertText,
  onClose,
}: InlineChatPanelProps) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [isStreaming, setIsStreaming] = useState(false);
  const [streamContent, setStreamContent] = useState("");
  const [isConnected, setIsConnected] = useState(false);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [connectionError, setConnectionError] = useState<string | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const streamBufferRef = useRef("");
  const isStreamingRef = useRef(false);
  const { addSession } = useChatSessions();

  const fileName = filePath.split("/").pop() || filePath;

  // EnterpriseではWebSocketの書き込みに所有者付き会話セッションが必要。
  // ファイラーはチャットページのactiveSessionIdを共有しないため、このパネル
  // 専用のセッションを作成してから同一オリジンのWSへ接続する。キャラクターは
  // "aoi" に固定せず、Enterprise設定を含む現在のキャラクター解決APIに従う。
  useEffect(() => {
    let cancelled = false;

    void chatApi
      .getCurrentCharacterName()
      .then((characterName) => chatApi.createSession(characterName))
      .then(({ session }) => {
        if (cancelled) return;
        addSession(session);
        setSessionId(session.id);
      })
      .catch(() => {
        if (!cancelled) {
          setConnectionError("チャットセッションを作成できませんでした");
        }
      });

    return () => {
      cancelled = true;
    };
  }, [addSession]);

  // WebSocket接続。window.location.hostを使い、localhost内側の3000番へ
  // 直結せず、CaddyのTLS・Cookie認証境界を必ず通す。
  useEffect(() => {
    if (!sessionId) return;

    const wsUrl = buildWebSocketUrl(sessionId);
    let cancelled = false;
    let reconnectAttempt = 0;
    let reconnectTimer: number | null = null;
    let generationPollTimer: number | null = null;
    let pollGenerationUntilIdle: (() => void) | null = null;

    const clearReconnectTimer = () => {
      if (reconnectTimer !== null) {
        window.clearTimeout(reconnectTimer);
        reconnectTimer = null;
      }
    };

    const clearGenerationPollTimer = () => {
      if (generationPollTimer !== null) {
        window.clearTimeout(generationPollTimer);
        generationPollTimer = null;
      }
    };

    const applyPersistedMessages = (
      persisted: Awaited<ReturnType<typeof chatApi.getMessages>>["messages"],
    ) => {
      const durable = (persisted || [])
        .filter(
          (message) =>
            message.role === "user" || message.role === "assistant",
        )
        .map((message) => ({
          id: message.id,
          role: message.role as "user" | "assistant",
          content: message.content,
          timestamp: message.created_at
            ? new Date(message.created_at).toLocaleTimeString()
            : "",
        }));
      const durableIds = new Set(durable.map((message) => message.id));
      const durableContentCounts = new Map<string, number>();
      for (const message of durable) {
        const key = `${message.role}\u0000${message.content}`;
        durableContentCounts.set(key, (durableContentCounts.get(key) || 0) + 1);
      }
      setMessages((current) => {
        const pending = current.filter((message) => {
          if (durableIds.has(message.id)) return false;
          if (!message.id.startsWith("user-") && !message.id.startsWith("assistant-")) {
            return true;
          }
          const key = `${message.role}\u0000${message.content}`;
          const count = durableContentCounts.get(key) || 0;
          if (count > 0) {
            durableContentCounts.set(key, count - 1);
            return false;
          }
          return true;
        });
        return [...durable, ...pending];
      });
    };

    const connect = () => {
      if (cancelled) return;
      const ws = new WebSocket(wsUrl);
      wsRef.current = ws;

      const scheduleGenerationPoll = () => {
        clearGenerationPollTimer();
        if (cancelled) return;
        generationPollTimer = window.setTimeout(async () => {
          generationPollTimer = null;
          if (cancelled || wsRef.current !== ws) return;
          try {
            const status = await chatApi.getGenerationStatus(sessionId);
            if (cancelled || wsRef.current !== ws) return;
            if (status.running) {
              isStreamingRef.current = true;
              setIsStreaming(true);
              scheduleGenerationPoll();
              return;
            }
            isStreamingRef.current = false;
            setIsStreaming(false);
            setStreamContent("");
            streamBufferRef.current = "";
            const { messages: persisted } = await chatApi.getMessages(sessionId);
            if (!cancelled && wsRef.current === ws) {
              applyPersistedMessages(persisted);
            }
          } catch {
            scheduleGenerationPoll();
          }
        }, 1000);
      };
      pollGenerationUntilIdle = scheduleGenerationPoll;

      const reconcileAfterConnect = async () => {
        const [historyResult, statusResult] = await Promise.allSettled([
          chatApi.getMessages(sessionId),
          chatApi.getGenerationStatus(sessionId),
        ]);
        if (cancelled || wsRef.current !== ws) return;
        if (historyResult.status === "fulfilled") {
          applyPersistedMessages(historyResult.value.messages);
        }
        if (statusResult.status === "fulfilled") {
          if (statusResult.value.running) {
            isStreamingRef.current = true;
            setIsStreaming(true);
            scheduleGenerationPoll();
          } else {
            isStreamingRef.current = false;
            setIsStreaming(false);
            setStreamContent("");
            streamBufferRef.current = "";
          }
        } else {
          // A reconnect can race the backend status endpoint. Reuse the
          // bounded generation poller so a transient 5xx cannot leave the
          // panel permanently stale.
          scheduleGenerationPoll();
        }
      };

      ws.onopen = () => {
        if (cancelled || wsRef.current !== ws) return;
        reconnectAttempt = 0;
        setConnectionError(null);
        setIsConnected(true);
        // Reconcile both durable history and generation state after every
        // reconnect. A turn can finish while the socket is down, so a single
        // history read is insufficient when persistence is still in flight.
        void reconcileAfterConnect();
      };
      ws.onclose = () => {
        if (cancelled || wsRef.current !== ws) return;
        isStreamingRef.current = false;
        setIsConnected(false);
        setIsStreaming(false);
        clearGenerationPollTimer();
        streamBufferRef.current = "";
        setStreamContent("");
        if (!cancelled) {
          setConnectionError("WebSocket接続が切断されました。再接続しています");
          const delay = Math.min(1000 * 2 ** reconnectAttempt, 5000);
          reconnectAttempt += 1;
          clearReconnectTimer();
          reconnectTimer = window.setTimeout(connect, delay);
        }
      };

      ws.onmessage = (event) => {
        try {
          if (cancelled || wsRef.current !== ws) return;
          const msg = JSON.parse(event.data);
          const eventSessionId = msg.session_id ?? msg.data?.session_id;
          // Every inline event must be scoped to this panel's durable session.
          // Unscoped broadcasts are intentionally ignored rather than risking
          // cross-session display in a shared Enterprise WebSocket.
          if (!eventSessionId || String(eventSessionId) !== sessionId) return;
          if (msg.type === "stream_start") {
            setConnectionError(null);
            isStreamingRef.current = true;
            setIsStreaming(true);
            streamBufferRef.current = "";
            setStreamContent("");
          } else if (msg.type === "stream_token") {
            const token = msg.content || msg.data?.content || msg.data?.token || "";
            streamBufferRef.current += token;
            setStreamContent(streamBufferRef.current);
          } else if (msg.type === "stream_end" || msg.type === "response") {
            const finalContent =
              msg.content || msg.data?.content || streamBufferRef.current || "";
            if (finalContent) {
              setMessages((prev) => [
                ...prev,
                {
                  id: `assistant-${Date.now()}`,
                  role: "assistant",
                  content: finalContent,
                  timestamp: new Date().toLocaleTimeString(),
                },
              ]);
            }
            isStreamingRef.current = false;
            setIsStreaming(false);
            clearGenerationPollTimer();
            setStreamContent("");
            streamBufferRef.current = "";
          } else if (msg.type === "stream_cancelled") {
            const cancellationStatus =
              typeof msg.status === "string"
                ? msg.status
                : typeof msg.data?.status === "string"
                  ? msg.data.status
                  : "cancelled";
            if (cancellationStatus === "cancellation_pending") {
              isStreamingRef.current = true;
              setIsStreaming(true);
              setConnectionError(
                typeof msg.message === "string"
                  ? msg.message
                  : "停止処理を継続しています",
              );
              pollGenerationUntilIdle?.();
              return;
            }
            const finalContent =
              msg.content || msg.data?.content || msg.data?.message || streamBufferRef.current || "";
            if (finalContent) {
              setMessages((prev) => [
                ...prev,
                {
                  id: msg.message_id || msg.data?.message_id || `assistant-${Date.now()}`,
                  role: "assistant",
                  content: finalContent,
                  timestamp: new Date().toLocaleTimeString(),
                },
              ]);
            }
            isStreamingRef.current = false;
            setIsStreaming(false);
            clearGenerationPollTimer();
            if (cancellationStatus === "cancellation_failed") {
              setConnectionError(
                typeof msg.message === "string"
                  ? msg.message
                  : "応答生成を完全に停止できませんでした",
              );
            }
            setStreamContent("");
            streamBufferRef.current = "";
          } else if (msg.type === "new_message" && msg.data?.type === "assistant") {
            const content = msg.data.message || "";
            if (content && !isStreamingRef.current) {
              setMessages((prev) => [
                ...prev,
                {
                  id: `assistant-${Date.now()}`,
                  role: "assistant",
                  content,
                  timestamp: new Date().toLocaleTimeString(),
                },
              ]);
            }
          }
        } catch { /* ignore parse errors */ }
      };

      ws.onerror = () => {
        if (!cancelled && wsRef.current === ws) {
          setIsConnected(false);
        }
      };
    };

    connect();

    return () => {
      cancelled = true;
      clearReconnectTimer();
      clearGenerationPollTimer();
      pollGenerationUntilIdle = null;
      wsRef.current?.close();
      wsRef.current = null;
      isStreamingRef.current = false;
    };
  }, [sessionId]);

  // 自動スクロール
  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [messages, streamContent]);

  const sendMessage = useCallback(
    (content: string) => {
      if (!sessionId || !wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return;
      if (!content.trim()) return;

      // ファイルコンテキストを自動追加
      const contextMsg = `${content}\n\n[参照ファイル: ${fileName} (${filePath})]`;

      setMessages((prev) => [
        ...prev,
        { id: `user-${Date.now()}`, role: "user", content, timestamp: new Date().toLocaleTimeString() },
      ]);

      wsRef.current.send(
        JSON.stringify({
          type: "user_message",
          data: {
            session_id: sessionId,
            message: contextMsg,
            mentions: [{ type: "file", id: filePath, name: fileName }],
          },
        })
      );

      setInput("");
    },
    [filePath, fileName, sessionId]
  );

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendMessage(input);
    }
  };

  const askAboutSelection = useCallback(() => {
    if (!selectedText) return;
    const q = `以下の選択範囲について教えてください：\n\`\`\`\n${selectedText}\n\`\`\``;
    sendMessage(q);
  }, [selectedText, sendMessage]);

  return (
    <div className="flex h-full w-80 flex-col border-l bg-background">
      {/* ヘッダー */}
      <div className="flex items-center justify-between border-b px-3 py-2">
        <div className="flex items-center gap-1.5">
          <span className="text-sm font-medium">AI チャット</span>
          <span
            className={`size-1.5 rounded-full ${isConnected ? "bg-green-500" : "bg-red-500"}`}
            title={connectionError || (isConnected ? "接続済み" : "接続中...")}
          />
        </div>
        <Button variant="ghost" size="icon" onClick={onClose} className="size-6">
          <X className="size-3.5" />
        </Button>
      </div>

      {connectionError && (
        <div className="border-b px-3 py-1.5 text-[11px] text-destructive">
          {connectionError}
        </div>
      )}

      {/* 選択テキスト引用ボタン */}
      {selectedText && (
        <div className="border-b px-3 py-1.5">
          <Button
            variant="outline"
            size="sm"
            className="w-full gap-1 text-xs"
            onClick={askAboutSelection}
          >
            <Quote className="size-3" />
            選択範囲について質問
          </Button>
        </div>
      )}

      {/* メッセージ一覧 */}
      <div ref={scrollRef} className="flex-1 overflow-auto p-3 space-y-3">
        {messages.length === 0 && !isStreaming && (
          <div className="py-8 text-center text-xs text-muted-foreground">
            {fileName} について質問できます
          </div>
        )}
        {messages.map((msg) => (
          <div key={msg.id} className={`flex ${msg.role === "user" ? "justify-end" : "justify-start"}`}>
            <div
              className={`max-w-[90%] rounded-lg px-3 py-2 text-xs ${
                msg.role === "user"
                  ? "bg-primary text-primary-foreground"
                  : "bg-muted prose-xs prose"
              }`}
            >
              {msg.role === "assistant" ? (
                <div className="relative">
                  <ReactMarkdown remarkPlugins={[remarkGfm]}>{msg.content}</ReactMarkdown>
                  {onInsertText && (
                    <Button
                      variant="ghost"
                      size="xs"
                      className="mt-1 h-5 text-[10px]"
                      onClick={() => onInsertText(msg.content)}
                    >
                      エディタに挿入
                    </Button>
                  )}
                </div>
              ) : (
                msg.content
              )}
            </div>
          </div>
        ))}

        {/* ストリーミング表示 */}
        {isStreaming && streamContent && (
          <div className="flex justify-start">
            <div className="max-w-[90%] rounded-lg bg-muted px-3 py-2 text-xs prose-xs prose">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{streamContent}</ReactMarkdown>
            </div>
          </div>
        )}
        {isStreaming && !streamContent && (
          <div className="flex justify-start">
            <div className="rounded-lg bg-muted px-3 py-2 text-xs text-muted-foreground animate-pulse">
              考え中...
            </div>
          </div>
        )}
      </div>

      {/* 入力欄 */}
      <div className="border-t p-2">
        <div className="flex items-end gap-1">
          <textarea
            ref={inputRef}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="質問を入力..."
            rows={1}
            className="flex-1 resize-none rounded-md border bg-transparent px-2 py-1.5 text-xs outline-none focus:border-ring"
            style={{ minHeight: "32px", maxHeight: "80px" }}
          />
          <Button
            size="icon"
            className="size-7 shrink-0"
            onClick={() => sendMessage(input)}
            disabled={!input.trim() || !isConnected}
          >
            <Send className="size-3" />
          </Button>
        </div>
      </div>
    </div>
  );
}
