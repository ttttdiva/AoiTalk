"use client";

import { useEffect, useRef, useState } from "react";
import useSWR, { useSWRConfig } from "swr";
import { useRouter } from "next/navigation";
import { ExternalLink, MessageSquare, RefreshCcw } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { HelpPopover } from "@/components/ui/help-popover";
import { AgentRunDetailSheet, type AgentRunDetailView } from "@/components/chat/agent-run-detail-sheet";
import type { AgentRun } from "@/lib/chat-api";
import {
  conversationAgentApi,
  unwrapAgentConversations,
  unwrapAgentRuns,
} from "@/lib/conversation-agent-api";
import { chatApi } from "@/lib/chat-api";
import { createRegularNewChatSession } from "@/lib/create-regular-new-chat-session";
import { useCurrentUserId } from "@/components/providers/swr-global-provider";
import { useChatSessionsOptional } from "@/contexts/chat-session-context";
import { PUBLIC_BASE_PATH, withBasePath } from "@/lib/base-path";
import { formatTimestamp } from "@/lib/agent-run-timeline-format";

type EmployeeChatActivityProps = {
  agentId: string;
  agentName?: string;
  agentState?: string | null;
};

function formatDate(value?: string | null): string {
  if (!value) return "日時不明";
  return formatTimestamp(value) || "日時不明";
}
function runStatus(run: AgentRun): string {
  if (run.status === "succeeded" || run.status === "completed") return "完了";
  if (run.status === "failed") return "失敗";
  if (run.status === "cancelled") return "停止";
  return run.status || "進行中";
}

function agentActivityHref(agentId: string): string {
  return `/operations?tab=agents&agent=${encodeURIComponent(agentId)}&panel=activity`;
}

function authenticatedScopeKey(userId: string | null | undefined): string | null {
  if (!userId) return null;
  const origin = typeof window === "undefined" ? "server" : window.location.origin;
  return `${origin}${PUBLIC_BASE_PATH}:user:${userId}`;
}

export function EmployeeChatActivity({ agentId, agentName, agentState }: EmployeeChatActivityProps) {
  const router = useRouter();
  const userId = useCurrentUserId();
  const chatSessions = useChatSessionsOptional();
  const { mutate: mutateSWR } = useSWRConfig();
  const [creating, setCreating] = useState(false);
  const [detailViews, setDetailViews] = useState<AgentRunDetailView[]>([]);
  const [detailRun, setDetailRun] = useState<AgentRun | null>(null);
  const [sessionsScopeError, setSessionsScopeError] = useState<unknown>(null);
  const [runsScopeError, setRunsScopeError] = useState<unknown>(null);
  const authScopeKey = authenticatedScopeKey(userId);
  const sessionsKey = authScopeKey
    ? ["conversation-agent-sessions", authScopeKey, agentId]
    : null;
  const runsKey = authScopeKey
    ? ["conversation-agent-runs", authScopeKey, agentId]
    : null;
  const previousScopeRef = useRef<string | null>(authScopeKey);
  useEffect(() => {
    const previousScope = previousScopeRef.current;
    if (previousScope && previousScope !== authScopeKey) {
      setSessionsScopeError(null);
      setRunsScopeError(null);
      void mutateSWR(["conversation-agent-sessions", previousScope, agentId], undefined, {
        revalidate: false,
      });
      void mutateSWR(["conversation-agent-runs", previousScope, agentId], undefined, {
        revalidate: false,
      });
    }
    previousScopeRef.current = authScopeKey;
  }, [agentId, authScopeKey, mutateSWR]);
  const sessions = useSWR(
    sessionsKey,
    () => conversationAgentApi.listAgentConversations(agentId),
    {
      shouldRetryOnError: false,
      keepPreviousData: false,
      revalidateOnMount: true,
    },
  );
  const runs = useSWR(
    runsKey,
    () => conversationAgentApi.listAgentRuns(agentId, { limit: 50 }),
    {
      shouldRetryOnError: false,
      keepPreviousData: false,
      revalidateOnMount: true,
    },
  );
  useEffect(() => {
    if (sessions.error) {
      setSessionsScopeError(sessions.error);
    } else if (sessions.data && !sessions.isValidating) {
      setSessionsScopeError(null);
    }
  }, [sessions.data, sessions.error, sessions.isValidating]);
  useEffect(() => {
    if (runs.error) {
      setRunsScopeError(runs.error);
    } else if (runs.data && !runs.isValidating) {
      setRunsScopeError(null);
    }
  }, [runs.data, runs.error, runs.isValidating]);
  useEffect(() => {
    if (sessions.error && authScopeKey) {
      void mutateSWR(["conversation-agent-sessions", authScopeKey, agentId], undefined, {
        revalidate: false,
      });
    }
  }, [agentId, authScopeKey, mutateSWR, sessions.error]);
  useEffect(() => {
    if (runs.error && authScopeKey) {
      void mutateSWR(["conversation-agent-runs", authScopeKey, agentId], undefined, {
        revalidate: false,
      });
    }
  }, [agentId, authScopeKey, mutateSWR, runs.error]);
  const sessionsLoading = Boolean(authScopeKey) && (sessions.isLoading || sessions.isValidating);
  const runsLoading = Boolean(authScopeKey) && (runs.isLoading || runs.isValidating);
  // SWR may retain a same-key value while a revalidation is in flight or after
  // a 403. Hide it until the current authenticated scope has a fresh response.
  const relatedSessions =
    authScopeKey && !sessionsScopeError && !sessionsLoading && sessions.data
      ? unwrapAgentConversations(sessions.data)
      : [];
  const relatedRuns =
    authScopeKey && !runsScopeError && !runsLoading && runs.data
      ? unwrapAgentRuns(runs.data)
      : [];
  const canCreate = agentState === undefined || agentState === "active";

  const createChat = async () => {
    if (creating || !canCreate) return;
    setCreating(true);
    try {
      const characterName = await chatApi.getCurrentCharacterName();
      const created = await createRegularNewChatSession({
        characterName,
        userId,
        agentId,
      });
      chatSessions?.upsertSession(created.session);
      router.push(`/chat?s=${encodeURIComponent(created.session.id)}`);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "AgentとChatを作成できませんでした");
    } finally {
      setCreating(false);
    }
  };

  const openRun = (run: AgentRun) => {
    setDetailRun(run);
    setDetailViews([{ kind: "run", runId: run.id }]);
  };

  return (
    <section className="space-y-4 rounded-lg border border-primary/30 bg-primary/5 p-4" data-testid="employee-chat-activity">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h3 className="inline-flex items-center gap-1 font-semibold">
            このAgentとChat
            <HelpPopover label="このAgentとChat">
              {agentName || "このAgent"}に紐づくChatと実行履歴です。Character（表示・音声）とは別に、Agentの職務・Execution Team・Profileを次の依頼から適用します。
            </HelpPopover>
          </h3>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button type="button" onClick={() => void createChat()} disabled={creating || !canCreate}>
            <MessageSquare className="mr-1.5 size-4" />
            {creating ? "作成中…" : canCreate ? "このAgentと新規Chat" : "Agentが有効ではありません"}
          </Button>
          <Button
            type="button"
            variant="outline"
            onClick={() => { void sessions.mutate(); void runs.mutate(); }}
            aria-label="AgentのChat活動を再取得"
          >
            <RefreshCcw className="mr-1.5 size-4" />
            更新
          </Button>
        </div>
      </header>

      {Boolean(sessionsScopeError || runsScopeError) && Boolean(authScopeKey) && (
        <p role="alert" className="rounded border border-destructive/40 bg-destructive/10 p-2 text-sm">
          Chat活動を取得できませんでした。権限またはAgentの状態を確認してください。
        </p>
      )}

      <div className="grid gap-4 lg:grid-cols-2">
        <div className="space-y-2">
          <div className="flex items-center justify-between gap-2">
            <h4 className="text-sm font-medium">関連Chat（{relatedSessions.length}）</h4>
            <a className="text-xs text-primary underline" href={withBasePath(`/chat?agent_id=${encodeURIComponent(agentId)}`)}>
              Chatで開く
            </a>
          </div>
          {sessionsLoading && <p className="text-sm text-muted-foreground">読み込み中…</p>}
          {!sessionsLoading && authScopeKey && !sessionsScopeError && relatedSessions.length === 0 && (
            <p className="rounded border border-dashed p-3 text-sm text-muted-foreground">このAgentのChatはまだありません。</p>
          )}
          {relatedSessions.map((session) => (
            <a
              key={session.id}
              href={withBasePath(`/chat?s=${encodeURIComponent(session.id)}`)}
              className="block rounded border bg-background p-3 transition-colors hover:border-primary/60"
            >
              <span className="flex items-center gap-2 text-sm font-medium">
                <MessageSquare className="size-3.5 text-muted-foreground" />
                <span className="min-w-0 flex-1 truncate">{session.title || "無題のChat"}</span>
                <ExternalLink className="size-3.5 shrink-0 text-muted-foreground" />
              </span>
              <span className="mt-1 block text-xs text-muted-foreground">
                {session.character_name} · {formatDate(session.last_activity || session.session_start)}
              </span>
            </a>
          ))}
        </div>

        <div className="space-y-2">
          <div className="flex items-center justify-between gap-2">
            <h4 className="text-sm font-medium">関連活動（{relatedRuns.length}）</h4>
            <a className="text-xs text-primary underline" href={withBasePath(agentActivityHref(agentId))}>
              Operationsで開く
            </a>
          </div>
          {runsLoading && <p className="text-sm text-muted-foreground">読み込み中…</p>}
          {!runsLoading && authScopeKey && !runsScopeError && relatedRuns.length === 0 && (
            <p className="rounded border border-dashed p-3 text-sm text-muted-foreground">実行履歴はまだありません。</p>
          )}
          {relatedRuns.map((run) => (
            <div key={run.id} className="rounded border bg-background p-3">
              <div className="flex items-start gap-2">
                <button type="button" className="min-w-0 flex-1 text-left" onClick={() => openRun(run)}>
                  <span className="block truncate text-sm font-medium">{run.title || run.objective || "Agent実行"}</span>
                  <span className="mt-1 block text-xs text-muted-foreground">{runStatus(run)} · {formatDate(run.created_at)}</span>
                </button>
                <div className="flex shrink-0 flex-wrap gap-2">
                  {run.agent_id && (
                    <a
                      className="text-xs text-primary underline"
                      href={withBasePath(agentActivityHref(run.agent_id))}
                    >
                      Agentを開く
                    </a>
                  )}
                  {run.work_item_id && (
                    <a
                      className="text-xs text-primary underline"
                      href={withBasePath(`/operations?tab=work&work_id=${encodeURIComponent(run.work_item_id)}`)}
                    >
                      Workを開く
                    </a>
                  )}
                </div>
              </div>
              {run.session_id && (
                <a className="mt-2 block text-xs text-primary underline" href={withBasePath(`/chat?s=${encodeURIComponent(run.session_id)}`)}>
                  関連Chatを開く
                </a>
              )}
            </div>
          ))}
        </div>
      </div>

      <AgentRunDetailSheet
        key={detailViews.map((view) => `${view.kind}:${view.runId}:${"itemId" in view ? view.itemId : ""}`).join("|") || "closed"}
        open={detailViews.length > 0}
        onOpenChange={(open) => { if (!open) { setDetailViews([]); setDetailRun(null); } }}
        views={detailViews}
        fallbackRun={detailRun}
      />
    </section>
  );
}

