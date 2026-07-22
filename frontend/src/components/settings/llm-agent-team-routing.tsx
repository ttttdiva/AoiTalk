"use client";

import type { Dispatch, DragEvent, SetStateAction } from "react";
import { ChevronDown, ChevronUp, Loader2, Save, Trash2 } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  defaultModeForOptions,
  effortPolicyLabel,
  providerSelection,
  EXTERNAL_AGENT_PROVIDERS,
  MODEL_ROUTE_DEFINITIONS,
  type AgentTeamModelGroup,
  type EffortPolicy,
  type LlmModelCatalogResponse,
  type ModelRouteDefinition,
  type ModelRouteDraft,
  type ModelRouteKey,
} from "./llm-model-section-types";

type AgentTeamRoutingProps = {
  catalog: LlmModelCatalogResponse;
  provider: string;
  selectedModelId: string;
  current: LlmModelCatalogResponse["current"] | undefined;
  modelGroups: Record<string, AgentTeamModelGroup>;
  setModelGroups: Dispatch<SetStateAction<Record<string, AgentTeamModelGroup>>>;
  setRoutingDrafts: Dispatch<SetStateAction<Record<ModelRouteKey, ModelRouteDraft>>>;
  routingDrafts: Record<ModelRouteKey, ModelRouteDraft>;
  updateRoutingDraft: (key: ModelRouteKey, patch: Partial<ModelRouteDraft>) => void;
  routeProviderOptions: (definition: ModelRouteDefinition) => LlmModelCatalogResponse["providers"];
  handleMemberDrop: (event: DragEvent<HTMLElement>, definition: ModelRouteDefinition) => void;
  delegationEnabled: boolean;
  changeDelegationEnabled: (enabled: boolean) => void;
  routingConfirmPrompt: boolean;
  setRoutingConfirmPrompt: Dispatch<SetStateAction<boolean>>;
  routingNotify: boolean;
  setRoutingNotify: Dispatch<SetStateAction<boolean>>;
  agentTeamRedactionText: string;
  setAgentTeamRedactionText: Dispatch<SetStateAction<string>>;
  routingDetailsOpen: boolean;
  setRoutingDetailsOpen: Dispatch<SetStateAction<boolean>>;
  savingRouting: boolean;
  saveRoutingSettings: () => void;
};

export function LlmAgentTeamRouting(props: AgentTeamRoutingProps) {
  const {
    catalog,
    provider,
    selectedModelId,
    current,
    modelGroups,
    setModelGroups,
    setRoutingDrafts,
    routingDrafts,
    updateRoutingDraft,
    routeProviderOptions,
    handleMemberDrop,
    delegationEnabled,
    changeDelegationEnabled,
    routingConfirmPrompt,
    setRoutingConfirmPrompt,
    routingNotify,
    setRoutingNotify,
    agentTeamRedactionText,
    setAgentTeamRedactionText,
    routingDetailsOpen,
    setRoutingDetailsOpen,
    savingRouting,
    saveRoutingSettings,
  } = props;

  const effortOptionsForGroup = (group: AgentTeamModelGroup): string[] => {
    const effectiveProvider = group.provider || provider;
    const effectiveModel = group.model || selectedModelId;
    return catalog.providers
      .find((item) => item.id === effectiveProvider)
      ?.models.find((item) => item.id === effectiveModel)
      ?.reasoning_effort_options ?? [];
  };
  const hasInvalidExplicitGroup = Object.values(modelGroups).some((group) => {
    if (group.effort_policy !== "explicit") return false;
    const options = effortOptionsForGroup(group);
    return !options.length || !options.includes(group.effort || "");
  });

  return (
      <div className="space-y-3">
        <div className="flex items-center justify-between gap-3">
          <div>
            <div className="text-xs font-medium">Agent Team</div>
            <p className="text-[11px] text-muted-foreground">
              複数の専門ロールへ作業を委譲し、モデルグループ単位で実行モデルとeffortを管理します。
            </p>
          </div>
          {delegationEnabled && <Button
            size="sm"
            variant="outline"
            onClick={saveRoutingSettings}
            disabled={savingRouting || hasInvalidExplicitGroup}
          >
            {savingRouting ? (
              <Loader2 className="mr-1 size-3 animate-spin" />
            ) : (
              <Save className="mr-1 size-3" />
            )}
            保存
          </Button>}
        </div>

        <div className="space-y-3 rounded-md border p-3">
            <label className="flex items-start gap-2 text-xs">
              <Checkbox
                checked={delegationEnabled}
                onCheckedChange={(checked) => void changeDelegationEnabled(checked === true)}
                disabled={savingRouting}
                className="mt-0.5"
              />
              <span>
                <span className="font-medium">Agent Teamを有効にする</span>
                <span className="mt-0.5 block text-[10px] text-muted-foreground">
                  設計・調査・実装・レビューのロールで構成するAgent Teamと高度推論をメインモデルに公開します。ユーティリティ・メディア・Spotify・シナリオ・執筆・取り込みなど単独の専門エージェントは各設定に従い、この切り替えの影響を受けません。
                </span>
              </span>
            </label>

            {delegationEnabled && <>
            <div className="space-y-3">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div>
                  <div className="text-xs font-medium">モデルグループ</div>
                  <p className="text-[10px] text-muted-foreground">
                    provider/modelとは独立してeffort方針を設定します。高負荷と軽量は削除できません。
                  </p>
                </div>
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  disabled={savingRouting}
                  onClick={() => {
                    let suffix = Date.now();
                    let id = `group_${suffix}`;
                    while (modelGroups[id] || ["heavy", "light", "auto"].includes(id)) {
                      suffix += 1;
                      id = `group_${suffix}`;
                    }
                    setModelGroups((groups) => ({
                      ...groups,
                      [id]: { name: "新しいグループ", provider: "", model: "", effort_policy: "same", effort: "" },
                    }));
                  }}
                >
                  グループを追加
                </Button>
              </div>
              <div className="grid gap-3 lg:grid-cols-2">
                {Object.entries(modelGroups).map(([groupId, group]) => {
                  const builtin = groupId === "heavy" || groupId === "light";
                  const groupProvider = catalog.providers.find((item) => item.id === group.provider);
                  const modelProvider = groupProvider ?? catalog.providers.find((item) => item.id === provider);
                  const effortOptions = effortOptionsForGroup(group);
                  const selectedEffort = effortOptions.includes(group.effort || "") ? group.effort || "" : "";
                  const updateGroup = (patch: Partial<AgentTeamModelGroup>) =>
                    setModelGroups((groups) => ({
                      ...groups,
                      [groupId]: { ...groups[groupId], ...patch },
                    }));
                  return (
                    <div key={groupId} className="space-y-2 rounded-lg border p-3">
                      <div className="flex items-center gap-2">
                        {builtin ? (
                          <div className="flex h-8 flex-1 items-center text-sm font-medium">{groupId === "heavy" ? "高負荷" : "軽量"}</div>
                        ) : (
                          <Input
                            value={group.name ?? ""}
                            onChange={(event) => updateGroup({ name: event.target.value })}
                            aria-label="グループ名"
                            className="h-8 font-medium"
                          />
                        )}
                        <Badge variant="outline">{groupId}</Badge>
                        {!builtin && (
                          <Button
                            type="button"
                            size="icon"
                            variant="ghost"
                            aria-label={`${group.name || groupId}を削除`}
                            onClick={() => {
                              setModelGroups((groups) => Object.fromEntries(
                                Object.entries(groups).filter(([id]) => id !== groupId),
                              ));
                              setRoutingDrafts((drafts) => Object.fromEntries(
                                Object.entries(drafts).map(([key, draft]) => [key, {
                                  ...draft,
                                  groupId: draft.groupId === groupId ? "" : draft.groupId,
                                }]),
                              ) as Record<ModelRouteKey, ModelRouteDraft>);
                            }}
                          >
                            <Trash2 className="size-4" />
                          </Button>
                        )}
                      </div>
                      <div className="grid gap-2 sm:grid-cols-2">
                        <label className="space-y-1 text-[10px] text-muted-foreground">
                          <span>provider</span>
                          <select
                            value={group.provider ?? ""}
                            onChange={(event) => {
                              const nextProvider = catalog.providers.find((item) => item.id === event.target.value);
                              const nextModel = nextProvider?.models[0]?.id ?? "";
                              const nextOptions = event.target.value
                                ? nextProvider?.models[0]?.reasoning_effort_options ?? []
                                : catalog.providers.find((item) => item.id === provider)
                                  ?.models.find((item) => item.id === selectedModelId)
                                  ?.reasoning_effort_options ?? [];
                              updateGroup({
                                provider: event.target.value,
                                model: nextModel,
                                effort: nextOptions.includes(group.effort || "") ? group.effort : "",
                              });
                            }}
                            disabled={savingRouting}
                            className="h-8 w-full rounded-lg border border-input bg-transparent px-2.5 text-sm"
                          >
                            <option value="">メインを継承</option>
                            {catalog.providers.filter((item) => item.selection_kind !== "routing_profile" && item.id !== "claude" && item.id !== "grok").map((item) => (
                              <option key={item.id} value={item.id}>{item.label}</option>
                            ))}
                          </select>
                        </label>
                        <label className="space-y-1 text-[10px] text-muted-foreground">
                          <span>model</span>
                          <select
                            value={group.model ?? ""}
                            onChange={(event) => {
                              const effectiveModel = event.target.value || selectedModelId;
                              const options = modelProvider?.models.find((item) => item.id === effectiveModel)?.reasoning_effort_options ?? [];
                              updateGroup({
                                model: event.target.value,
                                effort: options.includes(group.effort || "") ? group.effort : "",
                              });
                            }}
                            disabled={savingRouting}
                            className="h-8 w-full rounded-lg border border-input bg-transparent px-2.5 text-sm"
                          >
                            {!group.provider && <option value="">メインを継承: {selectedModelId || "未設定"}</option>}
                            {(modelProvider?.models ?? []).map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}
                          </select>
                        </label>
                        <label className="space-y-1 text-[10px] text-muted-foreground">
                          <span>effort方針</span>
                          <select
                            value={group.effort_policy ?? (groupId === "light" ? "lower" : "same")}
                            onChange={(event) => updateGroup({ effort_policy: event.target.value as EffortPolicy })}
                            disabled={savingRouting}
                            className="h-8 w-full rounded-lg border border-input bg-transparent px-2.5 text-sm"
                          >
                            <option value="same">メインと同じ</option>
                            <option value="lower">メインより1段階低い</option>
                            <option value="explicit">明示指定</option>
                            <option value="default">指定なし／モデル既定</option>
                          </select>
                        </label>
                        {group.effort_policy === "explicit" && (
                          <label className="space-y-1 text-[10px] text-muted-foreground">
                            <span>明示effort</span>
                            {effortOptions.length ? (
                              <select
                                value={selectedEffort}
                                onChange={(event) => updateGroup({ effort: event.target.value })}
                                disabled={savingRouting}
                                className="h-8 w-full rounded-lg border border-input bg-transparent px-2.5 text-sm"
                              >
                                <option value="">選択してください</option>
                                {effortOptions.map((item) => <option key={item} value={item}>{item}</option>)}
                              </select>
                            ) : (
                              <div className="flex h-8 items-center text-xs text-muted-foreground">このモデルはeffort指定に対応していません</div>
                            )}
                          </label>
                        )}
                      </div>
                    </div>
                  );
                })}
              </div>
              {hasInvalidExplicitGroup && (
                <p className="text-xs text-destructive">明示effortが未選択、または現在のモデルでは無効です。</p>
              )}
            </div>

            <div className="flex flex-wrap items-center gap-4">
              <label className="flex items-center gap-2 text-xs">
                <Checkbox
                  checked={routingConfirmPrompt}
                  onCheckedChange={(checked) => setRoutingConfirmPrompt(checked === true)}
                  disabled={savingRouting}
                />
                外部送信前に確認する
              </label>
              <label className="flex items-center gap-2 text-xs">
                <Checkbox
                  checked={routingNotify}
                  onCheckedChange={(checked) => setRoutingNotify(checked === true)}
                  disabled={savingRouting}
                />
                確認時に通知する
              </label>
              <Button
                size="sm"
                variant="outline"
                onClick={saveRoutingSettings}
                disabled={savingRouting || hasInvalidExplicitGroup}
              >
                {savingRouting ? (
                  <Loader2 className="mr-1 size-3 animate-spin" />
                ) : (
                  <Save className="mr-1 size-3" />
                )}
                保存
              </Button>
            </div>

            <div className="space-y-1">
              <Label className="text-xs">外部モデル送信時に追加でマスクする語句</Label>
              <Input
                value={agentTeamRedactionText}
                onChange={(event) => setAgentTeamRedactionText(event.target.value)}
                placeholder="顧客名, 案件名, 社内コード"
                disabled={savingRouting}
                className="h-8"
              />
            </div>

            <div className="space-y-2">
              <div className="flex items-center justify-between gap-3 rounded border p-2">
                <span className="text-xs font-medium">メインエージェント</span>
                <Badge variant="secondary" className="max-w-[360px] truncate">
                  {current?.provider ?? "-"} / {current?.model ?? "-"}
                </Badge>
              </div>
              <Button
                type="button"
                size="sm"
                variant="ghost"
                className="h-8 justify-start px-2 text-xs"
                onClick={() => setRoutingDetailsOpen((value) => !value)}
              >
                {routingDetailsOpen ? (
                  <ChevronUp className="mr-1 size-3" />
                ) : (
                  <ChevronDown className="mr-1 size-3" />
                )}
                用途別の詳細設定
              </Button>
              {routingDetailsOpen && MODEL_ROUTE_DEFINITIONS.map((definition) => {
                const draft = routingDrafts[definition.key];
                const providers = routeProviderOptions(definition);
                const providerCatalog = catalog.providers.find((item) => item.id === draft.provider);
                const selectedRouteModelId = draft.customModel.trim() || draft.model;
                const selectedRouteModel = providerCatalog?.models.find(
                  (item) => item.id === selectedRouteModelId,
                );
                const modeOptions = selectedRouteModel?.reasoning_effort_options ?? [];
                const routeMode = modeOptions.includes(draft.mode)
                  ? draft.mode
                  : defaultModeForOptions(modeOptions, draft.mode);
                const selectedGroup = modelGroups[draft.groupId];
                const isAutoGroup = draft.groupId === "auto";
                const groupProvider = selectedGroup?.provider || "";
                const groupModel = selectedGroup?.model || "";
                const groupEffortPolicy = selectedGroup?.effort_policy;
                const groupEffortValue = selectedGroup?.effort;
                const displayedProvider = isAutoGroup ? "呼び出し時" : draft.provider || groupProvider || draft.effectiveProvider || "main";
                const displayedModel = isAutoGroup ? "高負荷／軽量を選択" : selectedRouteModelId || groupModel || draft.effectiveModel || "継承";
                const displayedEffort = isAutoGroup
                  ? "呼び出し時のグループ設定"
                  : draft.effortPolicy === "inherit"
                  ? groupEffortPolicy === "explicit"
                      ? (groupEffortValue || "モデル既定")
                      : groupEffortPolicy
                        ? effortPolicyLabel(groupEffortPolicy)
                        : (draft.effectiveEffort || "モデル既定")
                  : draft.effortPolicy === "explicit"
                    ? (draft.mode || "モデル既定")
                    : effortPolicyLabel(draft.effortPolicy);
                const inheritedTargetLabel = isAutoGroup
                  ? "呼び出し時のグループを継承"
                  : draft.groupId
                    ? `${selectedGroup?.name || draft.groupId}グループを継承`
                    : "メインを継承";
                return (
                  <div
                    key={definition.key}
                    data-testid={`agent-team-member-${definition.key}`}
                    onDragOver={(event) => event.preventDefault()}
                    onDrop={(event) => handleMemberDrop(event, definition)}
                    className="grid gap-3 rounded-lg border p-3 transition-colors hover:bg-accent/30"
                  >
                    <div className="flex flex-wrap items-start justify-between gap-3">
                    <label className="flex min-w-0 items-start gap-2 text-xs font-medium">
                      <Checkbox
                        checked={draft.enabled}
                        onCheckedChange={(checked) =>
                          updateRoutingDraft(definition.key, { enabled: checked === true })
                        }
                        disabled={savingRouting}
                      />
                      <span className="whitespace-normal break-words">{definition.label}</span>
                    </label>
                    <div className="flex flex-wrap gap-1">
                      <Badge variant={draft.enabled ? "secondary" : "outline"}>{draft.enabled ? "有効" : "無効"}</Badge>
                      {draft.enabled && EXTERNAL_AGENT_PROVIDERS.has(displayedProvider) && <Badge variant="secondary">外部送信</Badge>}
                    </div>
                    </div>
                    <div className="grid gap-2 text-xs sm:grid-cols-3">
                      <label className="space-y-1">
                        <span className="text-muted-foreground">所属グループ</span>
                        <select
                          value={draft.groupId}
                          onChange={(event) => updateRoutingDraft(definition.key, { groupId: event.target.value })}
                          className="h-8 w-full rounded-lg border border-input bg-transparent px-2.5 text-sm"
                        >
                          <option value="">メインを継承</option>
                          {definition.scalable && <option value="auto">自動（委譲ごとに高負荷／軽量を選択）</option>}
                          {Object.entries(modelGroups).map(([id, group]) => <option key={id} value={id}>{group.name || id}</option>)}
                        </select>
                      </label>
                      <div><span className="block text-muted-foreground">実効モデル</span><span className="break-all">{displayedProvider} / {displayedModel}</span></div>
                      <div><span className="block text-muted-foreground">実効effort</span><span>{displayedEffort}</span></div>
                    </div>
                    {definition.key === "agent_harness" && (
                      <p className="text-[10px] text-muted-foreground">作業エージェントはrunner制約のため、Codex CLI／Claude Code設定を維持する特例です。</p>
                    )}
                    <Button type="button" size="sm" variant="ghost" className="w-fit" onClick={() => updateRoutingDraft(definition.key, { overrideOpen: !draft.overrideOpen })}>
                      {draft.overrideOpen ? <ChevronUp className="mr-1 size-3" /> : <ChevronDown className="mr-1 size-3" />}
                      個別上書き
                    </Button>
                    {draft.overrideOpen ? (
                      <>
                        <select
                          value={draft.provider}
                          onChange={(event) => {
                            const nextProvider = event.target.value;
                            const next = providers.find((item) => item.id === nextProvider);
                            const selection = providerSelection(next);
                            const nextModelId = selection.customModel.trim() || selection.model;
                            const nextModel = next?.models.find((item) => item.id === nextModelId);
                            updateRoutingDraft(definition.key, {
                              provider: nextProvider,
                              model: selection.model,
                              customModel: selection.customModel,
                              mode: defaultModeForOptions(
                                nextModel?.reasoning_effort_options,
                                next?.settings?.reasoning_effort ?? draft.mode,
                              ),
                              runner: definition.key === "agent_harness" && nextProvider === "claude-cli"
                                ? "claude_code"
                                : definition.key === "agent_harness" && nextProvider === "codex-cli"
                                  ? "codex_exec"
                                  : draft.runner,
                            });
                          }}
                          disabled={savingRouting || Boolean(definition.allowedProviders?.length === 1)}
                          className="h-8 w-full rounded-lg border border-input bg-transparent px-2.5 text-sm outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 dark:bg-input/30"
                        >
                          {providers.map((item) => (
                            <option key={item.id} value={item.id}>
                              {item.label}
                            </option>
                          ))}
                        </select>
                        <div className="grid gap-2 md:grid-cols-2">
                          <select
                            value={draft.model}
                            onChange={(event) => {
                              const nextModel = event.target.value;
                              const nextOption = providerCatalog?.models.find((item) => item.id === nextModel);
                              updateRoutingDraft(definition.key, {
                                model: nextModel,
                                customModel: "",
                                mode: defaultModeForOptions(
                                  nextOption?.reasoning_effort_options,
                                  draft.mode,
                                ),
                              });
                            }}
                            disabled={savingRouting}
                            className="h-8 w-full rounded-lg border border-input bg-transparent px-2.5 text-sm outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 dark:bg-input/30"
                          >
                            {(providerCatalog?.models ?? []).map((item) => (
                              <option key={item.id} value={item.id}>
                                {item.label}
                              </option>
                            ))}
                          </select>
                          <Input
                            value={draft.customModel}
                            onChange={(event) =>
                              updateRoutingDraft(definition.key, { customModel: event.target.value })
                            }
                            placeholder="カスタムID"
                            disabled={savingRouting}
                            className="h-8"
                          />
                        </div>
                        <select
                          value={draft.effortPolicy}
                          onChange={(event) => updateRoutingDraft(definition.key, { effortPolicy: event.target.value as EffortPolicy })}
                          className="h-8 w-full rounded-lg border border-input bg-transparent px-2.5 text-sm"
                        >
                          <option value="inherit">グループ設定を継承</option>
                          <option value="same">メインと同じ</option>
                          <option value="lower">メインより1段階低い</option>
                          <option value="explicit">明示指定</option>
                          <option value="default">指定なし／モデル既定</option>
                        </select>
                        {draft.effortPolicy === "explicit" && modeOptions.length > 0 ? (
                          <select
                            value={routeMode}
                            onChange={(event) =>
                              updateRoutingDraft(definition.key, { mode: event.target.value })
                            }
                            disabled={savingRouting}
                            className="h-8 w-full rounded-lg border border-input bg-transparent px-2.5 text-sm outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 dark:bg-input/30"
                          >
                            {modeOptions.map((item) => (
                              <option key={item} value={item}>
                                {item}
                              </option>
                            ))}
                          </select>
                        ) : draft.effortPolicy === "explicit" ? (
                          <span className="flex h-8 items-center text-xs text-muted-foreground">
                            このモデルはeffort指定に対応していません
                          </span>
                        ) : null}
                      </>
                    ) : (
                      <span className="col-span-full flex h-8 items-center text-xs text-muted-foreground md:col-span-3">
                        {inheritedTargetLabel}
                      </span>
                    )}
                    <div className="grid gap-2">
                      {definition.scalable ? (
                        <div className="grid grid-cols-[1fr_64px] items-center gap-2">
                          <span className="text-xs text-muted-foreground">自動増員</span>
                          <Input
                            type="number"
                            min={1}
                            max={32}
                            value={draft.maxInstances}
                            onChange={(event) => {
                              const next = Math.max(1, Math.min(32, Number(event.target.value) || 1));
                              updateRoutingDraft(definition.key, {
                                scalable: true,
                                maxInstances: next,
                                defaultInstances: Math.min(draft.defaultInstances || 1, next),
                              });
                            }}
                            disabled={savingRouting}
                            className="h-8"
                          />
                        </div>
                      ) : (
                        <span className="flex h-8 items-center text-xs text-muted-foreground">
                          単体
                        </span>
                      )}
                      {definition.key === "agent_harness" && (
                        <select
                          value={draft.runner || "codex_exec"}
                          onChange={(event) => {
                            const nextRunner = event.target.value;
                            const nextProvider =
                              nextRunner === "claude_code"
                                ? "claude-cli"
                                : nextRunner === "codex_exec"
                                  ? "codex-cli"
                                  : draft.provider;
                            const next = providers.find((item) => item.id === nextProvider);
                            const selection =
                              nextProvider === draft.provider
                                ? { model: draft.model, customModel: draft.customModel }
                                : providerSelection(next);
                            const nextModelId = selection.customModel.trim() || selection.model;
                            const nextModel = next?.models.find((item) => item.id === nextModelId);
                            updateRoutingDraft(definition.key, {
                              runner: nextRunner,
                              provider: nextProvider,
                              model: selection.model,
                              customModel: selection.customModel,
                              mode: defaultModeForOptions(
                                nextModel?.reasoning_effort_options,
                                next?.settings?.reasoning_effort ?? draft.mode,
                              ),
                            });
                          }}
                          disabled={savingRouting}
                          className="h-8 w-full rounded-lg border border-input bg-transparent px-2.5 text-sm outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 dark:bg-input/30"
                        >
                          <option value="codex_exec">Codex CLI</option>
                          <option value="claude_code">Claude Code</option>
                          <option value="custom_command">Custom</option>
                        </select>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
            </>}
          </div>
      </div>
  );
}
