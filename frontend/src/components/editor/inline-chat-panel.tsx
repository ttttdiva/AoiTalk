"use client";

import { useState, useRef, useEffect, useCallback } from "react";
import { Send, X, Quote } from "lucide-react";
import { Button } from "@/components/ui/button";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

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
  const wsRef = useRef<WebSocket | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const streamBufferRef = useRef("");

  const fileName = filePath.split("/").pop() || filePath;

  // WebSocket接続
  useEffect(() => {
    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(`${protocol}//127.0.0.1:3000/ws`);

    ws.onopen = () => setIsConnected(true);
    ws.onclose = () => setIsConnected(false);

    ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data);
        if (msg.type === "stream_start") {
          setIsStreaming(true);
          streamBufferRef.current = "";
          setStreamContent("");
        } else if (msg.type === "stream_token") {
          streamBufferRef.current += msg.data?.token || "";
          setStreamContent(streamBufferRef.current);
        } else if (msg.type === "stream_end" || msg.type === "response") {
          const finalContent = streamBufferRef.current || msg.data?.message || msg.data?.content || "";
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
          setIsStreaming(false);
          setStreamContent("");
          streamBufferRef.current = "";
        } else if (msg.type === "new_message" && msg.data?.type === "assistant") {
          const content = msg.data.message || "";
          if (content && !isStreaming) {
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

    wsRef.current = ws;
    return () => ws.close();
  }, [isStreaming]);

  // 自動スクロール
  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [messages, streamContent]);

  const sendMessage = useCallback(
    (content: string) => {
      if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return;
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
            message: contextMsg,
            mentions: [{ type: "file", id: filePath, name: fileName }],
          },
        })
      );

      setInput("");
    },
    [filePath, fileName]
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
          <span className={`size-1.5 rounded-full ${isConnected ? "bg-green-500" : "bg-red-500"}`} />
        </div>
        <Button variant="ghost" size="icon" onClick={onClose} className="size-6">
          <X className="size-3.5" />
        </Button>
      </div>

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
