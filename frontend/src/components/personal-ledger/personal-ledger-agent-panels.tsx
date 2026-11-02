"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useCurrentUserId } from "@/components/providers/swr-global-provider";
import { PersonalAgentInvocationPanel } from "@/components/personal-finance/personal-agent-invocation-panel";
import { personalFinanceApi } from "@/components/personal-finance/personal-finance-api";
import { Button } from "@/components/ui/button";
import { personalDraftApi, type PersonalDraftEvidencePage } from "./personal-draft-api";

type SavedEvidence = PersonalDraftEvidencePage["items"][number];
type Kind = SavedEvidence["proposal"]["kind"];

function label(item: SavedEvidence): string {
  const data = item.proposal.data as Record<string, unknown>;
  const subject = String(data.subject_key ?? "候補");
  return item.proposal.kind === "deadline"
    ? `${subject} · ${String(data.due_date ?? "日付未設定")}`
    : `${subject} · ${String(data.quantity ?? "数量未設定")} ${String(data.unit ?? "")}`;
}

function ProposalInput({ kind, items, selected, onSelect }: {
  kind: Kind; items: SavedEvidence[]; selected: string; onSelect: (value: string) => void;
}) {
  const candidates = items.filter((item) => item.proposal.kind === kind);
  const chosen = candidates.find((item) => item.evidence_ref === selected);
  return (
    <div className="max-w-xl space-y-2">
      <p className="text-sm font-medium">本人が保存した未提案の根拠を選択</p>
      {candidates.length ? (
        <ul className="grid min-w-0 gap-2 sm:grid-cols-2" aria-label={`${kind === "deadline" ? "期限" : "在庫"}の保存済み根拠`}>
          {candidates.map((item) => <li key={item.evidence_ref} className="min-w-0">
            <button type="button" aria-pressed={chosen?.evidence_ref === item.evidence_ref}
              className="min-h-11 w-full min-w-0 rounded border p-2 text-left focus-visible:outline-2 focus-visible:outline-offset-2 aria-pressed:border-primary [overflow-wrap:anywhere]"
              onClick={() => onSelect(item.evidence_ref)}>{label(item)}</button>
          </li>)}
        </ul>
      ) : <p className="text-xs text-muted-foreground">選択できる根拠はありません。</p>}
      {chosen ? (
        <details className="rounded border p-2 text-sm">
          <summary className="min-h-11 cursor-pointer py-2">選択した根拠を確認</summary>
          <pre className="max-h-48 overflow-auto whitespace-pre-wrap [overflow-wrap:anywhere]">{chosen.source_text}</pre>
        </details>
      ) : (
        <p className="text-xs text-muted-foreground">先に「本人が選ぶ根拠を保存」から根拠を保存してください。既に提案済みの根拠は選択肢に出ません。</p>
      )}
    </div>
  );
}

export function PersonalLedgerAgentPanels({ refreshRequest = 0, onProposed }: {
  refreshRequest?: number;
  onProposed?: () => void;
}) {
  const currentUserId = useCurrentUserId();
  const ownerKey = currentUserId?.trim() ?? "";
  const [items, setItems] = useState<PersonalDraftEvidencePage["items"]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [deadlineRef, setDeadlineRef] = useState("");
  const [inventoryRef, setInventoryRef] = useState("");
  const requestEpoch = useRef(0);
  const keysByEvidence = useRef(new Map<string, string>());

  const reload = useCallback(async () => {
    const epoch = ++requestEpoch.current;
    setItems([]);
    setDeadlineRef("");
    setInventoryRef("");
    if (!ownerKey) return;
    setLoading(true);
    setError("");
    try {
      const page = await personalDraftApi.evidence();
      if (requestEpoch.current !== epoch) return;
      setItems(page.items);
    } catch (cause) {
      if (requestEpoch.current === epoch) setError(cause instanceof Error ? cause.message : "根拠一覧を取得できませんでした");
    } finally {
      if (requestEpoch.current === epoch) setLoading(false);
    }
  }, [ownerKey]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- hydrate the owner-scoped saved evidence list.
    void reload();
    return () => { requestEpoch.current += 1; };
  }, [reload, refreshRequest]);

  const evidenceArgs = (ref: string) => {
    const item = items.find((candidate) => candidate.evidence_ref === ref);
    if (!item) return {};
    let key = keysByEvidence.current.get(ref);
    if (!key) {
      key = crypto.randomUUID();
      keysByEvidence.current.set(ref, key);
    }
    return { evidence_ref: ref, proposal: item.proposal, idempotency_key: key };
  };

  const proposed = () => {
    setDeadlineRef("");
    setInventoryRef("");
    void reload();
    onProposed?.();
  };

  return (
    <section className="space-y-4" aria-label="個人Agentの台帳操作" data-testid="personal-ledger-agents">
      <header className="space-y-1">
        <h2 className="text-lg font-semibold">個人Agent</h2>
        <p className="text-sm text-muted-foreground">本人のprivate sessionを選び、台帳状態を読み取るか、本人が保存した根拠から未受理の候補を作成します。候補の受理は下の「候補の確認」で本人が行います。</p>
        <Button type="button" size="sm" variant="outline" className="min-h-11" disabled={loading} onClick={() => void reload()}>
          保存済み根拠を再取得
        </Button>
        {loading && <p role="status">保存済み根拠を取得中…</p>}
        {error && <p role="alert">{error}</p>}
      </header>

      <PersonalAgentInvocationPanel title="期限の状態" profileId="personal-firearms-admin" operation="deadline.state"
        actionLabel="期限台帳を読む" invoke={personalFinanceApi.invokePersonalAgent} getArguments={() => ({})} />
      <PersonalAgentInvocationPanel title="期限候補を作成" profileId="personal-firearms-admin" operation="deadline.propose"
        actionLabel="選択した根拠から期限候補を作成" invoke={personalFinanceApi.invokePersonalAgent}
        getArguments={() => evidenceArgs(deadlineRef)} canInvoke={!!deadlineRef && !loading}
        disabledReason={!deadlineRef ? "本人が保存した期限の根拠を選択してください。" : undefined} onSuccess={proposed} />
      <ProposalInput kind="deadline" items={items} selected={deadlineRef} onSelect={setDeadlineRef} />

      <PersonalAgentInvocationPanel title="在庫の状態" profileId="personal-ammunition-bookkeeper" operation="inventory.state"
        actionLabel="在庫台帳を読む" invoke={personalFinanceApi.invokePersonalAgent} getArguments={() => ({})} />
      <PersonalAgentInvocationPanel title="在庫候補を作成" profileId="personal-ammunition-bookkeeper" operation="inventory.propose"
        actionLabel="選択した根拠から在庫候補を作成" invoke={personalFinanceApi.invokePersonalAgent}
        getArguments={() => evidenceArgs(inventoryRef)} canInvoke={!!inventoryRef && !loading}
        disabledReason={!inventoryRef ? "本人が保存した在庫の根拠を選択してください。" : undefined} onSuccess={proposed} />
      <ProposalInput kind="inventory" items={items} selected={inventoryRef} onSelect={setInventoryRef} />
    </section>
  );
}
