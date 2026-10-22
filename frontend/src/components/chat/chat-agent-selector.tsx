"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { RefreshCcw } from "lucide-react";
import { toast } from "sonner";
import {
  conversationAgentApi,
  ConversationAgentApiError,
  type ConversationAgent,
} from "@/lib/conversation-agent-api";
import { chatApi, type ConversationSession } from "@/lib/chat-api";
import { Button } from "@/components/ui/button";
import { AppSelect } from "@/components/ui/app-select";

type ChatAgentSelectorProps = {
  sessionId: string | null;
  sessionAgentId?: string | null;
  pendingAgentId?: string | null;
  projectId?: string | null;
  disabled?: boolean;
  busy?: boolean;
  isGroupChat?: boolean;
  onPendingAgentChange: (agentId: string | null) => void;
  onSessionUpdated: (session: ConversationSession) => void;
};

function agentLabel(agent: ConversationAgent): string {
  const name = agent.display_name?.trim() || agent.slug?.trim() || agent.id;
  const revision = agent.latest_revision_version
    ? ` · v${agent.latest_revision_version}`
    : "";
  return `${name}${revision}`;
}
/**
 * Session-scoped Operations Agent picker.
 *
 * Character is intentionally not part of this control. Character controls
 * display/voice; the selected Agent owns revision/team/profile routing on the
 * server. Existing Character-only and group sessions retain the empty option.
 */
export function ChatAgentSelector({
  sessionId,
  sessionAgentId = null,
  pendingAgentId = null,
  projectId = null,
  disabled = false,
  busy = false,
  isGroupChat = false,
  onPendingAgentChange,
  onSessionUpdated,
}: ChatAgentSelectorProps) {
  const [agents, setAgents] = useState<ConversationAgent[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [refreshKey, setRefreshKey] = useState(0);
  const requestGenerationRef = useRef(0);
  const bindingGenerationRef = useRef(0);
  const sessionIdRef = useRef(sessionId);
  useEffect(() => {
    sessionIdRef.current = sessionId;
    bindingGenerationRef.current += 1;
  }, [sessionId]);
  const currentValue = sessionId ? sessionAgentId : pendingAgentId;

  const loadAgents = useCallback(async (signal: AbortSignal) => {
    const generation = ++requestGenerationRef.current;
    setLoading(true);
    setError(null);
    try {
      const response = await conversationAgentApi.list({
        sessionId,
        projectId,
        signal,
      });
      if (signal.aborted || requestGenerationRef.current !== generation) return;
      setAgents(Array.isArray(response.agents) ? response.agents : []);
    } catch (cause) {
      if (signal.aborted || requestGenerationRef.current !== generation) return;
      setAgents([]);
      setError(cause instanceof Error ? cause.message : "Agent一覧を取得できませんでした");
    } finally {
      if (!signal.aborted && requestGenerationRef.current === generation) {
        setLoading(false);
      }
    }
  }, [projectId, sessionId]);

  useEffect(() => {
    const controller = new AbortController();
    void loadAgents(controller.signal);
    return () => controller.abort();
  }, [loadAgents, refreshKey]);

  const selectedAgent = useMemo(
    () => agents.find((agent) => agent.id === currentValue) ?? null,
    [agents, currentValue],
  );
  const missingCurrentBinding = Boolean(currentValue && !selectedAgent);
  const canChange = !disabled && !busy && !saving && !isGroupChat;

  const handleChange = useCallback(async (value: string) => {
    const nextAgentId = value || null;
    if (!sessionId) {
      onPendingAgentChange(nextAgentId);
      return;
    }
    if (nextAgentId === sessionAgentId) return;
    if (isGroupChat) {
      toast.info("グループチャットにはOperations Agentを紐付けられません");
      return;
    }
    if (!canChange) return;
    const bindingGeneration = ++bindingGenerationRef.current;
    const requestedSessionId = sessionId;
    setSaving(true);
    try {
      const response = await conversationAgentApi.bindSessionAgent(
        sessionId,
        nextAgentId,
        sessionAgentId ?? null,
      );
      onSessionUpdated(response.session);
      toast.success(
        nextAgentId ? "この会話のAgentを更新しました" : "この会話のAgentを解除しました",
      );
    } catch (cause) {
      if (cause instanceof ConversationAgentApiError) {
        if (cause.code === "agent_binding_busy") {
          toast.info("生成中はAgentを変更できません。完了後に再試行してください");
        } else if (cause.code === "agent_binding_stale") {
          try {
            const canonical = await chatApi.resumeSession(requestedSessionId, false);
            if (
              bindingGenerationRef.current !== bindingGeneration ||
              sessionIdRef.current !== requestedSessionId
            ) return;
            onSessionUpdated(canonical.session);
            setRefreshKey((value) => value + 1);
            toast.info("別の画面でAgentが変更されました。最新の会話設定を読み込みました");
          } catch {
            if (
              bindingGenerationRef.current === bindingGeneration &&
              sessionIdRef.current === requestedSessionId
            ) {
              toast.error("別の画面でAgentが変更されました。会話を再読み込みしてください");
            }
          }
        } else if (cause.code === "agent_not_found" || cause.code === "agent_inactive") {
          toast.error("選択したAgentは現在利用できません");
          setRefreshKey((value) => value + 1);
        } else {
          toast.error(cause.message);
        }
      } else {
        toast.error(cause instanceof Error ? cause.message : "Agentを更新できませんでした");
      }
    } finally {
      setSaving(false);
    }
  }, [canChange, isGroupChat, onPendingAgentChange, onSessionUpdated, sessionAgentId, sessionId]);

  return (
    <div className="flex min-w-0 items-center gap-2" data-testid="chat-agent-selector">
      <label className="sr-only" htmlFor="chat-agent-select">
        このChatのOperations Agent
      </label>
      <span className="shrink-0 text-[11px] text-muted-foreground" title="Character（表示・音声）とは別の、session-scopedなOperations Agentです">
        Agent
      </span>
      <AppSelect
        id="chat-agent-select"
        aria-label="このChatのOperations Agent"
        value={currentValue ?? ""}
        disabled={!canChange}
        onChange={(event) => void handleChange(event.target.value)}
        className="min-w-0 max-w-[15rem] rounded border border-border-subtle bg-background px-2 py-1 text-[11px] text-foreground disabled:cursor-not-allowed disabled:opacity-60"
        contentClassName="min-w-[16rem] max-w-[min(90vw,32rem)] [&_[role=option]]:whitespace-normal [&_[role=option]]:break-words"
        positionerClassName="max-w-[calc(100vw-1rem)]"
      >
        <option value="">Character-only（Agentなし）</option>
        {missingCurrentBinding && (
          <option value={currentValue ?? ""}>
            現在のAgent（{currentValue?.slice(0, 8)}…）
          </option>
        )}
        {agents.map((agent) => (
          <option key={agent.id} value={agent.id}>
            {agentLabel(agent)}
          </option>
        ))}
      </AppSelect>
      {(loading || saving) && <span className="text-[10px] text-muted-foreground">{saving ? "保存中…" : "読込中…"}</span>}
      {error && !loading && (
        <Button
          type="button"
          variant="ghost"
          size="icon-sm"
          className="size-6"
          title={error}
          aria-label="Agent一覧を再取得"
          onClick={() => setRefreshKey((value) => value + 1)}
        >
          <RefreshCcw className="size-3.5" />
        </Button>
      )}
      {!sessionId && (
        <span className="hidden text-[10px] text-muted-foreground sm:inline">
          新しいsessionに適用
        </span>
      )}
      {sessionId && !isGroupChat && missingCurrentBinding && (
        <span className="text-[10px] text-amber-600 dark:text-amber-300" role="status">
          Agent情報を確認できません
        </span>
      )}
      {sessionId && currentValue && !missingCurrentBinding && (
        <span className="hidden max-w-[22rem] truncate text-[10px] text-muted-foreground lg:inline" title="Agent管理の職務・Execution Team・Profileを正本として次の依頼から反映します">
          職務 / Execution Team / ProfileはAgent管理が正本（次の依頼から反映）
        </span>
      )}
      {isGroupChat && (
        <span className="text-[10px] text-muted-foreground" role="status">
          group session
        </span>
      )}
    </div>
  );
}

