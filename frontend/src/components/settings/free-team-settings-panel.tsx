"use client";

import { AppSelect } from "@/components/ui/app-select";

import { useCallback, useEffect, useId, useMemo, useState } from "react";
import { Loader2, RefreshCcw, Save, ShieldCheck } from "lucide-react";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { pyFetch } from "./llm-model-section-types";
import { HelpPopover } from "@/components/ui/help-popover";
import { OpenAIConfirmationPanel } from "./openai-confirmation-panel";
import { OpenRouterQuotaObservation } from "./openrouter-quota-observation";

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
  available: number | null;
  availability_state?: "provider_managed" | "local";
  daily_cap?: number;
  configured_daily_cap?: number;
  effective_daily_cap?: number;
  cap_restriction_reason?: string;
  usage_tier?: string;
  provider_external_delta?: number;
  model_group?: string | null;
  stop_reason?: string;
  reset_policy?: { kind?: string; timezone?: string; day?: number; hour?: number; billing_scope_id?: string; [key: string]: unknown };
  safety_margin_ratio: number;
  safety_margin_units: number;
  window_end?: string | null;
  last_provider_sync_at?: string | null;
  status: string;
};

type QuotaDraft = Partial<QuotaPool> & { billing_scope_id?: string };


type FreeTeamProfile = {
  display_name: string;
  enabled: boolean;
  main_pool_id: string;
  max_fallbacks: number;
  openrouter_free_tier?: 50 | 1000;
  pools: Record<string, { candidate_ids?: string[] }>;
};

type OpenAIUsageActualGroup = {
  actual_usage: number | null;
  input_tokens: number | null;
  output_tokens: number | null;
  input_cached_tokens: number | null;
  requests: number | null;
  limit: number;
  ratio: number | null;
  source: "ledger" | "openai_usage_api";
  actual_usage_state: string;
  last_synced_at: string | null;
  external_usage_estimate: number | null;
};

type FreeTeamResponse = {
  profile: FreeTeamProfile;
  credentials: Credential[];
  candidates: Candidate[];
  quota_pools: QuotaPool[];
  verification?: { status: string; stop_reason: string; external_usage_visibility: string };
  openai_usage_actual?: { groups?: Partial<Record<"sol" | "luna", OpenAIUsageActualGroup>> };
};

const stopReasonLabels: Record<string, string> = {
  confirmation_revoked: "確認記録が失効済みです",
  confirmation_credential_changed: "資格情報が確認記録から変更されています",
  complimentary_unverified: "無料枠の対象資格・登録・Projectが未確認",
  complimentary_not_enabled: "無料枠の確認済み設定が無効",
  complimentary_verification_expired: "無料枠の確認情報が期限切れ",
  complimentary_tier_unverified: "対象利用Tierが未確認",
  credential_project_unverified: "実行資格情報・Projectが未確認",
  complimentary_endpoint_unverified: "OpenAI API接続先が未確認",
  billing_scope_unverified: "組織の請求scopeが未確認",
  billing_scope_mismatch: "共有台帳の組織scopeが設定と一致しません",
  billing_scope_unbound_usage: "既存使用量の組織scopeが未確認",
  billing_scope_unmapped_usage: "中央使用量に組織scopeが未確定の記録があります",
  usage_ledger_not_ready: "中央使用量台帳が準備されていません",
  budget_insufficient: "リクエストの最大予約量を確保できません",
  budget_exhausted: "日次予算を使い切りました",
  exhausted: "日次予算を使い切りました",
  disabled: "無効",
  provider_disabled: "Providerが無効",
};

function dailyCap(quota: QuotaPool): number {
  // Older usage responses put the Tier-clamped value in daily_cap. Editing
  // must use the persisted setting (or its limit/margins), never that guard.
  return quota.configured_daily_cap ?? Math.max(0, quota.limit - Math.max(quota.safety_margin_units, quota.limit * quota.safety_margin_ratio));
}

function formatValue(value: number, metric: string): string {
  if (metric === "usd") return `$${value.toFixed(4)}`;
  return new Intl.NumberFormat("ja-JP", { maximumFractionDigits: 3 }).format(value);
}

export function FreeTeamSettingsPanel({ quotaOnly = false }: { quotaOnly?: boolean } = {}) {
  const panelId = useId();
  const [data, setData] = useState<FreeTeamResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [savingProfile, setSavingProfile] = useState(false);
  const [openRouterTierDraft, setOpenRouterTierDraft] = useState<50 | 1000 | null>(null);
  const [savingOpenRouterTier, setSavingOpenRouterTier] = useState(false);
  const [refreshingOpenRouter, setRefreshingOpenRouter] = useState(false);
  const [apiKeys, setApiKeys] = useState<Record<string, string>>({});
  const [candidateDrafts, setCandidateDrafts] = useState<Record<string, Partial<Candidate>>>({});
  const [quotaDrafts, setQuotaDrafts] = useState<Record<string, QuotaDraft>>({});

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [settings, usage] = await Promise.all([
        pyFetch<Omit<FreeTeamResponse, "quota_pools">>("/free-team/settings"),
        pyFetch<Pick<FreeTeamResponse, "quota_pools" | "verification" | "openai_usage_actual">>("/free-team/usage"),
      ]);
      setData({ ...settings, quota_pools: usage.quota_pools, verification: usage.verification ?? settings.verification, openai_usage_actual: usage.openai_usage_actual });
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "無料Team設定を取得できませんでした");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    void Promise.resolve().then(() => {
      if (!cancelled) void load();
    });
    return () => { cancelled = true; };
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
          model: draft.model ?? candidate.model,
          priority: draft.priority ?? candidate.priority,
          effort: draft.effort ?? candidate.effort ?? "",
          max_output_tokens: draft.max_output_tokens ?? candidate.max_output_tokens,
        }),
      });
      toast.success(`${candidate.id} を保存しました`);
      setCandidateDrafts((current) => { const next = { ...current }; delete next[candidate.id]; return next; });
      await load();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "候補設定を保存できませんでした");
    }
  };

  const saveQuota = async (quota: QuotaPool) => {
    const draft = quotaDrafts[quota.id] ?? {};
    const trimmedScope = quota.id === "gemini-promo-monthly-usd" ? draft.billing_scope_id?.trim() : undefined;
    const currentScope = quota.reset_policy?.billing_scope_id ?? "";
    // Omit unchanged or empty scope so the existing ID or backend default is retained.
    const scope = trimmedScope && trimmedScope !== currentScope ? trimmedScope : undefined;
    if (scope !== undefined) {
      if (scope.length > 100 || ["default", "unknown", "none"].includes(scope.toLowerCase())) {
        toast.error("ローカル帳簿のscope IDは1〜100文字で、default / unknown / none 以外を指定してください");
        return;
      }
      if (!quota.reset_policy || typeof quota.reset_policy !== "object" || Array.isArray(quota.reset_policy)) {
        toast.error("既存のリセット設定を取得できませんでした");
        return;
      }
    }
    try {
      await pyFetch(`/free-team/quota-pools/${encodeURIComponent(quota.id)}`, {
        method: "PATCH",
        body: JSON.stringify({
          ...(quota.availability_state !== "provider_managed" || draft.limit !== undefined ? { limit: draft.limit ?? quota.limit } : {}),
          safety_margin_ratio: draft.safety_margin_ratio ?? quota.safety_margin_ratio,
          safety_margin_units: draft.safety_margin_units ?? quota.safety_margin_units,
          ...(draft.daily_cap !== undefined ? { daily_cap: draft.daily_cap } : {}),
          ...(scope !== undefined ? { reset_policy: { ...quota.reset_policy, billing_scope_id: scope } } : {}),
        }),
      });
      toast.success(`${quota.id} を保存しました`);
      setQuotaDrafts((current) => { const next = { ...current }; delete next[quota.id]; return next; });
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
          max_fallbacks: data.profile.max_fallbacks,
          pools: data.profile.pools,
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

  const saveOpenRouterTier = async () => {
    setSavingOpenRouterTier(true);
    let tierSaved = false;
    try {
      const response = await pyFetch<{ profile: FreeTeamProfile }>("/free-team/settings", {
        method: "PUT",
        body: JSON.stringify({
          openrouter_free_tier: openRouterTierDraft ?? data?.profile.openrouter_free_tier ?? 1000,
        }),
      });
      tierSaved = true;
      setData((current) => current ? { ...current, profile: response.profile } : current);
      setOpenRouterTierDraft(null);
      const usage = await pyFetch<Pick<FreeTeamResponse, "quota_pools">>("/free-team/usage");
      setData((current) => current ? { ...current, ...usage } : current);
      toast.success("OpenRouter無料枠tierを保存しました");
    } catch (error) {
      toast.error(
        tierSaved
          ? "OpenRouter無料枠tierは保存しましたがquota状態を再読できませんでした"
          : error instanceof Error ? error.message : "OpenRouter無料枠tierを保存できませんでした",
      );
    } finally {
      setSavingOpenRouterTier(false);
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

  return (
    <div className="space-y-4 rounded-md border border-emerald-500/40 bg-emerald-500/5 p-3" data-testid="free-team-settings">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="inline-flex items-center gap-1 text-sm font-medium">
            <ShieldCheck className="size-4 text-emerald-600" />
            {quotaOnly ? "共有予算" : "無料Team"}
            <HelpPopover label={quotaOnly ? "共有予算" : "無料Team"}>
              {quotaOnly ? (
                <p>日次停止capと安全マージンを各QuotaPoolの行で保存します。</p>
              ) : (
                <>
                  <p>無料API枠を優先し、予約できない場合だけプロモーション・CLIへ移ります。通常課金への自動移行は行いません。</p>
                  <p>Sol群は970,000・Luna群は9,970,000 token/日で停止する初期値です。組織全体の同一model groupで共有し、00:00 UTCにリセットします。未確認の資格情報を有効化してもAPI実行は許可されません。</p>
                  <p>planner・worker・reviewerのモデルとeffortは、設定 → 言語モデル → モデル役割とAgentの職務・モデルで管理します。</p>
                </>
              )}
            </HelpPopover>
          </div>
        </div>
        {!quotaOnly && <div className="flex gap-2">
          <Button size="sm" variant="outline" onClick={() => void refreshOpenRouter()} disabled={refreshingOpenRouter}>
            <RefreshCcw className={`mr-1 size-3 ${refreshingOpenRouter ? "animate-spin" : ""}`} />OpenRouter更新
          </Button>
          <Button size="sm" onClick={() => void saveProfile()} disabled={savingProfile}>
            {savingProfile ? <Loader2 className="mr-1 size-3 animate-spin" /> : <Save className="mr-1 size-3" />}ルーティングを保存
          </Button>
        </div>}
      </div>

      {!quotaOnly && <div className="grid gap-3 sm:grid-cols-3">
        <label className="flex items-center gap-2 text-xs">
          <Checkbox
            checked={data.profile.enabled}
            onCheckedChange={(checked) => updateProfile({ enabled: checked === true })}
          />
          <span>無料Teamを有効化</span>
        </label>
        <label className="space-y-1 text-xs"><span className="text-muted-foreground">メインプール</span><AppSelect value={data.profile.main_pool_id} onChange={(event) => updateProfile({ main_pool_id: event.target.value })} className="h-8 w-full rounded-lg border border-input bg-transparent px-2.5 text-sm">{poolIds.map((id) => <option key={id} value={id}>{id}</option>)}</AppSelect></label>
        <label className="space-y-1 text-xs"><span className="text-muted-foreground">最大フォールバック回数</span><Input type="number" min={0} max={10} value={data.profile.max_fallbacks} onChange={(event) => updateProfile({ max_fallbacks: Number(event.target.value) })} className="h-8" /></label>
        <div className="space-y-1 text-xs"><span className="text-muted-foreground">超過時の動作</span><div className="flex h-8 items-center"><Badge variant="outline">停止（有料移行なし）</Badge></div></div>
      </div>}

      <div className="space-y-1 rounded border p-2 text-xs" role="status">
        <p>OpenAI無料枠: {data.verification?.status === "verified" ? "確認済み" : "確認待ち（API実行停止）"}</p>
        <p>{data.verification?.status === "verified" && !data.verification.stop_reason ? "対象組織・Projectの確認情報に基づき最大予約量を確認します" : stopReasonLabels[data.verification?.stop_reason || "complimentary_unverified"] || data.verification?.stop_reason}</p>
        <p className="text-muted-foreground">OpenAIはローカル台帳と組織全体のUsage API実績の大きい方で残量を判定します。実績が未取得・古い場合は状態に応じた安全余裕を適用します。使用量不明の予約は精算できるものを照合し、期間windowの終了後は猶予を置いて解放します。</p>
      </div>

      <OpenRouterQuotaObservation key={data.profile.openrouter_free_tier ?? 1000} />

      <section className="space-y-2 rounded border p-2">
        <div className="inline-flex items-center gap-1 text-xs font-medium">
          OpenRouter無料枠tier
          <HelpPopover label="OpenRouter無料枠tier">
            選択tierはOpenRouter側の日次資格です。実効上限は「ローカルの日次停止cap」と「tier上限から安全余白2件を引いた値」の小さい方です。既存の日次停止cap・quota上限・安全設定はtier変更では書き換えません。OpenRouter無料枠は20 requests/minute、日次resetはUTC 00:00です。1000件tierは累計10 USD購入済みのアカウント向けです。
          </HelpPopover>
        </div>
        <label className="block max-w-xs space-y-1 text-[10px] text-muted-foreground">
          <span>アカウントtier</span>
          <AppSelect
            aria-label="OpenRouter無料枠tier"
            value={String(openRouterTierDraft ?? data.profile.openrouter_free_tier ?? 1000)}
            onValueChange={(value) => setOpenRouterTierDraft(Number(value) as 50 | 1000)}
            className="h-8 w-full rounded-lg border border-input bg-transparent px-2.5 text-sm"
          >
            <option value="50">50 requests/day</option>
            <option value="1000">1000 requests/day（累計10 USD購入済み）</option>
          </AppSelect>
        </label>
        <Button size="sm" variant="outline" onClick={() => void saveOpenRouterTier()} disabled={savingOpenRouterTier}>
          {savingOpenRouterTier ? <Loader2 className="mr-1 size-3 animate-spin" /> : <Save className="mr-1 size-3" />}
          OpenRouter tierを保存
        </Button>
      </section>

      <OpenAIConfirmationPanel onChanged={async () => {
        const usage = await pyFetch<Pick<FreeTeamResponse, "quota_pools" | "verification" | "openai_usage_actual">>("/free-team/usage");
        setData((current) => current ? { ...current, ...usage } : current);
      }} />

      {!quotaOnly && <>
      <section className="space-y-2">
        <div className="text-xs font-medium">CredentialProfile</div>
        <div className="grid gap-2 lg:grid-cols-2">
          {data.credentials
            .filter((credential) => credential.id !== "openai-complimentary")
            .map((credential) => (
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
            return <div key={candidate.id} className="grid gap-2 rounded border p-2 md:grid-cols-[minmax(220px,1fr)_90px_100px_110px_auto] md:items-end"><div className="flex items-start gap-2"><Checkbox aria-label={`${candidate.id}を有効化`} checked={draft.enabled ?? candidate.enabled} onCheckedChange={(checked) => setCandidateDrafts((current) => ({ ...current, [candidate.id]: { ...current[candidate.id], enabled: checked === true } }))} /><div><div className="text-xs font-medium">{candidate.provider}</div><Input aria-label={`${candidate.id}のmodel`} value={draft.model ?? candidate.model} onChange={(event) => setCandidateDrafts((current) => ({ ...current, [candidate.id]: { ...current[candidate.id], model: event.target.value } }))} className="h-8" /><div className="text-[10px] text-muted-foreground">{candidate.id} · {candidate.status}{candidate.cooldown_until ? ` · cooldown ${candidate.cooldown_until}` : ""}</div></div></div><label className="space-y-1 text-[10px] text-muted-foreground"><span>優先順位</span><Input type="number" value={draft.priority ?? candidate.priority} onChange={(event) => setCandidateDrafts((current) => ({ ...current, [candidate.id]: { ...current[candidate.id], priority: Number(event.target.value) } }))} className="h-8" /></label><label className="space-y-1 text-[10px] text-muted-foreground"><span>effort</span><Input aria-label={`${candidate.id}のeffort`} value={draft.effort ?? candidate.effort ?? ""} onChange={(event) => setCandidateDrafts((current) => ({ ...current, [candidate.id]: { ...current[candidate.id], effort: event.target.value } }))} className="h-8" /></label><label className="space-y-1 text-[10px] text-muted-foreground"><span>最大出力token</span><Input type="number" min={1} value={draft.max_output_tokens ?? candidate.max_output_tokens} onChange={(event) => setCandidateDrafts((current) => ({ ...current, [candidate.id]: { ...current[candidate.id], max_output_tokens: Number(event.target.value) } }))} className="h-8" /></label><Button size="sm" variant="outline" onClick={() => void saveCandidate(candidate)}>保存</Button></div>;
          })}
        </div>
      </section>

      </>}
      <section className="space-y-2">
        <div className="text-xs font-medium">OpenAI 組織の今日の使用量（UTC）</div>
        <div className="grid gap-2 sm:grid-cols-2">
          {(["sol", "luna"] as const).map((name) => {
            const group = data.openai_usage_actual?.groups?.[name];
            const isCurrent = group?.actual_usage_state === "ok" && group.source === "openai_usage_api";
            return (
              <div key={name} className="rounded border p-2 text-xs" role="status">
                <div className="font-medium">{name}</div>
                {isCurrent ? (
                  <div>{formatValue(group.actual_usage ?? 0, "tokens")} / {formatValue(group.limit, "tokens")} tokens</div>
                ) : (
                  <div>実績未取得（{group?.actual_usage_state ?? "unavailable"}） · 上限 {formatValue(group?.limit ?? 0, "tokens")}</div>
                )}
                <div className="text-[10px] text-muted-foreground">取得時刻: {isCurrent ? group.last_synced_at || "不明" : "不明"}</div>
                {isCurrent && (group.external_usage_estimate ?? 0) > 0 && (
                  <div className="text-[10px]">AoiTalk 外を含む差分推定: {formatValue(group.external_usage_estimate ?? 0, "tokens")}</div>
                )}
              </div>
            );
          })}
        </div>
      </section>
      <section className="space-y-2">
        <div className="text-xs font-medium">QuotaPool</div>
        <div className="space-y-2">
          {data.quota_pools.map((quota) => {
            const draft = quotaDrafts[quota.id] ?? {};
            return (
              <div key={quota.id} className={quotaOnly ? "grid gap-2 rounded border p-2 sm:grid-cols-2 lg:grid-cols-4" : "grid gap-2 rounded border p-2 md:grid-cols-4 xl:grid-cols-[minmax(210px,1fr)_70px_70px_70px_90px_100px_75px_90px_auto] md:items-end"}>
                <div>
                  <div className="text-xs font-medium">{quota.id}</div>
                  <div className="text-[10px] text-muted-foreground">{quota.model_group ? `${quota.model_group}共有群 · ` : ""}{quota.metric_type} · reset (UTC) {quota.window_end || "なし"} · sync {quota.last_provider_sync_at || "未実行"}</div>
                  <div id={`${panelId}-${quota.id}-effective-cap`} aria-label={`${quota.id}の実効日次停止cap`} className="text-[10px]">
                    現在の実効cap: {quota.availability_state === "provider_managed" ? "Provider管理／上限未取得" : formatValue(quota.effective_daily_cap ?? quota.daily_cap ?? dailyCap(quota), quota.metric_type)}
                    {quota.cap_restriction_reason === "usage_tier" && <div>利用Tier（{quota.usage_tier}）による上限制限</div>}
                  </div>
                  {(quota.provider_external_delta ?? 0) > 0 && <div className="text-[10px]">外部観測分 {formatValue(quota.provider_external_delta ?? 0, quota.metric_type)} を残量から控除</div>}
                  <div className="text-[10px]" role="status">停止理由: {quota.stop_reason ? stopReasonLabels[quota.stop_reason] || quota.stop_reason : quota.availability_state === "provider_managed" ? "なし（Provider管理）" : "なし（最大予約量を事前確認）"}</div>
                </div>
                <div className="text-xs"><span className="block text-[10px] text-muted-foreground">使用済み</span>{formatValue(quota.consumed, quota.metric_type)}</div>
                <div className="text-xs"><span className="block text-[10px] text-muted-foreground">予約中</span>{formatValue(quota.reserved, quota.metric_type)}</div>
                <div className="text-xs"><span className="block text-[10px] text-muted-foreground">{quota.id === "gemini-promo-monthly-usd" ? "ローカル台帳残額（Google実credit未確認）" : "残量"}</span>{quota.availability_state === "provider_managed" ? "上限未取得" : formatValue(quota.available ?? 0, quota.metric_type)}</div>
                <label className="space-y-1 text-[10px] text-muted-foreground"><span>{quota.id === "openrouter-free-rpd" ? "ローカル上限" : "上限"}</span><Input aria-label={`${quota.id}の上限`} type="number" min={0} value={draft.limit ?? (quota.availability_state === "provider_managed" ? "" : quota.limit)} placeholder={quota.availability_state === "provider_managed" ? "Provider管理" : undefined} onChange={(event) => setQuotaDrafts((current) => ({ ...current, [quota.id]: { ...current[quota.id], limit: event.target.value === "" ? undefined : Number(event.target.value) } }))} className="h-8" /></label>
                {(quota.availability_state !== "provider_managed" || draft.limit !== undefined) && <label className="space-y-1 text-[10px] text-muted-foreground"><span>{quota.id === "openrouter-free-rpd" ? "日次停止cap（ローカル上限）" : "日次停止cap（設定値）"}</span><Input aria-label={`${quota.id}の日次停止cap`} aria-describedby={`${panelId}-${quota.id}-effective-cap`} type="number" min={0} max={quota.id === "openrouter-free-rpd" ? Math.max(0, (draft.limit ?? quota.limit) - 2) : draft.limit ?? quota.limit} value={draft.daily_cap ?? dailyCap(quota)} onChange={(event) => setQuotaDrafts((current) => ({ ...current, [quota.id]: { ...current[quota.id], daily_cap: Number(event.target.value), safety_margin_ratio: 0, safety_margin_units: (current[quota.id]?.limit ?? quota.limit) - Number(event.target.value) } }))} className="h-8" /></label>}
                {quota.id !== "openrouter-free-rpd" && <label className="space-y-1 text-[10px] text-muted-foreground"><span>安全率</span><Input aria-label={`${quota.id}の安全率`} type="number" min={0} max={1} step={0.01} value={draft.safety_margin_ratio ?? quota.safety_margin_ratio} onChange={(event) => setQuotaDrafts((current) => ({ ...current, [quota.id]: { ...current[quota.id], safety_margin_ratio: Number(event.target.value), daily_cap: undefined } }))} className="h-8" /></label>}
                {quota.id !== "openrouter-free-rpd" && <label className="space-y-1 text-[10px] text-muted-foreground"><span>安全量</span><Input aria-label={`${quota.id}の安全量`} type="number" min={0} value={draft.safety_margin_units ?? quota.safety_margin_units} onChange={(event) => setQuotaDrafts((current) => ({ ...current, [quota.id]: { ...current[quota.id], safety_margin_units: Number(event.target.value), daily_cap: undefined } }))} className="h-8" /></label>}
                {quota.id === "gemini-promo-monthly-usd" && <label className="space-y-1 text-[10px] text-muted-foreground"><span>ローカル帳簿のscope ID（Google実creditではない）</span><Input aria-label="ローカル帳簿のscope ID（Google実creditではない）" type="text" maxLength={100} placeholder="自動（未設定時）: freeteam-gemini-promo" value={draft.billing_scope_id ?? quota.reset_policy?.billing_scope_id ?? ""} onChange={(event) => setQuotaDrafts((current) => ({ ...current, [quota.id]: { ...current[quota.id], billing_scope_id: event.target.value } }))} className="h-8" /><span className="block">空欄は現在設定を維持、未設定時自動</span></label>}
                <Button size="sm" variant="outline" onClick={() => void saveQuota(quota)}>予算を保存</Button>
              </div>
            );
          })}
        </div>
      </section>
    </div>
  );
}
