"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useCurrentUserId } from "@/components/providers/swr-global-provider";
import { AppSelect } from "@/components/ui/app-select";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  personalAgentOwnerApi,
  type PersonalAgentOwnerAgentSaveRequest,
  type PersonalAgentProfileId,
  type PersonalOwnerAgent,
} from "./personal-agent-owner-api";

const PROFILES: Array<{ id: PersonalAgentProfileId; label: string; description: string }> = [
  { id: "personal-firearms-admin", label: "期限台帳", description: "期限の状態確認と未受理候補の作成" },
  { id: "personal-ammunition-bookkeeper", label: "在庫台帳", description: "在庫の状態確認と未受理候補の作成" },
  { id: "personal-finance-reviewer", label: "個人財務", description: "既に本人が受理した財務データの読取" },
  { id: "personal-career-planner", label: "キャリア", description: "本人が受理したプロフィールの読取" },
];

type PendingSave = { agentId?: string; body: PersonalAgentOwnerAgentSaveRequest };

export function PersonalAgentOwnerBuilder() {
  const ownerId = useCurrentUserId()?.trim() ?? "";
  return <PersonalAgentOwnerBuilderContent key={ownerId || "anonymous"} ownerId={ownerId} />;
}

function PersonalAgentOwnerBuilderContent({ ownerId }: { ownerId: string }) {
  const [agents, setAgents] = useState<PersonalOwnerAgent[]>([]);
  const [selectedId, setSelectedId] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [profileId, setProfileId] = useState<PersonalAgentProfileId>("personal-firearms-admin");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState("");
  const [pendingSave, setPendingSave] = useState<PendingSave | null>(null);
  const epoch = useRef(0);
  const selectedAgent = agents.find((item) => item.agent_id === selectedId);

  const reload = useCallback(async () => {
    const request = ++epoch.current;
    setLoading(true);
    setMessage("");
    try {
      const rows = ownerId ? await personalAgentOwnerApi.list() : [];
      if (epoch.current !== request) return;
      setAgents(rows);
      setSelectedId((current) => current && !rows.some((row) => row.agent_id === current) ? "" : current);
    } catch (error) {
      if (epoch.current === request) setMessage(error instanceof Error ? error.message : "個人Agent一覧を取得できません");
    } finally {
      if (epoch.current === request) setLoading(false);
    }
  }, [ownerId]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- hydrate this owner's safe Agent projection.
    void reload();
    return () => { epoch.current += 1; };
  }, [reload]);

  function selectAgent(id: string) {
    const next = agents.find((agent) => agent.agent_id === id);
    setSelectedId(id);
    setDisplayName(next?.display_name ?? "");
    setProfileId(next?.profile_id ?? "personal-firearms-admin");
    setPendingSave(null);
  }

  async function save() {
    if (saving || loading || !ownerId) return;
    const request: PendingSave = pendingSave ?? {
      ...(selectedAgent ? { agentId: selectedAgent.agent_id } : {}),
      body: { display_name: displayName.trim(), profile_id: profileId, idempotency_key: crypto.randomUUID() },
    };
    if (!request.body.display_name) return;
    setPendingSave(request);
    setSaving(true);
    setMessage("");
    const requestEpoch = epoch.current;
    try {
      const result = await personalAgentOwnerApi.save(request.body, request.agentId);
      if (epoch.current !== requestEpoch) return;
      setPendingSave(null);
      setSelectedId(result.agent_id);
      setDisplayName(result.display_name);
      setProfileId(result.profile_id);
      setAgents((current) => [result, ...current.filter((agent) => agent.agent_id !== result.agent_id)]);
      setMessage(`保存しました（revision ${result.revision_version}）。ChatでこのAgentを選択し直すと最新revisionがbindingされます。`);
    } catch (error) {
      if (epoch.current === requestEpoch) setMessage(`${error instanceof Error ? error.message : "保存結果を確認できませんでした"}。同じ要求のキーを保持しています。`);
    } finally {
      if (epoch.current === requestEpoch) setSaving(false);
    }
  }

  return (
    <section className="mx-auto max-w-3xl space-y-5 p-4" data-testid="personal-agent-owner-builder">
      <header className="space-y-2">
        <h1 className="text-xl font-semibold">本人用の個人Agent</h1>
        <p className="text-sm text-muted-foreground">本人のAgentを作成・更新し、既存のConversation Agent bindingで自分のprivate Chat sessionに選べます。自動起動せず、選択した個人用操作のcapability ceilingだけを使います。</p>
      </header>
      <div className="space-y-3 rounded-lg border p-4">
        <h2 className="font-semibold">Agent identity / revision</h2>
        <label className="grid gap-1 text-sm">更新する本人用Agent
          <AppSelect aria-label="更新する本人用Agent" className="w-full" value={selectedId}
            disabled={loading || saving} onChange={(event) => selectAgent(event.target.value)}>
            <option value="">新しいAgentを作成</option>
            {agents.map((agent) => <option key={agent.agent_id} value={agent.agent_id}>{agent.display_name} · revision {agent.revision_version}</option>)}
          </AppSelect>
        </label>
        <label className="grid gap-1 text-sm">表示名
          <Input maxLength={160} value={displayName} disabled={saving || pendingSave !== null}
            onChange={(event) => { setDisplayName(event.target.value); setPendingSave(null); }} />
        </label>
        <label className="grid gap-1 text-sm">既存の個人profile
          <AppSelect aria-label="個人Agent profile" className="w-full" value={profileId}
            disabled={saving || pendingSave !== null} onChange={(event) => { setProfileId(event.target.value as PersonalAgentProfileId); setPendingSave(null); }}>
            {PROFILES.map((profile) => <option key={profile.id} value={profile.id}>{profile.label} — {profile.description}</option>)}
          </AppSelect>
        </label>
        <p className="text-xs text-muted-foreground">provider・model・route・tools・grantはこの画面から設定できません。Chatの会話routeは本人がlocal providerに設定してください。個人adapterの台帳操作自体はmodelを呼び出しません。</p>
        <div className="flex flex-wrap gap-2">
          <Button type="button" className="min-h-11" disabled={saving || loading || !displayName.trim()}
            onClick={() => void save()}>{saving ? "保存中…" : pendingSave ? "同じ要求で再試行" : selectedAgent ? "新revisionを保存" : "個人Agentを作成"}</Button>
          <Button type="button" variant="outline" className="min-h-11" disabled={loading || saving} onClick={() => void reload()}>一覧を再取得</Button>
        </div>
        {loading && <p role="status">本人用Agentを取得中…</p>}
        {message && <p role="status" className="[overflow-wrap:anywhere]">{message}</p>}
      </div>
      <div className="rounded-lg border p-4 text-sm">
        <h2 className="font-semibold">次の手順</h2>
        <ol className="mt-2 list-decimal space-y-1 pl-5">
          <li>Chatで本人のprivate sessionを開くか作ります。</li>
          <li>会話Agent選択で、上の本人用Agentを選択します。更新した場合は選択し直して最新revisionをbindingします。</li>
          <li>Chatの会話model routeはlocal providerに設定します。personal adapterは外部modelを呼び出しません。</li>
        </ol>
        <Link href="/chat" className="mt-3 inline-flex min-h-11 items-center underline underline-offset-4">Chatへ戻る</Link>
      </div>
    </section>
  );
}
