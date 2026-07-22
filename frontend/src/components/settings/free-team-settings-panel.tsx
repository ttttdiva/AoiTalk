"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { Loader2, RefreshCcw, Save, ShieldCheck } from "lucide-react";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { pyFetch } from "./llm-model-section-types";

type Credential = {
  id: string;
  display_name: string;
  provider: string;
  billing_mode: string;
  enabled: boolean;
  configured: boolean;
  status: string;
};

type Candidate = {
  id: string;
  provider: string;
  model: string;
  effort?: string;
  priority: number;
  enabled: boolean;
  status: string;
  max_output_tokens: number;
  cooldown_until?: string | null;
};

type QuotaPool = {
  id: string;
  metric_type: string;
  limit: number;
  consumed: number;
  reserved: number;
  available: number;
  safety_margin_ratio: number;
  safety_margin_units: number;
  window_end?: string | null;
  last_provider_sync_at?: string | null;
  status: string;
};

type ModelGroup = {
  name?: string;
  target_type?: "inherit" | "static" | "pool";
  pool_id?: string;
  provider?: string;
  model?: string;
  effort_policy?: string;
  effort?: string;
};

type FreeTeamProfile = {
  display_name: string;
  enabled: boolean;
  main_pool_id: string;
  agent_team_enabled: boolean;
  max_fallbacks: number;
  pools: Record<string, { candidate_ids?: string[] }>;
  agent_team: {
    model_groups: Record<string, ModelGroup>;
    members?: Record<string, unknown>;
  };
};

type FreeTeamResponse = {
  profile: FreeTeamProfile;
  credentials: Credential[];
  candidates: Candidate[];
  quota_pools: QuotaPool[];
};

function formatValue(value: number, metric: string): string {
  if (metric === "usd") return `$${value.toFixed(4)}`;
  return new Intl.NumberFormat("ja-JP", { maximumFractionDigits: 3 }).format(value);
}

export function FreeTeamSettingsPanel() {
  const [data, setData] = useState<FreeTeamResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [savingProfile, setSavingProfile] = useState(false);
  const [refreshingOpenRouter, setRefreshingOpenRouter] = useState(false);
  const [apiKeys, setApiKeys] = useState<Record<string, string>>({});
  const [candidateDrafts, setCandidateDrafts] = useState<Record<string, Partial<Candidate>>>({});
  const [quotaDrafts, setQuotaDrafts] = useState<Record<string, Partial<QuotaPool>>>({});

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [settings, usage] = await Promise.all([
        pyFetch<Omit<FreeTeamResponse, "quota_pools">>("/free-team/settings"),
        pyFetch<Pick<FreeTeamResponse, "quota_pools">>("/free-team/usage"),
      ]);
      setData({ ...settings, quota_pools: usage.quota_pools });
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "無料Team設定を取得できませんでした");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const poolIds = useMemo(() => Object.keys(data?.profile.pools ?? {}), [data]);

  const saveCredential = async (credential: Credential) => {
    const apiKey = apiKeys[credential.id]?.trim();
    try {
      await pyFetch(`/free-team/credentials/${encodeURIComponent(credential.id)}`, {
        method: "PUT",
        body: JSON.stringify({
          enabled: credential.enabled,
          ...(apiKey ? { api_key: apiKey } : {}),
        }),
      });
      setApiKeys((current) => ({ ...current, [credential.id]: "" }));
      toast.success(`${credential.display_name} を保存しました`);
      await load();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "認証設定を保存できませんでした");
    }
  };

  const saveCandidate = async (candidate: Candidate) => {
    const draft = candidateDrafts[candidate.id] ?? {};
    try {
      await pyFetch(`/free-team/candidates/${encodeURIComponent(candidate.id)}`, {
        method: "PATCH",
        body: JSON.stringify({
          enabled: draft.enabled ?? candidate.enabled,
          priority: draft.priority ?? candidate.priority,
          effort: draft.effort ?? candidate.effort ?? "",
          max_output_tokens: draft.max_output_tokens ?? candidate.max_output_tokens,
        }),
      });
      toast.success(`${candidate.id} を保存しました`);
      await load();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "候補設定を保存できませんでした");
    }
  };

  const saveQuota = async (quota: QuotaPool) => {
    const draft = quotaDrafts[quota.id] ?? {};
    try {
      await pyFetch(`/free-team/quota-pools/${encodeURIComponent(quota.id)}`, {
        method: "PATCH",
        body: JSON.stringify({
          limit: draft.limit ?? quota.limit,
          safety_margin_ratio: draft.safety_margin_ratio ?? quota.safety_margin_ratio,
          safety_margin_units: draft.safety_margin_units ?? quota.safety_margin_units,
        }),
      });
      toast.success(`${quota.id} を保存しました`);
      await load();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "クォータ設定を保存できませんでした");
    }
  };

  const saveProfile = async () => {
    if (!data) return;
    setSavingProfile(true);
    try {
      const response = await pyFetch<{ profile: FreeTeamProfile }>("/free-team/settings", {
        method: "PUT",
        body: JSON.stringify({
          enabled: data.profile.enabled,
          main_pool_id: data.profile.main_pool_id,
          agent_team_enabled: true,
          max_fallbacks: data.profile.max_fallbacks,
          pools: data.profile.pools,
          agent_team: data.profile.agent_team,
        }),
      });
      setData((current) => current ? { ...current, profile: response.profile } : current);
      toast.success("無料Teamのルーティング設定を保存しました");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "ルーティング設定を保存できませんでした");
    } finally {
      setSavingProfile(false);
    }
  };

  const refreshOpenRouter = async () => {
    setRefreshingOpenRouter(true);
    try {
      const response = await pyFetch<{ count: number }>("/free-team/openrouter/refresh", { method: "POST" });
      toast.success(`OpenRouterの無料候補を${response.count}件更新しました`);
      await load();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "OpenRouter候補を更新できませんでした");
    } finally {
      setRefreshingOpenRouter(false);
    }
  };

  if (loading) {
    return <div className="flex items-center gap-2 rounded-md border p-3 text-xs text-muted-foreground"><Loader2 className="size-3 animate-spin" />無料Team設定を取得中...</div>;
  }
  if (!data) return null;

  const updateProfile = (patch: Partial<FreeTeamProfile>) => {
    setData((current) => current ? { ...current, profile: { ...current.profile, ...patch } } : current);
  };
  const updateGroup = (groupId: string, patch: Partial<ModelGroup>) => {
    setData((current) => current ? {
      ...current,
      profile: {
        ...current.profile,
        agent_team: {
          ...current.profile.agent_team,
          model_groups: {
            ...current.profile.agent_team.model_groups,
            [groupId]: { ...current.profile.agent_team.model_groups[groupId], ...patch },
          },
        },
      },
    } : current);
  };

  return (
    <div className="space-y-4 rounded-md border border-emerald-500/40 bg-emerald-500/5 p-3" data-testid="free-team-settings">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2 text-sm font-medium"><ShieldCheck className="size-4 text-emerald-600" />無料Team</div>
          <p className="mt-1 text-[11px] text-muted-foreground">無料API枠を優先し、予約できない場合だけプロモーション・CLIへ移ります。通常課金への自動移行は行いません。</p>
        </div>
        <div className="flex gap-2">
          <Button size="sm" variant="outline" onClick={() => void refreshOpenRouter()} disabled={refreshingOpenRouter}>
            <RefreshCcw className={`mr-1 size-3 ${refreshingOpenRouter ? "animate-spin" : ""}`} />OpenRouter更新
          </Button>
          <Button size="sm" onClick={() => void saveProfile()} disabled={savingProfile}>
            {savingProfile ? <Loader2 className="mr-1 size-3 animate-spin" /> : <Save className="mr-1 size-3" />}保存
          </Button>
        </div>
      </div>

      <div className="grid gap-3 sm:grid-cols-3">
        <label className="flex items-center gap-2 text-xs">
          <Checkbox
            checked={data.profile.enabled}
            onCheckedChange={(checked) => updateProfile({ enabled: checked === true })}
          />
          <span>無料Teamを有効化</span>
        </label>
        <label className="space-y-1 text-xs"><span className="text-muted-foreground">メインプール</span><select value={data.profile.main_pool_id} onChange={(event) => updateProfile({ main_pool_id: event.target.value })} className="h-8 w-full rounded-lg border border-input bg-transparent px-2.5 text-sm">{poolIds.map((id) => <option key={id} value={id}>{id}</option>)}</select></label>
        <label className="space-y-1 text-xs"><span className="text-muted-foreground">最大フォールバック回数</span><Input type="number" min={0} max={10} value={data.profile.max_fallbacks} onChange={(event) => updateProfile({ max_fallbacks: Number(event.target.value) })} className="h-8" /></label>
        <div className="space-y-1 text-xs"><span className="text-muted-foreground">超過時の動作</span><div className="flex h-8 items-center"><Badge variant="outline">停止（有料移行なし）</Badge></div></div>
      </div>

      <section className="space-y-2">
        <div className="text-xs font-medium">Agent Teamモデルグループ</div>
        <div className="grid gap-2 lg:grid-cols-2">
          {Object.entries(data.profile.agent_team.model_groups).map(([groupId, group]) => (
            <div key={groupId} className="space-y-2 rounded border p-2">
              <div className="flex items-center justify-between gap-2"><span className="text-xs font-medium">{group.name || groupId}</span><Badge variant="outline">{groupId}</Badge></div>
              <div className="grid gap-2 sm:grid-cols-2">
                <label className="space-y-1 text-[10px] text-muted-foreground"><span>対象</span><select value={group.target_type || (group.pool_id ? "pool" : group.provider ? "static" : "inherit")} onChange={(event) => updateGroup(groupId, { target_type: event.target.value as ModelGroup["target_type"] })} className="h-8 w-full rounded-lg border border-input bg-transparent px-2.5 text-sm"><option value="inherit">メインを継承</option><option value="static">固定モデル</option><option value="pool">無料Teamの候補プール</option></select></label>
                {(group.target_type || "pool") === "pool" ? <label className="space-y-1 text-[10px] text-muted-foreground"><span>プール</span><select value={group.pool_id || ""} onChange={(event) => updateGroup(groupId, { pool_id: event.target.value })} className="h-8 w-full rounded-lg border border-input bg-transparent px-2.5 text-sm">{poolIds.map((id) => <option key={id} value={id}>{id}</option>)}</select></label> : group.target_type === "static" ? <div className="grid grid-cols-2 gap-1"><Input value={group.provider || ""} onChange={(event) => updateGroup(groupId, { provider: event.target.value })} placeholder="provider" className="h-8" /><Input value={group.model || ""} onChange={(event) => updateGroup(groupId, { model: event.target.value })} placeholder="model" className="h-8" /></div> : null}
              </div>
            </div>
          ))}
        </div>
      </section>

      <section className="space-y-2">
        <div className="text-xs font-medium">CredentialProfile</div>
        <div className="grid gap-2 lg:grid-cols-2">
          {data.credentials.map((credential) => (
            <div key={credential.id} className="space-y-2 rounded border p-2">
              <div className="flex items-center justify-between gap-2"><div className="flex items-start gap-2"><Checkbox aria-label={`${credential.display_name}を有効化`} checked={credential.enabled} onCheckedChange={(checked) => setData((current) => current ? { ...current, credentials: current.credentials.map((item) => item.id === credential.id ? { ...item, enabled: checked === true } : item) } : current)} /><div><div className="text-xs font-medium">{credential.display_name}</div><div className="text-[10px] text-muted-foreground">{credential.id} · {credential.billing_mode}</div></div></div><Badge variant={credential.configured ? "default" : "outline"}>{credential.configured ? "設定済み" : "未設定"}</Badge></div>
              <div className="flex gap-2"><Input aria-label={`${credential.display_name}のAPIキー`} type="password" value={apiKeys[credential.id] || ""} onChange={(event) => setApiKeys((current) => ({ ...current, [credential.id]: event.target.value }))} placeholder={credential.provider.endsWith("-cli") ? "CLI認証を使用" : "新しいAPIキー（空なら維持）"} disabled={credential.provider.endsWith("-cli")} className="h-8" /><Button size="sm" variant="outline" onClick={() => void saveCredential(credential)}>保存</Button></div>
            </div>
          ))}
        </div>
      </section>

      <section className="space-y-2">
        <div className="text-xs font-medium">候補モデル</div>
        <div className="space-y-2">
          {data.candidates.map((candidate) => {
            const draft = candidateDrafts[candidate.id] ?? {};
            return <div key={candidate.id} className="grid gap-2 rounded border p-2 md:grid-cols-[minmax(220px,1fr)_90px_100px_110px_auto] md:items-end"><div className="flex items-start gap-2"><Checkbox checked={draft.enabled ?? candidate.enabled} onCheckedChange={(checked) => setCandidateDrafts((current) => ({ ...current, [candidate.id]: { ...current[candidate.id], enabled: checked === true } }))} /><div><div className="text-xs font-medium">{candidate.provider} / {candidate.model}</div><div className="text-[10px] text-muted-foreground">{candidate.id} · {candidate.status}{candidate.cooldown_until ? ` · cooldown ${candidate.cooldown_until}` : ""}</div></div></div><label className="space-y-1 text-[10px] text-muted-foreground"><span>優先順位</span><Input type="number" value={draft.priority ?? candidate.priority} onChange={(event) => setCandidateDrafts((current) => ({ ...current, [candidate.id]: { ...current[candidate.id], priority: Number(event.target.value) } }))} className="h-8" /></label><label className="space-y-1 text-[10px] text-muted-foreground"><span>effort</span><Input value={draft.effort ?? candidate.effort ?? ""} onChange={(event) => setCandidateDrafts((current) => ({ ...current, [candidate.id]: { ...current[candidate.id], effort: event.target.value } }))} className="h-8" /></label><label className="space-y-1 text-[10px] text-muted-foreground"><span>最大出力token</span><Input type="number" min={1} value={draft.max_output_tokens ?? candidate.max_output_tokens} onChange={(event) => setCandidateDrafts((current) => ({ ...current, [candidate.id]: { ...current[candidate.id], max_output_tokens: Number(event.target.value) } }))} className="h-8" /></label><Button size="sm" variant="outline" onClick={() => void saveCandidate(candidate)}>保存</Button></div>;
          })}
        </div>
      </section>

      <section className="space-y-2">
        <div className="text-xs font-medium">QuotaPool</div>
        <div className="space-y-2">
          {data.quota_pools.map((quota) => {
            const draft = quotaDrafts[quota.id] ?? {};
            return <div key={quota.id} className="grid gap-2 rounded border p-2 md:grid-cols-[minmax(210px,1fr)_90px_90px_90px_100px_100px_100px_auto] md:items-end"><div><div className="text-xs font-medium">{quota.id}</div><div className="text-[10px] text-muted-foreground">{quota.metric_type} · reset {quota.window_end || "なし"} · sync {quota.last_provider_sync_at || "未実行"}</div></div><div className="text-xs"><span className="block text-[10px] text-muted-foreground">使用済み</span>{formatValue(quota.consumed, quota.metric_type)}</div><div className="text-xs"><span className="block text-[10px] text-muted-foreground">予約中</span>{formatValue(quota.reserved, quota.metric_type)}</div><div className="text-xs"><span className="block text-[10px] text-muted-foreground">残量</span>{formatValue(quota.available, quota.metric_type)}</div><label className="space-y-1 text-[10px] text-muted-foreground"><span>上限</span><Input type="number" min={0} value={draft.limit ?? quota.limit} onChange={(event) => setQuotaDrafts((current) => ({ ...current, [quota.id]: { ...current[quota.id], limit: Number(event.target.value) } }))} className="h-8" /></label><label className="space-y-1 text-[10px] text-muted-foreground"><span>安全率</span><Input aria-label={`${quota.id}の安全率`} type="number" min={0} max={1} step={0.01} value={draft.safety_margin_ratio ?? quota.safety_margin_ratio} onChange={(event) => setQuotaDrafts((current) => ({ ...current, [quota.id]: { ...current[quota.id], safety_margin_ratio: Number(event.target.value) } }))} className="h-8" /></label><label className="space-y-1 text-[10px] text-muted-foreground"><span>安全量</span><Input aria-label={`${quota.id}の安全量`} type="number" min={0} value={draft.safety_margin_units ?? quota.safety_margin_units} onChange={(event) => setQuotaDrafts((current) => ({ ...current, [quota.id]: { ...current[quota.id], safety_margin_units: Number(event.target.value) } }))} className="h-8" /></label><Button size="sm" variant="outline" onClick={() => void saveQuota(quota)}>保存</Button></div>;
          })}
        </div>
      </section>
    </div>
  );
}
