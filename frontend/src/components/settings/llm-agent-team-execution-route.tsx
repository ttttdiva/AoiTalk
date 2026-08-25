"use client";

import { useEffect, useMemo } from "react";
import { AppSelect } from "@/components/ui/app-select";
import {
  canonicalExecutionRoute,
  reasoningEffortOptionsForModel,
  type ExecutionRoute,
  type LlmModelCatalogResponse,
} from "./llm-model-section-types";

type RoutePreset = "inherit_same" | "inherit_lower" | "inherit_explicit" | "explicit_model";

const routePresetLabels: Record<RoutePreset, string> = {
  inherit_same: "Mainと完全に同じ",
  inherit_lower: "Mainと同じモデルでeffortを1段下げる",
  inherit_explicit: "Mainと同じモデルでeffortを明示",
  explicit_model: "別のprovider/modelを指定",
};

function uniqueEffortValues(values: readonly string[] | undefined): string[] {
  return [...new Set((values ?? []).map((item) => item.trim()).filter(Boolean))];
}

function catalogProvider(catalog: LlmModelCatalogResponse, providerId: string) {
  return catalog.providers.find((item) => item.id === providerId);
}

function routeEffortOptions(
  catalog: LlmModelCatalogResponse,
  providerId: string,
  modelId: string,
): string[] {
  return uniqueEffortValues(reasoningEffortOptionsForModel(catalogProvider(catalog, providerId), modelId));
}

function modelHasCatalogEntry(
  catalog: LlmModelCatalogResponse,
  providerId: string,
  modelId: string,
): boolean {
  return Boolean(modelId && catalogProvider(catalog, providerId)?.models.some((item) => item.id === modelId));
}

const CATALOG_EFFORT_ORDER = [
  "none",
  "minimal",
  "low",
  "medium",
  "high",
  "xhigh",
  "max",
  "ultra",
  "fast",
  "thinking",
];

function catalogEffortUnion(catalog: LlmModelCatalogResponse): string[] {
  const values = uniqueEffortValues(
    catalog.providers.flatMap((provider) =>
      (provider.models ?? []).flatMap((model) => model.reasoning_effort_options ?? []),
    ),
  );
  return values.sort((left, right) => {
    const leftRank = CATALOG_EFFORT_ORDER.indexOf(left);
    const rightRank = CATALOG_EFFORT_ORDER.indexOf(right);
    if (leftRank === -1 && rightRank === -1) return left.localeCompare(right);
    if (leftRank === -1) return 1;
    if (rightRank === -1) return -1;
    return leftRank - rightRank;
  });
}

function inheritEffortChoices(
  catalog: LlmModelCatalogResponse,
  currentEffort: string,
  mainProvider: string,
  mainModel: string,
): string[] {
  // Main-inherited effort is still request-scoped to Main's model.  Prefer
  // that model's catalog contract so a managed profile (for example Qwen3.8)
  // cannot expose values from unrelated providers.  Preserve the historical
  // union only while Main is not selected/known yet (e.g. a fresh Team draft).
  const mainOptions = routeEffortOptions(catalog, mainProvider, mainModel);
  if (mainProvider.trim() && mainModel.trim() && mainOptions.length > 0) {
    return mainOptions;
  }
  const values = catalogEffortUnion(catalog);
  if (currentEffort && !values.includes(currentEffort)) values.push(currentEffort);
  return values;
}

function explicitModelRoute(
  current: ExecutionRoute,
  patch: { provider?: string; model?: string; effort?: string },
  catalog: LlmModelCatalogResponse,
): ExecutionRoute {
  const providerChanged = patch.provider !== undefined && patch.provider !== current.provider;
  const modelChanged = patch.model !== undefined && patch.model !== current.model;
  const provider = patch.provider ?? current.provider;
  const model = patch.model !== undefined ? patch.model : current.model;
  if (providerChanged || modelChanged) {
    return canonicalExecutionRoute({
      inherit_model: false,
      provider,
      model,
      // Selecting/changing a model must start from that model's own default;
      // never carry an effort that belonged to the previous model.
      effort_policy: "default",
      effort: "",
    });
  }

  const options = routeEffortOptions(catalog, provider, model);
  if (patch.effort !== undefined) {
    const requested = String(patch.effort || "").trim();
    if (requested && options.includes(requested)) {
      return canonicalExecutionRoute({
        inherit_model: false,
        provider,
        model,
        effort_policy: "explicit",
        effort: requested,
      });
    }
    // The empty option is the canonical model-default choice.  Unsupported
    // values are also cleared rather than remapped to a fixed fallback.
    return canonicalExecutionRoute({ inherit_model: false, provider, model, effort_policy: "default", effort: "" });
  }

  const currentEffort = String(current.effort || "").trim();
  if (current.effort_policy === "explicit" && currentEffort && options.includes(currentEffort)) {
    return canonicalExecutionRoute({
      inherit_model: false,
      provider,
      model,
      effort_policy: "explicit",
      effort: currentEffort,
    });
  }
  return canonicalExecutionRoute({
    inherit_model: false,
    provider,
    model,
    effort_policy: "default",
    effort: "",
  });
}

function routePreset(route: ExecutionRoute): RoutePreset {
  if (!route.inherit_model) return "explicit_model";
  if (route.effort_policy === "lower") return "inherit_lower";
  if (route.effort_policy === "explicit") return "inherit_explicit";
  return "inherit_same";
}

function routeFromPreset(
  preset: RoutePreset,
  current: ExecutionRoute,
  catalog: LlmModelCatalogResponse,
): ExecutionRoute {
  if (preset === "inherit_same") {
    return canonicalExecutionRoute({ inherit_model: true, effort_policy: "same" });
  }
  if (preset === "inherit_lower") {
    return canonicalExecutionRoute({ inherit_model: true, effort_policy: "lower" });
  }
  if (preset === "inherit_explicit") {
    return canonicalExecutionRoute({
      inherit_model: true,
      effort_policy: "explicit",
      effort: current.effort,
    });
  }
  return explicitModelRoute(current, {}, catalog);
}

export function ExecutionRouteEditor({
  route,
  onChange,
  catalog,
  disabled,
  ariaPrefix,
  mainProvider,
  mainModel,
}: {
  route: ExecutionRoute;
  onChange: (next: ExecutionRoute) => void;
  catalog: LlmModelCatalogResponse;
  disabled: boolean;
  ariaPrefix: string;
  /** Kept for call-site compatibility. Inherit effort is resolved at runtime against Chat Main. */
  mainProvider: string;
  mainModel: string;
}) {
  const availableProviders = useMemo(
    () => catalog.providers.filter((item) => item.selection_kind !== "routing_profile" && item.disabled !== true && item.unavailable !== true),
    [catalog.providers],
  );
  const providerCatalog = availableProviders.find((item) => item.id === route.provider);
  const modelCatalog = providerCatalog?.models ?? [];
  const inheritEffortValues = useMemo(
    () => inheritEffortChoices(catalog, route.effort, mainProvider, mainModel),
    [catalog, mainModel, mainProvider, route.effort],
  );
  const routeEffortValues = route.model
    ? routeEffortOptions(catalog, route.provider, route.model)
    : [];
  const modelKnown = modelHasCatalogEntry(catalog, route.provider, route.model);
  const effortChoices = route.inherit_model ? inheritEffortValues : routeEffortValues;
  const preset = routePreset(route);
  const showModelFields = preset === "explicit_model";
  const showExplicitEffort = preset === "inherit_explicit"
    ? inheritEffortValues.length > 0
    : preset === "explicit_model" && routeEffortValues.length > 0;
  const showUnsupportedEffort = showModelFields && Boolean(route.model) && modelKnown && routeEffortValues.length === 0;

  useEffect(() => {
    if (route.inherit_model) return;
    const next = explicitModelRoute(route, {}, catalog);
    if (
      next.provider === route.provider
      && next.model === route.model
      && next.effort_policy === route.effort_policy
      && next.effort === route.effort
    ) {
      return;
    }
    onChange(next);
    // Parent onChange identity is not stable; compare route fields instead.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [catalog, route.effort, route.effort_policy, route.inherit_model, route.model, route.provider]);

  return (
    <div className="space-y-2">
      <label className="space-y-1 text-[10px] text-muted-foreground">
        <span>実行ルート</span>
        <AppSelect
          aria-label={`${ariaPrefix}の実行ルート`}
          value={preset}
          onChange={(event) => onChange(routeFromPreset(event.target.value as RoutePreset, route, catalog))}
          disabled={disabled}
          className="h-8 w-full"
        >
          {Object.entries(routePresetLabels).map(([value, label]) => (
            <option key={value} value={value}>{label}</option>
          ))}
        </AppSelect>
      </label>
      {showModelFields && (
        <div className="grid gap-2 md:grid-cols-2">
          <label className="space-y-1 text-[10px] text-muted-foreground">
            <span>プロバイダー</span>
            <AppSelect
              aria-label={`${ariaPrefix}のプロバイダー`}
              value={route.provider}
              onChange={(event) => onChange(explicitModelRoute(route, { provider: event.target.value, model: "" }, catalog))}
              disabled={disabled}
              className="h-8 w-full"
            >
              <option value="">プロバイダーを選択</option>
              {availableProviders.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}
              {route.provider && !availableProviders.some((item) => item.id === route.provider) && <option value={route.provider}>現在の設定</option>}
            </AppSelect>
          </label>
          <label className="space-y-1 text-[10px] text-muted-foreground">
            <span>モデル</span>
            <AppSelect
              aria-label={`${ariaPrefix}のモデル`}
              value={route.model}
              onChange={(event) => onChange(explicitModelRoute(route, { model: event.target.value }, catalog))}
              disabled={disabled || !route.provider}
              className="h-8 w-full"
            >
              <option value="">モデルを選択</option>
              {modelCatalog.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}
              {route.model && !modelCatalog.some((item) => item.id === route.model) && <option value={route.model}>現在の設定</option>}
            </AppSelect>
          </label>
        </div>
      )}
      {showExplicitEffort && (
        <label className="space-y-1 text-[10px] text-muted-foreground">
          <span>{preset === "explicit_model" ? "推論の強さ（モデル既定 / 明示）" : "推論の強さ（明示）"}</span>
          <AppSelect
            aria-label={`${ariaPrefix}の明示的な推論の強さ`}
            value={route.effort}
            onChange={(event) => onChange(
              route.inherit_model
                ? canonicalExecutionRoute({ ...route, effort_policy: "explicit", effort: event.target.value })
                : explicitModelRoute(route, { effort: event.target.value }, catalog),
            )}
            disabled={disabled}
            className="h-8 w-full"
          >
            {preset === "explicit_model" && <option value="">モデルのデフォルト</option>}
            {preset !== "explicit_model" && <option value="">選択してください</option>}
            {effortChoices.map((value) => <option key={value} value={value}>{value}</option>)}
          </AppSelect>
        </label>
      )}
      {showUnsupportedEffort && (
        <p className="text-[10px] text-muted-foreground">このモデルは推論の強さの明示指定に対応していません。</p>
      )}
    </div>
  );
}
