"use client";

import { useCallback, useEffect, useMemo, useRef, useState, type DragEvent, type ReactNode } from "react";
import { ArrowDown, ArrowUp, GripVertical, Search, Settings2, X } from "lucide-react";
import { toast } from "sonner";
import { AppSelect } from "@/components/ui/app-select";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { HelpPopover } from "@/components/ui/help-popover";
import { Input } from "@/components/ui/input";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { FreeTeamSettingsPanel } from "./free-team-settings-panel";
import { pyFetch } from "./llm-model-section-types";
import { cn } from "@/lib/utils";
import {
  BILLING_LABELS,
  buildBillingContext,
  freeQuotaText,
  routeBillingClass,
  type BillingClass,
  type BillingContext,
  type FreeTeamCandidate,
  type FreeTeamCredential,
  type FreeTeamQuotaPool,
} from "@/lib/model-role-billing";
import { ModelRolesConflictError, WORK_ROLES, WORK_ROLE_LABELS, modelRolesApi, routeLabel, sameRoute, type ModelRoles, type ModelRoute, type WorkRole } from "@/lib/model-roles-api";
import type { useLlmModelSection } from "./llm-model-section-state";
import type { RoutingSaveScope } from "./llm-model-section-state";

type SectionState = ReturnType<typeof useLlmModelSection>;
type DetailTarget = { section: "routing"; tab: "vision" | "audio" | "video" | "clip_ingest" | "project_automation" } | { section: "cloud" } | { section: "base" };

/** A model offered in the palette. */
type Candidate = { provider: string; providerLabel: string; model: string; label: string; image: boolean; audio: boolean };
type RoleId = "chat" | WorkRole | "vision" | "audio" | "video" | "clip_ingest" | "project_automation" | "advisor" | "image_generation";

const BILLING_TONE: Record<BillingClass, string> = {
  free: "border-emerald-500/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300",
  subscription: "border-sky-500/40 bg-sky-500/10 text-sky-700 dark:text-sky-300",
  paid: "border-amber-500/40 bg-amber-500/10 text-amber-800 dark:text-amber-200",
  local: "border-border bg-muted/50 text-muted-foreground",
};
const PALETTE_ORDER: BillingClass[] = ["free", "subscription", "local", "paid"];
const DEFAULT_IMAGE_MODEL = "gemini-3-pro-image-preview";
const MAX_FALLBACKS = 5;
const WORK_ROLE_HELP: Record<WorkRole, string> = {
  code: "Agent がコードを書く・読むときのモデルです。空なら会話と同じモデルを使います。Agent ごとの上書きは Operations の Agent 画面で設定します。",
  review: "Agent がレビューや確認をするときのモデルです。空なら会話と同じモデルを使います。",
  research: "Agent が調査や計画を立てるときのモデルです。空なら会話と同じモデルを使います。",
  summarize: "要約や短い整理に使うモデルです。空なら会話と同じモデルを使います。",
};

async function readJson<T>(path: string): Promise<T> {
  return pyFetch<T>(path, { cache: "no-store" });
}

function Choice({ label, value, onChange, choices }: { label: string; value: string; onChange: (value: string) => void; choices: Array<{ value: string; label: string }> }) {
  return <AppSelect aria-label={label} size="sm" className="h-6 text-[11px]" value={value} onValueChange={onChange}>
    {choices.map(choice => <option key={choice.value} value={choice.value}>{choice.label}</option>)}
  </AppSelect>;
}

function BillingBadge({ billing, quota }: { billing: BillingClass; quota?: string | null }) {
  return <span className={cn("rounded-full border px-1.5 py-px text-[10px]", BILLING_TONE[billing])}>{BILLING_LABELS[billing]}{quota ? ` · ${quota}` : ""}</span>;
}

function RouteChip({ label, billing, quota, muted, children }: { label: string; billing: BillingClass | null; quota?: string | null; muted?: boolean; children?: ReactNode }) {
  return <span className={cn("inline-flex min-w-0 max-w-full items-center gap-1.5 rounded-md border bg-card px-2 py-1 text-xs", muted && "opacity-50")}>
    <span className="min-w-0 truncate font-medium">{label}</span>
    {billing ? <BillingBadge billing={billing} quota={quota} /> : null}
    {children}
  </span>;
}

function RoleRow({ title, help, accepts, active, onDrop, onAssign, assignable, primary, controls, onDetail, children }: {
  title: string;
  help: string;
  /** Whether the model being dragged or picked can be used for this role. */
  accepts: boolean | null;
  active: boolean;
  onDrop: () => void;
  onAssign: () => void;
  assignable: boolean;
  primary: ReactNode;
  controls?: ReactNode;
  onDetail?: () => void;
  children?: ReactNode;
}) {
  const [over, setOver] = useState(false);
  return <li
    className={cn("grid min-w-0 gap-2 p-3 transition-colors sm:grid-cols-[9rem_minmax(0,1fr)_auto] sm:items-start",
      accepts === true && "bg-primary/5", accepts === false && "opacity-60", over && accepts && "ring-2 ring-inset ring-primary")}
    data-role-row={title}
    onDragOver={(event) => { if (accepts) { event.preventDefault(); setOver(true); } }}
    onDragLeave={() => setOver(false)}
    onDrop={(event) => { event.preventDefault(); setOver(false); if (accepts) onDrop(); }}
  >
    <div className="flex items-center gap-1 text-sm font-semibold">{title}<HelpPopover label={title}>{help}</HelpPopover></div>
    <div className="flex min-w-0 flex-col gap-2">
      <div className="flex min-w-0 flex-wrap items-center gap-1.5">
        <span className="text-[10px] font-semibold text-muted-foreground">1</span>
        {primary}
        {active && assignable ? <Button type="button" size="xs" onClick={onAssign}>ここに設定</Button> : null}
      </div>
      {children}
    </div>
    <div className="flex flex-wrap items-center gap-1.5">
      {controls}
      {onDetail ? <Button type="button" size="xs" variant="ghost" onClick={onDetail} aria-label={`${title}の詳細`}><Settings2 className="size-3.5" />詳細</Button> : null}
    </div>
  </li>;
}

/**
 * One screen for "which model does each job". A role's first model is saved to
 * the settings the runtime already reads; only the chat fallback order and the
 * image-generation model live in the role table.
 */
export function LlmModelRoles({ state, onOpenDetail, canManage = true }: { state: SectionState; onOpenDetail: (target: DetailTarget) => void; canManage?: boolean }) {
  const {
    providerOptions, provider: chatProvider, selectedModelId, saveModelSelection,
    classDrafts, setClassDrafts, imageMode, setImageMode, videoMode, setVideoMode, mageVl,
    clipIngestProviders, projectAutomationProviders, projectAutomationDraft, setProjectAutomationDraft,
    cloudAdvisor, setCloudAdvisor, cloudAdvisorProviderIds, saveRoutingSettings, saveCloudAdvisorSettings, savingRouting,
  } = state;
  const [roles, setRoles] = useState<ModelRoles | null>(null);
  const [rolesError, setRolesError] = useState<string | null>(null);
  const [billing, setBilling] = useState<BillingContext>({ freeRoutes: new Map() });
  const [quotaPools, setQuotaPools] = useState<FreeTeamQuotaPool[]>([]);
  const [quotaError, setQuotaError] = useState<string | null>(null);
  const [poolIds, setPoolIds] = useState<string[]>([]);
  const [search, setSearch] = useState("");
  const [picked, setPicked] = useState<Candidate | null>(null);
  const [dragged, setDragged] = useState<Candidate | null>(null);
  const [draggedFallback, setDraggedFallback] = useState<number | null>(null);
  // Save after React has applied the draft, so the save sees the new values.
  const [saveRequest, setSaveRequest] = useState<{ id: number; scope: RoutingSaveScope | "advisor" } | null>(null);
  const handledSave = useRef(0);

  const loadRoles = useCallback(async () => {
    try {
      setRoles(await modelRolesApi.get());
      setRolesError(null);
    } catch (error) {
      setRolesError(error instanceof Error ? error.message : "役割の設定を読み込めませんでした");
    }
  }, []);

  const loadFreeTeamQuota = useCallback(async () => {
    // Free Team quotas and credentials are administrator data.
    if (!canManage) return;
    try {
      const [settings, usage] = await Promise.all([
        readJson<{ credentials?: FreeTeamCredential[]; candidates?: FreeTeamCandidate[]; profile?: { pools?: Record<string, unknown> } }>("/free-team/settings"),
        readJson<{ quota_pools?: FreeTeamQuotaPool[] }>("/free-team/usage"),
      ]);
      const pools = usage.quota_pools ?? [];
      setQuotaPools(pools);
      setPoolIds(Object.keys(settings.profile?.pools ?? {}));
      setBilling(buildBillingContext(settings.credentials ?? [], settings.candidates ?? [], pools));
      setQuotaError(null);
    } catch (error) {
      setQuotaError(error instanceof Error ? error.message : "無料Team の残量を取得できません");
    }
  }, [canManage]);

  useEffect(() => {
    const task = window.setTimeout(() => {
      void loadRoles();
      void loadFreeTeamQuota();
    }, 0);
    return () => window.clearTimeout(task);
  }, [loadRoles, loadFreeTeamQuota]);

  useEffect(() => {
    if (!saveRequest || handledSave.current === saveRequest.id) return;
    handledSave.current = saveRequest.id;
    void (saveRequest.scope === "advisor" ? saveCloudAdvisorSettings() : saveRoutingSettings(saveRequest.scope));
  }, [saveRequest, saveRoutingSettings, saveCloudAdvisorSettings]);
  const requestSave = (scope: RoutingSaveScope | "advisor") => setSaveRequest(current => ({ id: (current?.id ?? 0) + 1, scope }));

  const candidates = useMemo<Candidate[]>(() => providerOptions
    .filter(item => item.selection_kind !== "routing_profile")
    .flatMap(item => item.models
      .filter(option => option.selection_kind !== "routing_profile")
      .map(option => ({ provider: item.id, providerLabel: item.label || item.id, model: option.id, label: option.label || option.id, image: Boolean(option.media?.image), audio: Boolean(option.media?.audio) }))),
  [providerOptions]);

  const billingOf = useCallback((provider: string, model: string) => routeBillingClass(provider, model, billing), [billing]);
  const quotaOf = useCallback((provider: string, model: string) => freeQuotaText(provider, model, billing), [billing]);
  const routeBilling = (route: ModelRoute): BillingClass => route.kind === "pool" ? "free" : billingOf(route.provider, route.model);

  const clipIds = useMemo(() => new Set(clipIngestProviders.map(item => item.id)), [clipIngestProviders]);
  const automationIds = useMemo(() => new Set(projectAutomationProviders.map(item => item.id)), [projectAutomationProviders]);
  const advisorIds = useMemo(() => new Set(cloudAdvisorProviderIds), [cloudAdvisorProviderIds]);
  const accepts: Record<RoleId, (candidate: Candidate) => boolean> = {
    chat: () => true,
    code: () => true,
    review: () => true,
    research: () => true,
    summarize: () => true,
    vision: candidate => candidate.image,
    audio: candidate => candidate.audio,
    video: () => false,
    clip_ingest: candidate => clipIds.has(candidate.provider),
    project_automation: candidate => automationIds.has(candidate.provider),
    advisor: candidate => advisorIds.has(candidate.provider),
    image_generation: candidate => candidate.provider === "gemini",
  };
  const current = dragged ?? picked;
  const acceptState = (role: RoleId) => current ? accepts[role](current) : null;

  const fallbacks = roles?.roles.chat?.fallbacks ?? [];
  const allowPaidFallback = roles?.roles.chat?.allow_paid_fallback ?? false;
  const saveRoles = async (next: ModelRoles["roles"]) => {
    if (!roles || !canManage) return;
    try {
      setRoles(await modelRolesApi.put(roles.version, next));
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "役割の設定を保存できませんでした");
      if (error instanceof ModelRolesConflictError) void loadRoles();
    }
  };
  const saveChatRoles = (nextFallbacks: ModelRoute[], nextAllowPaid = allowPaidFallback) =>
    saveRoles({ ...roles?.roles, chat: { fallbacks: nextFallbacks, allow_paid_fallback: nextAllowPaid } });

  const saveWorkRole = (role: WorkRole, route: ModelRoute | null) =>
    saveRoles({ ...roles?.roles, [role]: { routes: route ? [route] : [] } });

  const assign = (role: RoleId, candidate: Candidate) => {
    setPicked(null);
    setDragged(null);
    if (role === "chat") void saveModelSelection(candidate.provider, candidate.model);
    else if ((WORK_ROLES as readonly string[]).includes(role)) void saveWorkRole(role as WorkRole, { kind: "llm", provider: candidate.provider, model: candidate.model });
    else if (role === "vision" || role === "audio" || role === "clip_ingest") {
      setClassDrafts(prev => ({ ...prev, [role]: { ...prev[role], inherit: false, provider: candidate.provider, model: candidate.model, customModel: "", ...(role === "audio" ? { engine: "llm" as const } : {}) } }));
      requestSave(role);
    } else if (role === "project_automation") {
      setProjectAutomationDraft(prev => ({ ...prev, inherit: false, provider: candidate.provider, model: candidate.model, customModel: "" }));
      requestSave("project_automation");
    } else if (role === "advisor") {
      setCloudAdvisor(prev => ({ ...prev, provider: candidate.provider, model: candidate.model }));
      requestSave("advisor");
    } else if (role === "image_generation") {
      void saveRoles({ ...roles?.roles, image_generation: { primary: { kind: "llm", provider: candidate.provider, model: candidate.model } } });
    }
  };
  const addFallback = (route: ModelRoute) => {
    if (fallbacks.some(item => sameRoute(item, route)) || fallbacks.length >= MAX_FALLBACKS) return;
    void saveChatRoles([...fallbacks, route]);
  };
  const moveFallback = (from: number, to: number) => {
    if (to < 0 || to >= fallbacks.length || from === to) return;
    const next = [...fallbacks];
    const [item] = next.splice(from, 1);
    next.splice(to, 0, item);
    void saveChatRoles(next);
  };
  const inheritRoute = (role: "vision" | "audio" | "clip_ingest") => {
    setClassDrafts(prev => ({ ...prev, [role]: { ...prev[role], inherit: true, ...(role === "audio" ? { engine: "llm" as const } : {}) } }));
    requestSave(role);
  };

  const chip = (provider: string, model: string, label = `${provider} / ${model}`) =>
    <RouteChip label={label} billing={billingOf(provider, model)} quota={quotaOf(provider, model)} />;
  const inheritChip = <RouteChip label="会話と同じモデル" billing={null} />;
  const dragProps = (candidate: Candidate) => ({
    draggable: true,
    onDragStart: (event: DragEvent) => { event.dataTransfer.effectAllowed = "copy"; event.dataTransfer.setData("text/plain", `${candidate.provider}/${candidate.model}`); setDragged(candidate); },
    onDragEnd: () => setDragged(null),
  });

  const filtered = candidates.filter(candidate => !search.trim() || `${candidate.providerLabel} ${candidate.label} ${candidate.model}`.toLowerCase().includes(search.trim().toLowerCase()));
  const paidRoles = [
    billingOf(chatProvider, selectedModelId) === "paid" ? "会話" : null,
    !classDrafts.vision.inherit && classDrafts.vision.provider && billingOf(classDrafts.vision.provider, classDrafts.vision.model) === "paid" ? "画像認識" : null,
    !classDrafts.clip_ingest.inherit && classDrafts.clip_ingest.provider && billingOf(classDrafts.clip_ingest.provider, classDrafts.clip_ingest.model) === "paid" ? "サイト解析" : null,
    !projectAutomationDraft.inherit && projectAutomationDraft.provider && billingOf(projectAutomationDraft.provider, projectAutomationDraft.model) === "paid" ? "Project 自動化" : null,
    cloudAdvisor.mode !== "disabled" && cloudAdvisor.provider && cloudAdvisor.provider !== "chatgpt-web" && billingOf(cloudAdvisor.provider, cloudAdvisor.model ?? "") === "paid" ? "相談" : null,
  ].filter(Boolean);
  const firstPaidFallback = fallbacks.findIndex(route => routeBilling(route) === "paid");

  return <div className="space-y-3" data-testid="llm-model-roles">
    <section aria-label="無料枠と課金" className="rounded-lg border bg-card p-3 text-xs">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <span className="font-semibold">無料枠</span>
        {quotaPools.filter(pool => typeof pool.available === "number").slice(0, 8).map(pool => <span key={pool.id} className={cn(pool.stop_reason && "text-destructive")}>{pool.id} {pool.stop_reason ? "停止中" : `${pool.id === "gemini-promo-monthly-usd" ? "ローカル台帳残額" : "残"} ${pool.available! >= 1000 ? `${Math.round(pool.available! / 100) / 10}k` : pool.available}`}{pool.id === "gemini-promo-monthly-usd" && "（Google実credit未確認）"}</span>)}
        {canManage && !quotaPools.length && !quotaError && <span className="text-muted-foreground">無料Team の残量を確認中</span>}
        {canManage && quotaError && <span role="alert" className="text-destructive">無料Team の残量を取得できません: {quotaError}</span>}
        {canManage && <Dialog onOpenChange={(open) => { if (!open) void loadFreeTeamQuota(); }}>
          <DialogTrigger render={<Button size="xs" variant="outline" />}>無料Team設定</DialogTrigger>
          <DialogContent size="4xl" className="max-h-[85vh] overflow-y-auto">
            <DialogHeader><DialogTitle>無料Team設定</DialogTitle></DialogHeader>
            <FreeTeamSettingsPanel />
          </DialogContent>
        </Dialog>}
      </div>
      <p className="mt-1">{paidRoles.length ? <>従量課金になる役割: <span className="font-medium text-amber-700 dark:text-amber-300">{paidRoles.join("、")}</span></> : <span className="text-muted-foreground">従量課金で動く役割はありません。</span>}</p>
    </section>

    <div className="grid min-w-0 items-start gap-3 lg:grid-cols-[16rem_minmax(0,1fr)]">
      <aside aria-label="モデル一覧" className="min-w-0 rounded-lg border bg-card p-2 lg:sticky lg:top-2">
        <label className="relative block"><Search className="absolute left-2 top-2 size-3.5 text-muted-foreground" /><Input value={search} onChange={event => setSearch(event.target.value)} placeholder="モデルを検索" aria-label="モデルを検索" className="h-7 pl-7 text-xs" /></label>
        <p className="mt-1 px-1 text-[10px] text-muted-foreground">役割の行へドラッグします。タップで選んでから「ここに設定」でも割り当てられます。</p>
        <div className="mt-2 max-h-[60vh] space-y-2 overflow-y-auto">
          {PALETTE_ORDER.map(group => {
            const items = filtered.filter(candidate => billingOf(candidate.provider, candidate.model) === group);
            if (!items.length) return null;
            return <div key={group}>
              <p className="px-1 text-[10px] font-semibold text-muted-foreground">{BILLING_LABELS[group]}</p>
              <ul className="mt-1 space-y-1">{items.slice(0, 60).map(candidate => {
                const selected = picked?.provider === candidate.provider && picked.model === candidate.model;
                return <li key={`${candidate.provider}/${candidate.model}`}>
                  <button type="button" {...dragProps(candidate)} onClick={() => setPicked(selected ? null : candidate)} aria-pressed={selected}
                    className={cn("flex w-full min-w-0 cursor-grab items-center gap-1.5 rounded-md border px-2 py-1 text-left text-xs hover:bg-muted/60", selected && "border-primary bg-primary/10")}
                    data-model-chip={`${candidate.provider}/${candidate.model}`}>
                    <GripVertical className="size-3 shrink-0 text-muted-foreground" />
                    <span className="min-w-0 flex-1 truncate"><span className="text-muted-foreground">{candidate.providerLabel} · </span>{candidate.label}</span>
                    {quotaOf(candidate.provider, candidate.model) ? <span className="shrink-0 text-[10px] text-emerald-700 dark:text-emerald-300">{quotaOf(candidate.provider, candidate.model)}</span> : null}
                  </button>
                </li>;
              })}</ul>
            </div>;
          })}
        </div>
      </aside>

      <div className="min-w-0 space-y-2">
        {rolesError ? <p role="alert" className="text-xs text-destructive">{rolesError}</p> : null}
        <ul className="divide-y rounded-lg border bg-card" aria-label="役割 → モデル">
          <RoleRow title="会話" help="普通の Chat と、Agent の既定のモデルです。先頭が使えないとき（起動できない、要求を送る前に拒否された）は、2 番目以降を順に使います。"
            accepts={acceptState("chat")} active={Boolean(current)} assignable={Boolean(current && accepts.chat(current))}
            onDrop={() => dragged && assign("chat", dragged)} onAssign={() => picked && assign("chat", picked)}
            primary={chip(chatProvider, selectedModelId)} onDetail={() => onOpenDetail({ section: "base" })}>
            <div className="flex min-w-0 flex-wrap items-center gap-1.5 rounded-md border border-dashed p-1.5"
              onDragOver={event => { if (dragged || draggedFallback !== null) event.preventDefault(); }}
              onDrop={event => { event.preventDefault(); event.stopPropagation(); if (dragged) { addFallback({ kind: "llm", provider: dragged.provider, model: dragged.model }); setDragged(null); } }}
              aria-label="会話の fallback">
              {fallbacks.map((route, index) => <span key={routeLabel(route)} className="inline-flex items-center gap-1"
                draggable onDragStart={() => setDraggedFallback(index)} onDragEnd={() => setDraggedFallback(null)}
                onDragOver={event => { if (draggedFallback !== null) event.preventDefault(); }}
                onDrop={event => { if (draggedFallback === null) return; event.preventDefault(); event.stopPropagation(); moveFallback(draggedFallback, index); setDraggedFallback(null); }}>
                {index === firstPaidFallback && !allowPaidFallback ? <span className="text-[10px] font-medium text-amber-700 dark:text-amber-300">ここで停止 ▸</span> : null}
                <span className="text-[10px] font-semibold text-muted-foreground">{index + 2}</span>
                <RouteChip label={routeLabel(route)} billing={routeBilling(route)} quota={route.kind === "llm" ? quotaOf(route.provider, route.model) : null} muted={!allowPaidFallback && firstPaidFallback >= 0 && index >= firstPaidFallback}>
                  <button type="button" aria-label={`${routeLabel(route)} を上へ`} onClick={() => moveFallback(index, index - 1)} disabled={index === 0}><ArrowUp className="size-3" /></button>
                  <button type="button" aria-label={`${routeLabel(route)} を下へ`} onClick={() => moveFallback(index, index + 1)} disabled={index === fallbacks.length - 1}><ArrowDown className="size-3" /></button>
                  <button type="button" aria-label={`${routeLabel(route)} を外す`} onClick={() => void saveChatRoles(fallbacks.filter((_, at) => at !== index))}><X className="size-3" /></button>
                </RouteChip>
              </span>)}
              {picked && fallbacks.length < MAX_FALLBACKS ? <Button type="button" size="xs" variant="outline" onClick={() => { addFallback({ kind: "llm", provider: picked.provider, model: picked.model }); setPicked(null); }}>fallback に追加</Button> : null}
              {poolIds.length > 0 && fallbacks.length < MAX_FALLBACKS ? <Choice label="無料Team の pool を fallback に追加" value="" onChange={value => { if (value) addFallback({ kind: "pool", pool_id: value }); }}
                choices={[{ value: "", label: "＋ 無料Team の pool" }, ...poolIds.map(id => ({ value: id, label: id }))]} /> : null}
              {!fallbacks.length && !picked ? <span className="text-[11px] text-muted-foreground">fallback なし（ここにドロップして追加）</span> : null}
            </div>
            <label className="inline-flex items-center gap-1.5 text-[11px] text-muted-foreground">
              <Checkbox checked={allowPaidFallback} onCheckedChange={value => void saveChatRoles(fallbacks, value === true)} />
              従量課金の fallback へ進むことを許可する
            </label>
          </RoleRow>

          {WORK_ROLES.map(role => {
            const route = roles?.roles[role]?.routes?.[0];
            return <RoleRow key={role} title={WORK_ROLE_LABELS[role]} help={WORK_ROLE_HELP[role]}
              accepts={acceptState(role)} active={Boolean(current)} assignable={Boolean(current)}
              onDrop={() => dragged && assign(role, dragged)} onAssign={() => picked && assign(role, picked)}
              primary={route ? <RouteChip label={routeLabel(route)} billing={routeBilling(route)} quota={route.kind === "llm" ? quotaOf(route.provider, route.model) : null} /> : inheritChip}
              controls={<>
                {route ? <Button type="button" size="xs" variant="ghost" onClick={() => void saveWorkRole(role, null)}>会話と同じに</Button> : null}
                {poolIds.length > 0 ? <Choice label={`${WORK_ROLE_LABELS[role]}に無料Team の pool を設定`} value="" onChange={value => { if (value) void saveWorkRole(role, { kind: "pool", pool_id: value }); }}
                  choices={[{ value: "", label: "無料Team の pool" }, ...poolIds.map(id => ({ value: id, label: id }))]} /> : null}
              </>} />;
          })}

          <RoleRow title="画像認識" help="Chat に添付された画像を読むモデルです。画像に対応したモデルだけを割り当てられます。"
            accepts={acceptState("vision")} active={Boolean(current)} assignable={Boolean(current && accepts.vision(current))}
            onDrop={() => dragged && assign("vision", dragged)} onAssign={() => picked && assign("vision", picked)}
            primary={classDrafts.vision.inherit || !classDrafts.vision.provider ? inheritChip : chip(classDrafts.vision.provider, classDrafts.vision.customModel || classDrafts.vision.model)}
            controls={<>
              {!classDrafts.vision.inherit ? <Button type="button" size="xs" variant="ghost" onClick={() => inheritRoute("vision")}>会話と同じに</Button> : null}
              <Choice label="画像の送信" value={imageMode} onChange={value => { setImageMode(value as typeof imageMode); requestSave("vision"); }}
                choices={[{ value: "auto", label: "送信: 自動" }, { value: "always", label: "送信: 常に" }, { value: "off", label: "送信: しない" }]} />
            </>}
            onDetail={() => onOpenDetail({ section: "routing", tab: "vision" })} />

          <RoleRow title="音声認識" help="音声の文字起こしです。ローカル STT、会話と同じモデル、音声に対応した別のモデル、のどれかを使います。"
            accepts={acceptState("audio")} active={Boolean(current)} assignable={Boolean(current && accepts.audio(current))}
            onDrop={() => dragged && assign("audio", dragged)} onAssign={() => picked && assign("audio", picked)}
            primary={classDrafts.audio.engine === "speech_recognition" ? <RouteChip label="ローカル STT" billing="local" /> : classDrafts.audio.engine === "off" ? <RouteChip label="オフ" billing={null} /> : classDrafts.audio.inherit || !classDrafts.audio.provider ? inheritChip : chip(classDrafts.audio.provider, classDrafts.audio.customModel || classDrafts.audio.model)}
            controls={<Choice label="音声認識の方式"
              value={classDrafts.audio.engine === "speech_recognition" ? "local" : classDrafts.audio.engine === "off" ? "off" : classDrafts.audio.inherit ? "inherit" : "model"}
              onChange={value => {
                if (value === "inherit") { inheritRoute("audio"); return; }
                if (value === "model") return;
                setClassDrafts(prev => ({ ...prev, audio: { ...prev.audio, engine: value === "local" ? "speech_recognition" : "off" } }));
                requestSave("audio");
              }}
              choices={[{ value: "local", label: "ローカル STT" }, { value: "inherit", label: "会話と同じ" }, { value: "model", label: "別のモデル（ドロップ）" }, { value: "off", label: "オフ" }]} />}
            onDetail={() => onOpenDetail({ section: "routing", tab: "audio" })} />

          <RoleRow title="動画" help="動画の理解です。ローカルの Mage-VL を使います。起動やフレーム数などは「詳細」で設定します。"
            accepts={current ? false : null} active={false} assignable={false} onDrop={() => undefined} onAssign={() => undefined}
            primary={<RouteChip label={`mage_vl / ${classDrafts.video.customModel || classDrafts.video.model || mageVl.model || "Mage-VL"}`} billing="local" muted={videoMode === "off"} />}
            controls={<Choice label="動画の扱い" value={videoMode} onChange={value => { setVideoMode(value as typeof videoMode); requestSave("video"); }}
              choices={[{ value: "auto", label: "動画: 自動" }, { value: "off", label: "動画: オフ" }]} />}
            onDetail={() => onOpenDetail({ section: "routing", tab: "video" })} />

          <RoleRow title="サイト解析" help="Web クリップや資料の取り込みで、内容を読み解くモデルです。"
            accepts={acceptState("clip_ingest")} active={Boolean(current)} assignable={Boolean(current && accepts.clip_ingest(current))}
            onDrop={() => dragged && assign("clip_ingest", dragged)} onAssign={() => picked && assign("clip_ingest", picked)}
            primary={classDrafts.clip_ingest.inherit || !classDrafts.clip_ingest.provider ? inheritChip : chip(classDrafts.clip_ingest.provider, classDrafts.clip_ingest.customModel || classDrafts.clip_ingest.model)}
            controls={!classDrafts.clip_ingest.inherit ? <Button type="button" size="xs" variant="ghost" onClick={() => inheritRoute("clip_ingest")}>会話と同じに</Button> : null}
            onDetail={() => onOpenDetail({ section: "routing", tab: "clip_ingest" })} />

          <RoleRow title="Project 自動化" help="Project の要約や進捗確認など、Project の自動処理に使うモデルです。"
            accepts={acceptState("project_automation")} active={Boolean(current)} assignable={Boolean(current && accepts.project_automation(current))}
            onDrop={() => dragged && assign("project_automation", dragged)} onAssign={() => picked && assign("project_automation", picked)}
            primary={projectAutomationDraft.inherit || !projectAutomationDraft.provider ? inheritChip : chip(projectAutomationDraft.provider, projectAutomationDraft.customModel || projectAutomationDraft.model)}
            controls={!projectAutomationDraft.inherit ? <Button type="button" size="xs" variant="ghost" onClick={() => { setProjectAutomationDraft(prev => ({ ...prev, inherit: true })); requestSave("project_automation"); }}>会話と同じに</Button> : null}
            onDetail={() => onOpenDetail({ section: "routing", tab: "project_automation" })} />

          <RoleRow title="相談" help="難しい判断のときに、別のモデルへ相談します（Cloud Advisor）。"
            accepts={acceptState("advisor")} active={Boolean(current)} assignable={Boolean(current && accepts.advisor(current))}
            onDrop={() => dragged && assign("advisor", dragged)} onAssign={() => picked && assign("advisor", picked)}
            primary={cloudAdvisor.provider === "chatgpt-web" ? <RouteChip label="Web ChatGPT" billing="subscription" muted={cloudAdvisor.mode === "disabled"} />
              : <RouteChip label={`${cloudAdvisor.provider ?? "openai"} / ${cloudAdvisor.model || "既定"}`} billing={billingOf(cloudAdvisor.provider ?? "openai", cloudAdvisor.model ?? "")} muted={cloudAdvisor.mode === "disabled"} />}
            controls={<Choice label="相談の使い方" value={cloudAdvisor.mode ?? "disabled"} onChange={value => { setCloudAdvisor(prev => ({ ...prev, mode: value as CloudAdvisorMode })); requestSave("advisor"); }}
              choices={[{ value: "disabled", label: "使わない" }, { value: "manual", label: "必要なときだけ" }, { value: "automatic", label: "自動" }]} />}
            onDetail={() => onOpenDetail({ section: "cloud" })} />

          <RoleRow title="画像生成" help="Chat の画像生成 tool が使うモデルです。Gemini の画像モデルだけを割り当てられます。"
            accepts={acceptState("image_generation")} active={Boolean(current)} assignable={Boolean(current && accepts.image_generation(current))}
            onDrop={() => dragged && assign("image_generation", dragged)} onAssign={() => picked && assign("image_generation", picked)}
            primary={(() => {
              const primary = roles?.roles.image_generation?.primary;
              return primary && primary.kind === "llm" ? chip(primary.provider, primary.model) : <RouteChip label={`gemini / ${DEFAULT_IMAGE_MODEL}（既定）`} billing={billingOf("gemini", DEFAULT_IMAGE_MODEL)} />;
            })()} />
        </ul>
        {savingRouting ? <p role="status" className="text-xs text-muted-foreground">保存中…</p> : null}
      </div>
    </div>
  </div>;
}

type CloudAdvisorMode = "disabled" | "manual" | "automatic";
