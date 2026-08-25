"use client";

// Agent Team schema v3 settings UI.  This file intentionally keeps the
// background automation and Character/TRPG boundaries separate from the Team
// topology.

import { AppSelect } from "@/components/ui/app-select";
import { useCallback, useEffect, useMemo, useRef, useState, type Dispatch, type SetStateAction } from "react";
import { ChevronDown, ChevronUp, Globe2, Loader2, Plus, RefreshCw, Save, Search, Trash2 } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { AgentTeamTopologyPreview } from "./agent-team-topology-preview";
import { ExecutionRouteEditor } from "./llm-agent-team-execution-route";
import {
  canonicalAgentTeamConfig,
  canonicalTeamExecutionProfile,
  emptyAgentTeamConfig,
  emptyExecutionRoute,
  pyFetch,
  reasoningEffortOptionsForModel,
  type AgentTeamActivationMode,
  type AgentTeamCapabilityInfo,
  type AgentTeamConfig,
  type AgentTeamConfigEnvelope,
  type AgentTeamSubagent,
  type AgentTeamTeam,
  type LlmModelCatalogResponse,
  type TeamExecutionProfile,
} from "./llm-model-section-types";

type ChatGPTWebSettings = {
  profile_dir: string;
  response_timeout_seconds: number;
  max_rounds_per_turn: number;
};

type ChatGPTWebStatus = {
  busy?: boolean;
  settings_browser_open?: boolean;
  director_running?: boolean;
  playwright_available?: boolean;
  logged_in?: boolean;
  needs_human?: boolean;
  error?: boolean;
  message?: string;
};

type ChatGPTWebStatusPresentation = {
  label: string;
  tone: "neutral" | "warning" | "error";
};

export function getChatGPTWebStatusPresentation(status: ChatGPTWebStatus | null): ChatGPTWebStatusPresentation {
  if (status?.error || status?.playwright_available === false) {
    return { label: status?.playwright_available === false ? "利用不可" : "接続エラー", tone: "error" };
  }
  if (status?.settings_browser_open) return { label: "設定ブラウザを使用中", tone: "neutral" };
  if (status?.director_running) return { label: "Directorを実行中", tone: "neutral" };
  if (status?.needs_human) return { label: "確認が必要", tone: "warning" };
  if (status?.logged_in === true) return { label: "ログイン済み", tone: "neutral" };
  if (status?.logged_in === false) return { label: "未ログイン", tone: "warning" };
  return { label: "接続未確認", tone: "neutral" };
}

type AgentTeamRoutingProps = {
  catalog: LlmModelCatalogResponse;
  provider: string;
  selectedModelId: string;
  delegationEnabled: boolean;
  setDelegationEnabled: Dispatch<SetStateAction<boolean>>;
  orchestrationMode: "standard" | "director";
  setOrchestrationMode: Dispatch<SetStateAction<"standard" | "director">>;
  chatgptWeb: ChatGPTWebSettings;
  setChatgptWeb: Dispatch<SetStateAction<ChatGPTWebSettings>>;
  savingRouting: boolean;
  saveRoutingSettings: () => void | Promise<void>;
  agentTeamConfig?: AgentTeamConfig | null;
  setAgentTeamConfig?: Dispatch<SetStateAction<AgentTeamConfig | null>>;
};

const activationLabels: Record<AgentTeamActivationMode, string> = {
  always: "常時有効",
  contextual: "指定場面で自動有効化",
  manual: "必要時にオンデマンド読込",
};
const workspaceLabels: Record<AgentTeamSubagent["max_workspace_access"], string> = {
  none: "なし",
  read: "読み取り",
  write: "読み書き",
};
// Context tags are an implementation detail.  Keep their stable values in the
// draft, but expose only the user-facing descriptions in the settings UI.
const contextOptions: Array<{ value: string; label: string }> = [
  { value: "app_development", label: "App開発時" },
  { value: "story", label: "Story利用時" },
  { value: "trpg", label: "TRPG利用時" },
];

function newId(prefix: string, existing: Record<string, unknown>): string {
  let id = `${prefix}_${Date.now().toString(36)}`;
  let suffix = 1;
  while (existing[id]) id = `${prefix}_${Date.now().toString(36)}_${suffix++}`;
  return id;
}
function defaultSubagent(id: string): AgentTeamSubagent {
  return { subagent_id: id, name: "新しいSubagent", description: "", instructions: "", enabled: true, capability_ids: [], scalable: false, default_instances: 1, max_instances: 1, max_workspace_access: "none", allow_cli_native_tools: false };
}
function defaultTeam(id: string, sortOrder: number): AgentTeamTeam {
  return { team_id: id, name: "新しいTeam", description: "", enabled: true, sort_order: sortOrder, activation: { mode: "always", contexts: [] }, subagent_ids: [], execution_profiles: {} };
}
function defaultExecutionProfile(id: string): TeamExecutionProfile {
  return canonicalTeamExecutionProfile(id, {
    profile_id: id,
    name: "新しいExecution Profile",
    enabled: true,
    default_route: emptyExecutionRoute(),
    overrides: {},
  });
}
function updateRecord<T>(record: Record<string, T>, id: string, patch: Partial<T>): Record<string, T> {
  return record[id] ? { ...record, [id]: { ...record[id], ...patch } } : record;
}

function explicitRouteValidationError(
  route: { inherit_model: boolean; provider: string; model: string; effort_policy: string; effort: string },
  catalog: LlmModelCatalogResponse,
  label: string,
): string {
  if (route.inherit_model) {
    if (route.effort_policy === "explicit" && !String(route.effort || "").trim()) {
      return `${label} は明示的な effort を選択してください。`;
    }
    return "";
  }
  const provider = String(route.provider || "").trim().toLowerCase();
  const model = String(route.model || "").trim();
  if (!provider || !model) return `${label} は provider と model の両方が必要です。`;
  // Free Team is a routing profile rather than an effort-capable model.
  if (provider === "routing-profile" && model === "free-team") return "";
  const effortPolicy = route.effort_policy;
  // Gemini's catalog exposes fast/thinking, but the Agent Team native
  // transport cannot pass those modes without silently dropping or mapping
  // them.  Model-default remains valid because it sends no explicit effort.
  if (provider === "gemini" && effortPolicy === "explicit") {
    return `${label} の Gemini explicit effort は Agent Team runtime では利用できません。`;
  }
  if (effortPolicy === "default") {
    if (String(route.effort || "").trim()) return `${label} はモデル既定では effort を空にしてください。`;
    return "";
  }
  if (effortPolicy !== "explicit") return `${label} は effort_policy=default または explicit が必要です。`;
  const providerCatalog = catalog.providers.find((item) => item.id === provider);
  const options = reasoningEffortOptionsForModel(providerCatalog, model);
  if (!options.length) return `${label} の ${provider}/${model} は effort の明示指定に対応していません。`;
  const effort = String(route.effort || "").trim();
  if (!effort) return `${label} の effort を選択してください。`;
  if (!options.includes(effort)) return `${label} の effort は catalog の値を選択してください。`;
  return "";
}

function firstExplicitRouteError(config: AgentTeamConfig, catalog: LlmModelCatalogResponse): string {
  for (const [teamId, team] of Object.entries(config.teams)) {
    for (const [profileId, profile] of Object.entries(team.execution_profiles)) {
      const defaultError = explicitRouteValidationError(
        profile.default_route,
        catalog,
        `${teamId}/${profileId} default route`,
      );
      if (defaultError) return defaultError;
      for (const [subagentId, route] of Object.entries(profile.overrides)) {
        const overrideError = explicitRouteValidationError(
          route,
          catalog,
          `${teamId}/${profileId}/${subagentId} override`,
        );
        if (overrideError) return overrideError;
      }
    }
  }
  return "";
}

export function LlmAgentTeamRouting(props: AgentTeamRoutingProps) {
  const {
    catalog,
    provider,
    selectedModelId,
    delegationEnabled,
    setDelegationEnabled,
    orchestrationMode,
    setOrchestrationMode,
    chatgptWeb,
    setChatgptWeb,
    savingRouting,
    saveRoutingSettings,
    agentTeamConfig,
    setAgentTeamConfig,
  } = props;
  const [draft, setDraft] = useState<AgentTeamConfig>(() => canonicalAgentTeamConfig(agentTeamConfig ?? emptyAgentTeamConfig()));
  const draftRef = useRef(draft);
  draftRef.current = draft;
  const [capabilityCatalog, setCapabilityCatalog] = useState<Record<string, AgentTeamCapabilityInfo>>({});
  const [teamSaving, setTeamSaving] = useState(false);
  const [teamMessage, setTeamMessage] = useState("");
  const [teamError, setTeamError] = useState("");
  const [connectionStatus, setConnectionStatus] = useState<ChatGPTWebStatus | null>(null);
  const [connectionAction, setConnectionAction] = useState<"open" | "test" | "status" | null>(null);
  const [directorDetailsOpen, setDirectorDetailsOpen] = useState(false);
  const [selectedTeamId, setSelectedTeamId] = useState("");
  const [selectedSubagentId, setSelectedSubagentId] = useState("");
  const [selectedExecutionProfileId, setSelectedExecutionProfileId] = useState("");
  const [activeTab, setActiveTab] = useState<"teams" | "subagents" | "director">("teams");
  const [teamSearch, setTeamSearch] = useState("");
  const [teamAssignmentSearch, setTeamAssignmentSearch] = useState("");
  const [subagentSearch, setSubagentSearch] = useState("");
  const [subagentAdvancedOpen, setSubagentAdvancedOpen] = useState(false);

  useEffect(() => {
    if (agentTeamConfig) setDraft(canonicalAgentTeamConfig(agentTeamConfig));
  }, [agentTeamConfig]);

  // Keep each selector on a real item after an async GET, an add, or a delete.
  useEffect(() => {
    const ids = Object.keys(draft.teams);
    if (!ids.length) {
      setSelectedTeamId("");
    } else if (!selectedTeamId || !draft.teams[selectedTeamId]) {
      setSelectedTeamId(ids.slice().sort((a, b) => draft.teams[a].sort_order - draft.teams[b].sort_order)[0] ?? ids[0]);
    }
  }, [draft.teams, selectedTeamId]);
  useEffect(() => {
    const ids = Object.keys(draft.subagents);
    if (!ids.length) setSelectedSubagentId("");
    else if (!selectedSubagentId || !draft.subagents[selectedSubagentId]) setSelectedSubagentId(ids[0]);
  }, [draft.subagents, selectedSubagentId]);
  useEffect(() => {
    const profiles = selectedTeamId ? draft.teams[selectedTeamId]?.execution_profiles ?? {} : {};
    const ids = Object.keys(profiles);
    if (!ids.length) setSelectedExecutionProfileId("");
    else if (!selectedExecutionProfileId || !profiles[selectedExecutionProfileId]) setSelectedExecutionProfileId(ids[0]);
  }, [draft.teams, selectedExecutionProfileId, selectedTeamId]);

  // The canonical endpoint is the only Agent Team source of truth.  GET-only
  // catalog/effective route fields are kept outside the draft and never sent
  // back by canonicalAgentTeamConfig().
  useEffect(() => {
    let active = true;
    void pyFetch<{ agent_team?: AgentTeamConfigEnvelope }>("/agent-team/config")
      .then((response) => {
        if (!active || !response.agent_team) return;
        const next = canonicalAgentTeamConfig(response.agent_team);
        draftRef.current = next;
        setDraft(next);
        setAgentTeamConfig?.(next);
        setCapabilityCatalog(response.agent_team.capability_catalog ?? {});
      })
      .catch((error) => {
        if (active) setTeamError(error instanceof Error ? error.message : "Agent Team設定を取得できませんでした");
      });
    return () => { active = false; };
  }, [setAgentTeamConfig]);

  const commitDraft = useCallback((next: AgentTeamConfig) => {
    draftRef.current = next;
    setDraft(next);
    setAgentTeamConfig?.(next);
  }, [setAgentTeamConfig]);

  const updateDraft = useCallback((updater: (value: AgentTeamConfig) => AgentTeamConfig) => {
    // Compute outside setState so parent LlmModelSection is never updated
    // from a child render-phase updater.
    commitDraft(canonicalAgentTeamConfig(updater(draftRef.current)));
  }, [commitDraft]);

  const saveTeamConfig = useCallback(async () => {
    setTeamSaving(true);
    setTeamMessage("");
    setTeamError("");
    try {
      const payload = canonicalAgentTeamConfig({ ...draft, delegation_enabled: delegationEnabled, orchestration_mode: orchestrationMode });
      const routeError = firstExplicitRouteError(payload, catalog);
      if (routeError) {
        setTeamError(routeError);
        setTeamMessage("Agent Team設定を保存できませんでした。");
        return;
      }
      const response = await pyFetch<{ agent_team?: AgentTeamConfigEnvelope }>("/agent-team/config", {
        method: "PUT",
        body: JSON.stringify({ agent_team: payload }),
      });
      const saved = canonicalAgentTeamConfig(response.agent_team ?? payload);
      commitDraft(saved);
      setTeamMessage("Agent Team設定を保存しました。");
      // Director connection options remain owned by their dedicated settings
      // paths; save them together without adding them to the Team document.
      await Promise.resolve(saveRoutingSettings());
    } catch (error) {
      const message = error instanceof Error ? error.message : "Agent Team設定の保存に失敗しました";
      setTeamError(message);
      setTeamMessage("Agent Team設定を保存できませんでした。");
    } finally {
      setTeamSaving(false);
    }
  }, [catalog, commitDraft, delegationEnabled, draft, orchestrationMode, saveRoutingSettings]);

  const updateTeam = (id: string, patch: Partial<AgentTeamTeam>) => updateDraft((current) => {
    const existing = current.teams[id];
    if (!existing) return current;
    const next: AgentTeamTeam = { ...existing, ...patch };
    if (Array.isArray(patch.subagent_ids)) {
      const members = new Set(patch.subagent_ids);
      next.execution_profiles = Object.fromEntries(
        Object.entries(next.execution_profiles).map(([profileId, profile]) => [
          profileId,
          {
            ...profile,
            overrides: Object.fromEntries(
              Object.entries(profile.overrides).filter(([subagentId]) => members.has(subagentId)),
            ),
          },
        ]),
      );
    }
    return { ...current, teams: { ...current.teams, [id]: next } };
  });
  const updateSubagent = (id: string, patch: Partial<AgentTeamSubagent>) => updateDraft((current) => ({ ...current, subagents: updateRecord(current.subagents, id, patch) }));
  const updateExecutionProfile = (teamId: string, profileId: string, patch: Partial<TeamExecutionProfile>) => {
    updateDraft((current) => {
      const team = current.teams[teamId];
      if (!team) return current;
      return {
        ...current,
        teams: updateRecord(current.teams, teamId, {
          execution_profiles: updateRecord(team.execution_profiles, profileId, patch),
        }),
      };
    });
  };
  const deleteTeam = (id: string) => {
    setSelectedTeamId((current) => current === id ? "" : current);
    updateDraft((current) => { const teams = { ...current.teams }; delete teams[id]; return { ...current, teams }; });
  };
  const deleteSubagent = (id: string) => {
    setSelectedSubagentId((current) => current === id ? "" : current);
    updateDraft((current) => {
    const subagents = { ...current.subagents }; delete subagents[id];
    const teams = Object.fromEntries(Object.entries(current.teams).map(([teamId, team]) => {
      const execution_profiles = Object.fromEntries(Object.entries(team.execution_profiles).map(([profileId, profile]) => {
        const overrides = { ...profile.overrides };
        delete overrides[id];
        return [profileId, { ...profile, overrides }];
      }));
      return [teamId, { ...team, subagent_ids: team.subagent_ids.filter((item) => item !== id), execution_profiles }];
    }));
    return { ...current, teams, subagents };
    });
  };
  const deleteExecutionProfile = (teamId: string, profileId: string) => {
    setSelectedExecutionProfileId((current) => current === profileId ? "" : current);
    updateDraft((current) => {
      const team = current.teams[teamId];
      if (!team) return current;
      const execution_profiles = { ...team.execution_profiles };
      delete execution_profiles[profileId];
      return { ...current, teams: updateRecord(current.teams, teamId, { execution_profiles }) };
    });
  };
  const addTeam = () => {
    const id = newId("team", draft.teams);
    setSelectedTeamId(id);
    updateDraft((current) => ({ ...current, teams: { ...current.teams, [id]: defaultTeam(id, (Object.keys(current.teams).length + 1) * 10) } }));
  };
  const addSubagent = () => {
    const id = newId("subagent", draft.subagents);
    setSelectedSubagentId(id);
    updateDraft((current) => ({ ...current, subagents: { ...current.subagents, [id]: defaultSubagent(id) } }));
  };
  const addExecutionProfile = (teamId: string) => {
    const team = draft.teams[teamId];
    if (!team) return;
    const id = newId("ep", team.execution_profiles);
    setSelectedExecutionProfileId(id);
    updateDraft((current) => {
      const currentTeam = current.teams[teamId];
      if (!currentTeam) return current;
      return {
        ...current,
        teams: updateRecord(current.teams, teamId, {
          execution_profiles: { ...currentTeam.execution_profiles, [id]: defaultExecutionProfile(id) },
        }),
      };
    });
  };

  const sortedTeams = useMemo(
    () => Object.entries(draft.teams).sort(([, a], [, b]) => a.sort_order - b.sort_order),
    [draft.teams],
  );
  const visibleTeams = useMemo(() => {
    const query = teamSearch.trim().toLowerCase();
    return query
      ? sortedTeams.filter(([, team]) => `${team.name} ${team.description}`.toLowerCase().includes(query))
      : sortedTeams;
  }, [sortedTeams, teamSearch]);
  const visibleSubagents = useMemo(() => {
    const query = subagentSearch.trim().toLowerCase();
    return Object.entries(draft.subagents).filter(([, subagent]) =>
      !query || `${subagent.name} ${subagent.description}`.toLowerCase().includes(query),
    );
  }, [draft.subagents, subagentSearch]);
  const assignableSubagents = useMemo(() => {
    const query = teamAssignmentSearch.trim().toLowerCase();
    return Object.entries(draft.subagents).filter(([, subagent]) =>
      !query || `${subagent.name} ${subagent.description}`.toLowerCase().includes(query),
    );
  }, [draft.subagents, teamAssignmentSearch]);
  const selectedTeam = selectedTeamId ? draft.teams[selectedTeamId] : undefined;
  const selectedSubagent = selectedSubagentId ? draft.subagents[selectedSubagentId] : undefined;
  const selectedExecutionProfile = selectedTeam && selectedExecutionProfileId
    ? selectedTeam.execution_profiles[selectedExecutionProfileId]
    : undefined;
  const teamExecutionProfiles = selectedTeam
    ? Object.entries(selectedTeam.execution_profiles)
    : [];
  const connectionPresentation = getChatGPTWebStatusPresentation(connectionStatus);
  const directorEnabled = orchestrationMode === "director";

  const refreshConnectionStatus = useCallback(async () => {
    setConnectionAction("status");
    try { setConnectionStatus(await pyFetch<ChatGPTWebStatus>("/chatgpt-web/status")); }
    catch (error) { setConnectionStatus({ error: true, message: error instanceof Error ? error.message : "接続状態を取得できませんでした" }); }
    finally { setConnectionAction(null); }
  }, []);
  useEffect(() => { if (delegationEnabled && directorEnabled) void refreshConnectionStatus(); }, [delegationEnabled, directorEnabled, refreshConnectionStatus]);
  useEffect(() => {
    if (!connectionStatus?.settings_browser_open) return;
    const timer = window.setInterval(() => void refreshConnectionStatus(), 2000);
    return () => window.clearInterval(timer);
  }, [connectionStatus?.settings_browser_open, refreshConnectionStatus]);
  const runConnectionAction = async (kind: "open" | "test") => {
    setConnectionAction(kind);
    try { setConnectionStatus(await pyFetch<ChatGPTWebStatus>(kind === "open" ? "/chatgpt-web/settings-browser" : "/chatgpt-web/check-login", { method: "POST" })); }
    catch (error) { setConnectionStatus({ error: true, message: error instanceof Error ? error.message : "ChatGPT接続操作に失敗しました" }); }
    finally { setConnectionAction(null); }
  };

  return (
    <div className="space-y-4" data-settings-surface="agent-team">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="text-sm font-medium">Agent Team</div>
          <p className="text-[11px] text-muted-foreground">Team、Subagent、Execution Profileを一つの構成として管理します。</p>
        </div>
        <div className="flex items-center gap-2">
          <Badge variant={delegationEnabled ? "secondary" : "outline"}>{delegationEnabled ? "有効" : "無効"}</Badge>
          <Button type="button" size="sm" onClick={() => void saveTeamConfig()} disabled={teamSaving || savingRouting}>
            {teamSaving ? <Loader2 className="mr-1 size-3 animate-spin" /> : <Save className="mr-1 size-3" />}保存
          </Button>
        </div>
      </div>

      <label className="flex items-start gap-2 rounded-md border bg-muted/35 p-3 text-xs">
        <Checkbox checked={delegationEnabled} onCheckedChange={(checked) => setDelegationEnabled(checked === true)} disabled={teamSaving || savingRouting} className="mt-0.5" />
        <span><span className="font-medium">Agent Team topologyを有効にする</span><span className="mt-0.5 block text-[10px] text-muted-foreground">個別Team/Subagentの有効状態とは別に、委譲機能全体を切り替えます。</span></span>
      </label>
      {(teamMessage || teamError) && <p role={teamError ? "alert" : "status"} aria-live="polite" className={`rounded border p-2 text-[10px] ${teamError ? "border-destructive/40 text-destructive" : "text-muted-foreground"}`}>{teamError || teamMessage}</p>}

      <Tabs value={activeTab} onValueChange={(value) => setActiveTab(value as typeof activeTab)} className="space-y-3">
      <TabsList variant="line" aria-label="Agent Team設定" className="flex w-full flex-wrap justify-start gap-1 border-b">
        <TabsTrigger value="teams">Teams</TabsTrigger>
        <TabsTrigger value="subagents">Subagents</TabsTrigger>
        <TabsTrigger value="director">Director</TabsTrigger>
      </TabsList>

      <TabsContent value="teams"><section className="space-y-3 rounded-md border border-border bg-muted/35 p-3" aria-label="Teams">
        <div className="flex items-center justify-between gap-2"><div><h3 className="text-xs font-medium">Teams</h3><p className="text-[10px] text-muted-foreground">一覧から選択して詳細を編集します。</p></div><Button type="button" size="sm" variant="outline" onClick={addTeam} disabled={teamSaving}><Plus className="mr-1 size-3" />Teamを追加</Button></div>
        <div className="grid gap-3 md:grid-cols-[220px_minmax(0,1fr)]">
          <aside className="space-y-1 border-b pb-3 md:border-b-0 md:border-r md:pb-0 md:pr-3" aria-label="Team一覧">
            <Input aria-label="Teamを検索" value={teamSearch} onChange={(event) => setTeamSearch(event.target.value)} placeholder="Teamを検索" className="h-8 text-xs" />
            {visibleTeams.map(([id, team]) => <button key={id} type="button" onClick={() => setSelectedTeamId(id)} aria-current={id === selectedTeamId ? "true" : undefined} className={`w-full rounded-sm border-l-2 px-2 py-1.5 text-left text-[10px] ${id === selectedTeamId ? "border-l-primary bg-primary/10" : "border-l-transparent hover:bg-muted"}`}><span className="block truncate font-medium">{team.name || "名称未設定"}</span><span className="block truncate text-muted-foreground">{team.enabled ? "有効" : "無効"} · {team.subagent_ids.length} Subagents</span></button>)}
            {!visibleTeams.length && <p className="p-2 text-[10px] text-muted-foreground">一致するTeamはありません。</p>}
          </aside>
          {selectedTeam ? <div className="min-w-0 space-y-3 rounded-md border bg-background/45 p-3">
            <div className="flex items-start gap-2">
              <Input value={selectedTeam.name} onChange={(event) => updateTeam(selectedTeamId, { name: event.target.value })} aria-label="Team名" className="h-8 flex-1" disabled={teamSaving} />
              <Button type="button" size="icon" variant="ghost" aria-label="Teamを削除" onClick={() => deleteTeam(selectedTeamId)} disabled={teamSaving}><Trash2 className="size-4" /></Button>
            </div>
            <Textarea value={selectedTeam.description} onChange={(event) => updateTeam(selectedTeamId, { description: event.target.value })} aria-label="Teamの説明" placeholder="Teamの説明" className="min-h-16 text-xs" disabled={teamSaving} />
            <div className="grid gap-2 md:grid-cols-2">
              <label className="flex items-center gap-2 text-[10px] text-muted-foreground"><Checkbox checked={selectedTeam.enabled} onCheckedChange={(checked) => updateTeam(selectedTeamId, { enabled: checked === true })} disabled={teamSaving} />有効</label>
              <label className="space-y-1 text-[10px] text-muted-foreground"><span>利用タイミング</span><AppSelect aria-label="Teamの利用タイミング" value={selectedTeam.activation.mode} onChange={(event) => updateTeam(selectedTeamId, { activation: { ...selectedTeam.activation, mode: event.target.value as AgentTeamActivationMode } })} disabled={teamSaving} className="h-8 w-full">{Object.entries(activationLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</AppSelect></label>
            </div>
            {selectedTeam.activation.mode === "contextual" && <div className="space-y-1 rounded border border-border dark:border-[#333335] bg-background/40 dark:bg-[#131313]/40 p-2" role="group" aria-label="Teamを利用できる場面"><span className="text-[10px] text-muted-foreground">利用できる場面</span><div className="grid gap-1 sm:grid-cols-3">{contextOptions.map((option) => <label key={option.value} className="flex items-center gap-2 text-[10px] text-muted-foreground"><Checkbox checked={selectedTeam.activation.contexts.includes(option.value)} onCheckedChange={(checked) => { const known = new Set(contextOptions.map((item) => item.value)); const retained = selectedTeam.activation.contexts.filter((item) => !known.has(item)); const next = contextOptions.filter((item) => item.value === option.value ? checked === true : selectedTeam.activation.contexts.includes(item.value)).map((item) => item.value); updateTeam(selectedTeamId, { activation: { ...selectedTeam.activation, contexts: [...retained, ...next] } }); }} disabled={teamSaving} />{option.label}</label>)}</div></div>}
            <div className="space-y-2 rounded border border-border bg-background/40 p-2" role="group" aria-label="Teamの所属Subagent">
              <div className="flex items-center justify-between gap-2"><div className="text-[10px] font-medium">所属Subagent</div><Badge variant="secondary" className="text-[9px]">{selectedTeam.subagent_ids.length}人</Badge></div>
              <div className="relative">
                <Search className="pointer-events-none absolute left-2.5 top-1/2 size-3 -translate-y-1/2 text-muted-foreground" />
                <Input aria-label="TeamのSubagentを検索" value={teamAssignmentSearch} onChange={(event) => setTeamAssignmentSearch(event.target.value)} placeholder="Subagentを検索" className="h-8 pl-8 text-xs" />
              </div>
              <div className="grid gap-1 sm:grid-cols-2">
                {assignableSubagents.map(([id, subagent]) => <label key={id} className={`flex items-start gap-2 rounded-sm border-l-2 px-2 py-1.5 text-[10px] ${selectedTeam.subagent_ids.includes(id) ? "border-l-primary bg-primary/10" : "border-l-transparent bg-muted/30"}`}><Checkbox checked={selectedTeam.subagent_ids.includes(id)} onCheckedChange={(checked) => updateTeam(selectedTeamId, { subagent_ids: checked === true ? [...selectedTeam.subagent_ids, id] : selectedTeam.subagent_ids.filter((item) => item !== id) })} disabled={teamSaving} /><span className="min-w-0"><span className="block truncate font-medium text-foreground">{subagent.name || "名称未設定"}</span><span className="block truncate text-muted-foreground">{subagent.description || "説明なし"}</span></span></label>)}
              </div>
              {!assignableSubagents.length && <p className="text-[10px] text-muted-foreground">一致するSubagentはありません。</p>}
            </div>
            <AgentTeamTopologyPreview team={selectedTeam} subagents={draft.subagents} />
            <div className="space-y-2 rounded border border-border bg-background/40 p-2" role="group" aria-label="Execution Profiles">
              <div className="flex items-center justify-between gap-2">
                <div>
                  <div className="text-[10px] font-medium">Execution Profiles</div>
                  <p className="text-[10px] text-muted-foreground">このTeamの各Subagentを今回どのLLMで実行するかを定義します。</p>
                </div>
                <Button type="button" size="sm" variant="outline" onClick={() => addExecutionProfile(selectedTeamId)} disabled={teamSaving}><Plus className="mr-1 size-3" />Profileを追加</Button>
              </div>
              {teamExecutionProfiles.length ? (
                <div className="grid gap-3 md:grid-cols-[180px_minmax(0,1fr)]">
                  <aside className="space-y-1" aria-label="Execution Profile一覧">
                    {teamExecutionProfiles.map(([id, profile]) => (
                      <button
                        key={id}
                        type="button"
                        onClick={() => setSelectedExecutionProfileId(id)}
                        aria-current={id === selectedExecutionProfileId ? "true" : undefined}
                        className={`w-full rounded-sm border-l-2 px-2 py-1.5 text-left text-[10px] ${id === selectedExecutionProfileId ? "border-l-primary bg-primary/10" : "border-l-transparent hover:bg-muted"}`}
                      >
                        <span className="block truncate font-medium">{profile.name || "名称未設定"}</span>
                        <span className="block truncate text-muted-foreground">{profile.enabled ? "有効" : "無効"}</span>
                      </button>
                    ))}
                  </aside>
                  {selectedExecutionProfile ? (
                    <div className="min-w-0 space-y-3">
                      <div className="flex items-start gap-2">
                        <Input
                          value={selectedExecutionProfile.name}
                          onChange={(event) => updateExecutionProfile(selectedTeamId, selectedExecutionProfileId, { name: event.target.value })}
                          aria-label="Execution Profile名"
                          className="h-8 flex-1"
                          disabled={teamSaving}
                        />
                        <Button type="button" size="icon" variant="ghost" aria-label="Execution Profileを削除" onClick={() => deleteExecutionProfile(selectedTeamId, selectedExecutionProfileId)} disabled={teamSaving}>
                          <Trash2 className="size-4" />
                        </Button>
                      </div>
                      <label className="flex items-center gap-2 text-[10px] text-muted-foreground">
                        <Checkbox
                          checked={selectedExecutionProfile.enabled}
                          onCheckedChange={(checked) => updateExecutionProfile(selectedTeamId, selectedExecutionProfileId, { enabled: checked === true })}
                          disabled={teamSaving}
                        />
                        有効
                      </label>
                      <div className="space-y-2 rounded border border-border bg-background/50 p-2">
                        <div className="text-[10px] font-medium">Default Subagent Route</div>
                        <ExecutionRouteEditor
                          route={selectedExecutionProfile.default_route}
                          onChange={(next) => updateExecutionProfile(selectedTeamId, selectedExecutionProfileId, { default_route: next })}
                          catalog={catalog}
                          disabled={teamSaving}
                          ariaPrefix="Default Subagent Route"
                          mainProvider={provider}
                          mainModel={selectedModelId}
                        />
                      </div>
                      <div className="space-y-2 rounded border border-border bg-background/50 p-2" role="group" aria-label="Execution Profile overrides">
                        <div className="text-[10px] font-medium">Overrides</div>
                        <p className="text-[10px] text-muted-foreground">未設定のメンバーは Default Subagent Route を使います。</p>
                        {selectedTeam.subagent_ids.map((subagentId) => {
                          const subagent = draft.subagents[subagentId];
                          const override = selectedExecutionProfile.overrides[subagentId];
                          return (
                            <div key={subagentId} className="space-y-2 rounded border border-border/70 p-2">
                              <label className="flex items-center gap-2 text-[10px] text-muted-foreground">
                                <Checkbox
                                  checked={Boolean(override)}
                                  onCheckedChange={(checked) => {
                                    const overrides = { ...selectedExecutionProfile.overrides };
                                    if (checked === true) {
                                      overrides[subagentId] = selectedExecutionProfile.default_route;
                                    } else {
                                      delete overrides[subagentId];
                                    }
                                    updateExecutionProfile(selectedTeamId, selectedExecutionProfileId, { overrides });
                                  }}
                                  disabled={teamSaving}
                                />
                                <span className="font-medium text-foreground">{subagent?.name || subagentId}</span>
                                <span>個別指定</span>
                              </label>
                              {override ? (
                                <ExecutionRouteEditor
                                  route={override}
                                  onChange={(next) => updateExecutionProfile(selectedTeamId, selectedExecutionProfileId, {
                                    overrides: { ...selectedExecutionProfile.overrides, [subagentId]: next },
                                  })}
                                  catalog={catalog}
                                  disabled={teamSaving}
                                  ariaPrefix={`${subagent?.name || subagentId} override`}
                                  mainProvider={provider}
                                  mainModel={selectedModelId}
                                />
                              ) : (
                                <p className="text-[10px] text-muted-foreground">Default を使用</p>
                              )}
                            </div>
                          );
                        })}
                        {!selectedTeam.subagent_ids.length && <p className="text-[10px] text-muted-foreground">所属Subagentがないため override はありません。</p>}
                      </div>
                    </div>
                  ) : null}
                </div>
              ) : (
                <p className="text-[10px] text-muted-foreground">Execution Profileはありません。「Profileを追加」から作成できます。None は Team に保存しません。</p>
              )}
            </div>
          </div> : <p className="rounded-md border border-dashed p-3 text-xs text-muted-foreground">Teamがありません。「Teamを追加」から作成できます。</p>}
        </div>
      </section></TabsContent>

      <TabsContent value="subagents"><section className="space-y-3 rounded-md border border-border bg-muted/35 p-3" aria-label="Subagents">
        <div className="flex items-center justify-between gap-2"><div><h3 className="text-xs font-medium">Subagents</h3><p className="text-[10px] text-muted-foreground">役割と指示だけを編集します。実行するLLMは Execution Profile で決めます。</p></div><Button type="button" size="sm" variant="outline" onClick={addSubagent} disabled={teamSaving}><Plus className="mr-1 size-3" />Subagentを追加</Button></div>
        <div className="grid gap-3 md:grid-cols-[220px_minmax(0,1fr)]">
          <aside className="space-y-1 border-b pb-3 md:border-b-0 md:border-r md:pb-0 md:pr-3" aria-label="Subagent一覧">
            <Input aria-label="Subagentを検索" value={subagentSearch} onChange={(event) => setSubagentSearch(event.target.value)} placeholder="Subagentを検索" className="h-8 text-xs" />
            {visibleSubagents.map(([id, subagent]) => <button key={id} type="button" onClick={() => setSelectedSubagentId(id)} aria-current={id === selectedSubagentId ? "true" : undefined} className={`w-full rounded-sm border-l-2 px-2 py-1.5 text-left text-[10px] ${id === selectedSubagentId ? "border-l-primary bg-primary/10" : "border-l-transparent hover:bg-muted"}`}><span className="block truncate font-medium">{subagent.name || "名称未設定"}</span><span className="block truncate text-muted-foreground">{subagent.enabled ? "有効" : "無効"}</span></button>)}
            {!visibleSubagents.length && <p className="p-2 text-[10px] text-muted-foreground">一致するSubagentはありません。</p>}
          </aside>
        {selectedSubagent ? <div className="min-w-0 space-y-3 rounded-md border bg-background/45 p-3">
          <div className="flex items-start gap-2"><Input value={selectedSubagent.name} onChange={(event) => updateSubagent(selectedSubagentId, { name: event.target.value })} aria-label="Subagent名" className="h-8 flex-1" disabled={teamSaving} /><Button type="button" size="icon" variant="ghost" aria-label="Subagentを削除" onClick={() => deleteSubagent(selectedSubagentId)} disabled={teamSaving}><Trash2 className="size-4" /></Button></div>
          <Textarea value={selectedSubagent.description} onChange={(event) => updateSubagent(selectedSubagentId, { description: event.target.value })} aria-label="Subagentの説明" placeholder="Subagentの説明" className="min-h-16 text-xs" disabled={teamSaving} />
          <Textarea value={selectedSubagent.instructions} onChange={(event) => updateSubagent(selectedSubagentId, { instructions: event.target.value })} aria-label="Subagentへの指示" placeholder="Subagentへの指示" className="min-h-20 text-xs" disabled={teamSaving} />
          <label className="flex items-center gap-2 text-[10px] text-muted-foreground"><Checkbox checked={selectedSubagent.enabled} onCheckedChange={(checked) => updateSubagent(selectedSubagentId, { enabled: checked === true })} disabled={teamSaving} />有効</label>
          <div className="space-y-2"><span className="text-[10px] text-muted-foreground">Capabilities</span><div className="flex flex-wrap gap-1">{selectedSubagent.capability_ids.map((capabilityId) => { const capability = capabilityCatalog[capabilityId]; return <Badge key={capabilityId} variant="secondary" className="gap-1 text-[9px]">{capability?.label || "権限"}<button type="button" className="ml-1" onClick={() => updateSubagent(selectedSubagentId, { capability_ids: selectedSubagent.capability_ids.filter((item) => item !== capabilityId) })} aria-label="Capabilityを外す">×</button></Badge>; })}</div>{Object.keys(capabilityCatalog).length > 0 && <AppSelect aria-label="Capabilityを追加" value="" onChange={(event) => { const id = event.target.value; if (id && !selectedSubagent.capability_ids.includes(id)) updateSubagent(selectedSubagentId, { capability_ids: [...selectedSubagent.capability_ids, id] }); }} disabled={teamSaving} className="h-8 w-full"><option value="">Capabilityを追加</option>{Object.entries(capabilityCatalog).filter(([id]) => !selectedSubagent.capability_ids.includes(id)).map(([id, capability]) => <option key={id} value={id}>{capability.label || capability.description || "権限"}</option>)}</AppSelect>}</div>
          <Button type="button" size="sm" variant="ghost" className="h-8 justify-start px-2 text-xs" onClick={() => setSubagentAdvancedOpen((value) => !value)} aria-expanded={subagentAdvancedOpen} aria-controls="subagent-advanced">{subagentAdvancedOpen ? <ChevronUp className="mr-1 size-3" /> : <ChevronDown className="mr-1 size-3" />}Advanced Configuration</Button>
          {subagentAdvancedOpen && <div id="subagent-advanced" className="space-y-3 rounded-md border bg-background/40 p-3">
          <div className="grid gap-2 sm:grid-cols-2"><label className="space-y-1 text-[10px] text-muted-foreground"><span>Workspaceアクセス</span><AppSelect aria-label="Workspaceアクセス" value={selectedSubagent.max_workspace_access} onChange={(event) => updateSubagent(selectedSubagentId, { max_workspace_access: event.target.value as AgentTeamSubagent["max_workspace_access"] })} disabled={teamSaving} className="h-8 w-full">{Object.entries(workspaceLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</AppSelect></label><label className="flex items-end gap-2 pb-1 text-[10px] text-muted-foreground"><Checkbox checked={selectedSubagent.allow_cli_native_tools} onCheckedChange={(checked) => updateSubagent(selectedSubagentId, { allow_cli_native_tools: checked === true })} disabled={teamSaving} />CLI native toolsを許可</label></div>
          <div className="grid gap-2 sm:grid-cols-3"><label className="flex items-center gap-2 text-[10px] text-muted-foreground sm:col-span-1"><Checkbox checked={selectedSubagent.scalable} onCheckedChange={(checked) => updateSubagent(selectedSubagentId, { scalable: checked === true })} disabled={teamSaving} />拡張可能</label><label className="space-y-1 text-[10px] text-muted-foreground"><span>既定インスタンス数</span><Input type="number" min={1} max={32} value={selectedSubagent.default_instances} onChange={(event) => updateSubagent(selectedSubagentId, { default_instances: Math.max(1, Math.min(32, Number(event.target.value) || 1)) })} className="h-8" disabled={teamSaving} /></label><label className="space-y-1 text-[10px] text-muted-foreground"><span>最大インスタンス数</span><Input type="number" min={1} max={32} value={selectedSubagent.max_instances} onChange={(event) => updateSubagent(selectedSubagentId, { max_instances: Math.max(1, Math.min(32, Number(event.target.value) || 1)) })} className="h-8" disabled={teamSaving} /></label></div>
          <div className="space-y-1 rounded border border-border dark:border-[#333335] bg-background/40 dark:bg-[#131313]/40 p-2"><div className="text-[10px] font-medium">所属Team</div><div className="flex flex-wrap gap-1">{sortedTeams.filter(([, team]) => team.subagent_ids.includes(selectedSubagentId)).map(([id, team]) => <Badge key={id} variant="outline" className="text-[9px]">{team.name || "名称未設定"}</Badge>)}{!sortedTeams.some(([, team]) => team.subagent_ids.includes(selectedSubagentId)) && <span className="text-[10px] text-muted-foreground">所属Teamはありません。</span>}</div></div>
          </div>}
        </div> : <p className="rounded-md border border-dashed p-3 text-xs text-muted-foreground">Subagentがありません。「Subagentを追加」から作成できます。</p>}
        </div>
      </section></TabsContent>

      <TabsContent value="director"><section className="space-y-3 rounded-md border border-border bg-muted/35 p-3" aria-labelledby="agent-team-orchestration">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <label className="flex items-start gap-2 text-xs"><Checkbox checked={directorEnabled} onCheckedChange={(checked) => setOrchestrationMode(checked === true ? "director" : "standard")} disabled={!delegationEnabled || savingRouting} className="mt-0.5" /><span><span className="font-medium">Web版ChatGPTをDirectorとして使用する</span><span className="mt-0.5 block text-[10px] text-muted-foreground">OFFの場合はAoiTalkが通常どおりAgent Teamを実行します。</span></span></label>
          {delegationEnabled && directorEnabled && <Badge role="status" variant={connectionPresentation.tone === "error" ? "destructive" : connectionPresentation.tone === "warning" ? "outline" : "secondary"}>{connectionPresentation.label}</Badge>}
        </div>
          {delegationEnabled && directorEnabled && <div className="space-y-3 rounded-md border border-border dark:border-[#333335] bg-muted/65 dark:bg-[#242426]/65 p-3">
          <p className="text-[10px] text-muted-foreground">会話履歴、今回の入力、選択した添付ファイルをWeb版ChatGPTへ送信します。</p>
          <div className="flex flex-wrap gap-2"><Button type="button" size="sm" variant="outline" onClick={() => void runConnectionAction("open")} disabled={Boolean(connectionAction) || connectionStatus?.busy}>{connectionAction === "open" ? <Loader2 className="mr-1 size-3 animate-spin" /> : <Globe2 className="mr-1 size-3" />}ChatGPTを設定</Button><Button type="button" size="sm" variant="outline" onClick={() => void runConnectionAction("test")} disabled={Boolean(connectionAction) || connectionStatus?.busy}>{connectionAction === "test" ? <Loader2 className="mr-1 size-3 animate-spin" /> : <RefreshCw className="mr-1 size-3" />}接続を確認</Button></div>
          <Button type="button" size="sm" variant="ghost" className="h-8 justify-start px-2 text-xs" onClick={() => setDirectorDetailsOpen((value) => !value)} aria-expanded={directorDetailsOpen}>{directorDetailsOpen ? <ChevronUp className="mr-1 size-3" /> : <ChevronDown className="mr-1 size-3" />}Directorの詳細設定</Button>
          {directorDetailsOpen && <div className="grid gap-2 md:grid-cols-2"><label className="space-y-1 text-[10px] text-muted-foreground md:col-span-2"><span>ログイン情報の保存先</span><Input aria-label="ChatGPT会話プロファイル" value={chatgptWeb.profile_dir} onChange={(event) => setChatgptWeb((value) => ({ ...value, profile_dir: event.target.value }))} disabled={savingRouting} className="h-8" /></label><label className="space-y-1 text-[10px] text-muted-foreground"><span>回答待ち時間（秒）</span><Input type="number" min={1} max={3600} value={chatgptWeb.response_timeout_seconds} onChange={(event) => setChatgptWeb((value) => ({ ...value, response_timeout_seconds: Math.max(1, Math.min(3600, Number(event.target.value) || 1)) }))} disabled={savingRouting} className="h-8" /></label><label className="space-y-1 text-[10px] text-muted-foreground"><span>最大往復回数</span><Input type="number" min={1} max={100} value={chatgptWeb.max_rounds_per_turn} onChange={(event) => setChatgptWeb((value) => ({ ...value, max_rounds_per_turn: Math.max(1, Math.min(100, Number(event.target.value) || 1)) }))} disabled={savingRouting} className="h-8" /></label></div>}
          {connectionStatus?.message && <p className="text-xs text-muted-foreground">{connectionStatus.message}</p>}
        </div>}
      </section></TabsContent>
      </Tabs>

    </div>
  );
}
