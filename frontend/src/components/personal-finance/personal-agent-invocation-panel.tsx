"use client";

import { useLayoutEffect, useMemo, useRef, useState } from "react";
import { useChatSessionsOptional } from "@/contexts/chat-session-context";
import { useCurrentUserId } from "@/components/providers/swr-global-provider";
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
}: PersonalAgentInvocationPanelProps) {
  const currentUserId = useCurrentUserId();
  const ownerKey = currentUserId?.trim() || "";
  const chatSessions = useChatSessionsOptional();
  const sessions = useMemo(
    () => (chatSessions?.sessions ?? []).filter((session) => Boolean(
      session.user_id === ownerKey && session.agent_id && !session.is_group_chat && !session.parent_session_id,
    )),
    [chatSessions?.sessions, ownerKey],
  );
  const [sessionId, setSessionId] = useState("");
  const [busy, setBusy] = useState(false);
  const [response, setResponse] = useState<InvokeResponse | null>(null);
  const [error, setError] = useState("");
  const ownerRef = useRef(ownerKey);
  const requestEpoch = useRef(0);

  useLayoutEffect(() => {
    if (ownerRef.current === ownerKey) return;
    ownerRef.current = ownerKey;
    requestEpoch.current += 1;
    setSessionId("");
    setBusy(false);
    setResponse(null);
    setError("");
  }, [ownerKey]);

  const selectedSessionId = sessions.some((session) => session.id === sessionId) ? sessionId : "";

  async function invokeSelectedSession() {
    if (!selectedSessionId || busy || !canInvoke) return;
    const ownerAtStart = ownerKey;
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
      if (ownerRef.current === ownerAtStart && requestEpoch.current === epochAtStart) {
        setResponse(result);
      }
    } catch (cause) {
      if (ownerRef.current === ownerAtStart && requestEpoch.current === epochAtStart) {
        setError(cause instanceof Error && cause.message ? cause.message : "personal adapter を実行できませんでした");
      }
    } finally {
      if (ownerRef.current === ownerAtStart && requestEpoch.current === epochAtStart) {
        setBusy(false);
      }
    }
  }

  return (
    <section className="space-y-3 rounded-lg border border-slate-300 p-4" aria-labelledby={`${operation}-agent-title`}>
      <div>
        <h2 id={`${operation}-agent-title`} className="font-semibold">{title}</h2>
        <p className="text-xs text-muted-foreground">
          本人が選んだ private session の current Agent binding だけを使う adapter-only 操作です。model生成・自動起動・Agent登録は行いません。
        </p>
      </div>
      <label className="grid max-w-xl gap-1">
        実行対象の本人用 private Agent session
        {/* eslint-disable-next-line no-restricted-syntax -- this owner-only session picker keeps native keyboard/form behavior. */}
        <select
          aria-label={`${title}のprivate session`}
          value={selectedSessionId}
          disabled={busy || sessions.length === 0}
          onChange={(event) => {
            setSessionId(event.target.value);
            setResponse(null);
            setError("");
          }}
        >
          <option value="">本人が選択してください</option>
          {sessions.map((session) => (
            <option key={session.id} value={session.id}>
              {session.title || session.character_name || "private session"}・{session.id.slice(-6)}
            </option>
          ))}
        </select>
      </label>
      {sessions.length === 0 && (
        <p role="status">Agent binding がある本人用 private session がありません。Chatで本人が選択した session を用意してください。</p>
      )}
      {disabledReason && <p role="status">{disabledReason}</p>}
      <Button
        type="button"
        variant="outline"
        className="h-auto max-w-full justify-start whitespace-normal break-words py-2 text-left leading-snug"
        disabled={busy || !sessionId || !canInvoke}
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
