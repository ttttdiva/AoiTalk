"use client";

import { useLayoutEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { useCurrentUserId } from "@/components/providers/swr-global-provider";
import {
  personalAgentOwnerApi,
  type PersonalOwnerAgentSession,
} from "@/components/personal-agents/personal-agent-owner-api";
import { Button } from "@/components/ui/button";
import type { components } from "@/lib/api-types.gen";

type InvokeRequest = components["schemas"]["PersonalAgentInvokeRequest"];
type InvokeResponse = components["schemas"]["PersonalAgentInvokeResponse"];

export type PersonalAgentInvocationPanelProps = {
  title: string;
  profileId: InvokeRequest["profile_id"];
  operation: InvokeRequest["operation"];
  actionLabel: string;
  invoke: (body: InvokeRequest) => Promise<InvokeResponse>;
  getArguments: () => Record<string, unknown>;
  canInvoke?: boolean;
  disabledReason?: string;
  onSuccess?: (response: InvokeResponse) => void;
};

/**
 * Owner selected entry point for the personal adapter.
 *
 * Session IDs come only from the authenticated owner's existing private chat
 * sessions. The panel never asks for an Agent, model, route, or owner value,
 * and it labels the response as adapter-only because no model generation is
 * performed by this endpoint.
 */
export function PersonalAgentInvocationPanel({
  title,
  profileId,
  operation,
  actionLabel,
  invoke,
  getArguments,
  canInvoke = true,
  disabledReason,
  onSuccess,
}: PersonalAgentInvocationPanelProps) {
  const currentUserId = useCurrentUserId();
  const ownerKey = currentUserId?.trim() || "";
  const [ownerAgentIds, setOwnerAgentIds] = useState<Set<string>>(new Set());
  const [candidateSessions, setSessions] = useState<PersonalOwnerAgentSession[]>([]);
  const [ownerAgentsLoading, setOwnerAgentsLoading] = useState(true);
  const [ownerAgentsError, setOwnerAgentsError] = useState("");
  const ownerAgentRead = useRef(0);
  useLayoutEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- clear the previous owner's Agent ids before filtering sessions.
    setOwnerAgentIds(new Set());
    setSessions([]);
    setOwnerAgentsLoading(true);
    setOwnerAgentsError("");
    if (!ownerKey) {
      setOwnerAgentsLoading(false);
      return () => { ownerAgentRead.current += 1; };
    }
    let disposed = false;
    let inFlight = false;
    let initialRead = true;
    const refresh = async () => {
      if (disposed || inFlight) return;
      inFlight = true;
      const read = ++ownerAgentRead.current;
      try {
        const agents = await personalAgentOwnerApi.list();
        if (disposed || ownerAgentRead.current !== read) return;
        const profileAgentIds = new Set(
          agents
            .filter((agent) => agent.profile_id === profileId)
            .map((agent) => agent.agent_id),
        );
        setOwnerAgentIds(profileAgentIds);
        const candidates = profileAgentIds.size > 0
          ? await personalAgentOwnerApi.listSessions(profileId)
          : [];
        if (disposed || ownerAgentRead.current !== read) return;
        setSessions(candidates.filter((session) =>
          session.user_id === ownerKey && profileAgentIds.has(session.agent_id),
        ));
        setOwnerAgentsError("");
      } catch (cause) {
        if (!disposed && ownerAgentRead.current === read) {
          setOwnerAgentIds(new Set());
          setSessions([]);
          setOwnerAgentsError(cause instanceof Error ? cause.message : "本人用Agentを取得できませんでした");
        }
      } finally {
        inFlight = false;
        if (!disposed && initialRead) {
          initialRead = false;
          setOwnerAgentsLoading(false);
        }
      }
    };
    const refreshIfVisible = () => {
      if (document.visibilityState !== "hidden") void refresh();
    };
    void refresh();
    const refreshTimer = window.setInterval(refreshIfVisible, 15_000);
    document.addEventListener("visibilitychange", refreshIfVisible);
    window.addEventListener("focus", refreshIfVisible);
    return () => {
      disposed = true;
      ownerAgentRead.current += 1;
      window.clearInterval(refreshTimer);
      document.removeEventListener("visibilitychange", refreshIfVisible);
      window.removeEventListener("focus", refreshIfVisible);
    };
  }, [ownerKey, profileId]);
  const sessions = useMemo(
    () => candidateSessions.filter((session) => Boolean(
      session.user_id === ownerKey && session.agent_id && ownerAgentIds.has(session.agent_id),
    )),
    [candidateSessions, ownerAgentIds, ownerKey],
  );
  const [sessionId, setSessionId] = useState("");
  const [busy, setBusy] = useState(false);
  const [response, setResponse] = useState<InvokeResponse | null>(null);
  const [error, setError] = useState("");
  const selectedSession = sessions.find((session) => session.id === sessionId);
  const selectedSessionId = selectedSession?.id ?? "";
  const invocationScope = JSON.stringify([
    ownerKey,
    profileId,
    selectedSessionId,
    selectedSession?.agent_id ?? "",
    selectedSession?.agent_binding_updated_at ?? "",
  ]);
  const ownerRef = useRef(ownerKey);
  const invocationScopeRef = useRef(invocationScope);
  const requestEpoch = useRef(0);

  useLayoutEffect(() => {
    if (invocationScopeRef.current === invocationScope) return;
    const nextSessionId = ownerRef.current === ownerKey ? selectedSessionId : "";
    ownerRef.current = ownerKey;
    invocationScopeRef.current = invocationScope;
    requestEpoch.current += 1;
    setSessionId(nextSessionId);
    setBusy(false);
    setResponse(null);
    setError("");
  }, [invocationScope, ownerKey, profileId, selectedSessionId]);

  async function invokeSelectedSession() {
    if (!selectedSessionId || busy || !canInvoke) return;
    const scopeAtStart = invocationScopeRef.current;
    const epochAtStart = ++requestEpoch.current;
    setBusy(true);
    setError("");
    setResponse(null);
    try {
      const result = await invoke({
        session_id: selectedSessionId,
        profile_id: profileId,
        operation,
        arguments: getArguments(),
      });
      if (invocationScopeRef.current === scopeAtStart && requestEpoch.current === epochAtStart) {
        setResponse(result);
        onSuccess?.(result);
      }
    } catch (cause) {
      if (invocationScopeRef.current === scopeAtStart && requestEpoch.current === epochAtStart) {
        setError(cause instanceof Error && cause.message ? cause.message : "personal adapter を実行できませんでした");
      }
    } finally {
      if (invocationScopeRef.current === scopeAtStart && requestEpoch.current === epochAtStart) {
        setBusy(false);
      }
    }
  }

  return (
    <section className="space-y-3 rounded-lg border border-slate-300 p-4" aria-labelledby={`${operation}-agent-title`}>
      <div>
        <h2 id={`${operation}-agent-title`} className="font-semibold">{title}</h2>
        <p className="text-xs text-muted-foreground">
          本人が選んだ private session の current Agent binding だけを使う adapter-only 操作です。この操作はmodelを呼び出さず、自動起動もしません。
        </p>
      </div>
      <label className="grid max-w-xl gap-1">
        実行対象の本人用 private Agent session
        {/* eslint-disable-next-line no-restricted-syntax -- this owner-only session picker keeps native keyboard/form behavior. */}
        <select
          aria-label={`${title}のprivate session`}
          value={selectedSessionId}
          disabled={busy || sessions.length === 0}
          onChange={(event) => setSessionId(event.target.value)}
        >
          <option value="">本人が選択してください</option>
          {sessions.map((session) => (
            <option key={session.id} value={session.id}>
              {session.title || session.character_name || "private session"}・{session.id.slice(-6)}
            </option>
          ))}
        </select>
      </label>
      {ownerAgentsLoading && <p role="status">本人用Agentを確認中…</p>}
      {ownerAgentsError && <p role="alert">{ownerAgentsError}</p>}
      {!ownerAgentsLoading && sessions.length === 0 && (
        <div role="status" className="space-y-1">
          <p>{ownerAgentIds.size === 0 ? "本人用のpersonal Agentがありません。" : "personal Agentを選択した本人用private sessionがありません。"}</p>
          <Link className="inline-flex min-h-11 items-center underline underline-offset-4" href="/personal-agent">
            個人Agentを作成・更新する
          </Link>
          <p className="text-xs text-muted-foreground">作成後、Chatで本人用private sessionにAgentを選択してください。</p>
        </div>
      )}
      {disabledReason && <p role="status">{disabledReason}</p>}
      <Button
        type="button"
        variant="outline"
        className="h-auto max-w-full justify-start whitespace-normal break-words py-2 text-left leading-snug"
        disabled={busy || !selectedSessionId || !canInvoke}
        onClick={() => void invokeSelectedSession()}
      >
        {busy ? "adapterを実行中…" : actionLabel}
      </Button>
      {error && <p role="alert">{error}</p>}
      {response && (
        <div className="space-y-1 rounded border bg-muted/20 p-3" data-testid={`${operation}-agent-result`}>
          <p role="status">adapter-only 実行結果（generative_response=false）</p>
          <p>AgentRevision: {response.agent_revision_id}</p>
          <p>binding: {response.binding_id}</p>
          <pre className="max-h-64 overflow-auto whitespace-pre-wrap text-xs">
            {typeof response.result === "string" ? response.result : JSON.stringify(response.result, null, 2)}
          </pre>
        </div>
      )}
    </section>
  );
}
