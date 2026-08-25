import type { ChatResponseModelOption } from "../../types/api";
import {
  getProviderLabel,
  type DirectMobileLlmProvider,
} from "../../lib/cloud-model-catalog";

/** 1 段目のプロバイダー選択で扱う Direct モデルの最小形。 */
export interface ModelPickerDirectOption {
  provider: DirectMobileLlmProvider;
  model: string;
  /** 選択中の Direct モデルを一覧先頭へ寄せるための印。 */
  isCurrent?: boolean;
}

/** 2 段目に並ぶ個別モデル。 */
export interface ModelPickerEntry {
  kind: "direct" | "server";
  provider: string;
  model: string;
  label: string;
  /** 同じ provider/model が両 route にある場合にだけ表示する補助情報。 */
  routeLabel?: string;
  isCurrent?: boolean;
}

/** 1 段目に並ぶプロバイダーグループ。 */
export interface ModelPickerGroup {
  /** provider のみを識別子に使い、route を分類軸へ露出しない。 */
  key: string;
  kind: "direct" | "server" | "mixed";
  provider: string;
  label: string;
  models: ModelPickerEntry[];
}

/**
 * Direct モデルと Server カタログをプロバイダー単位でグルーピングする。
 *
 * Direct と Server は内部の execution route として entry に残すが、
 * 1 段目の主ラベルは常に provider 名、2 段目の主ラベルは model 名にする。
 * 同じ provider/model が両 route に存在する場合のみ小さな補助ラベルで区別する。
 */
export function buildModelPickerGroups(
  directOptions: ModelPickerDirectOption[],
  serverOptions: ChatResponseModelOption[],
): ModelPickerGroup[] {
  const groups: ModelPickerGroup[] = [];
  const byProvider = new Map<string, ModelPickerGroup>();

  const ensureGroup = (provider: string, label: string): ModelPickerGroup => {
    const key = provider.trim() || "unknown";
    let group = byProvider.get(key);
    if (!group) {
      group = {
        key,
        kind: "server",
        provider: key,
        label,
        models: [],
      };
      byProvider.set(key, group);
      groups.push(group);
    }
    return group;
  };

  for (const option of directOptions) {
    const provider = option.provider;
    const group = ensureGroup(provider, getProviderLabel(provider));
    group.kind = group.kind === "server" && group.models.length > 0
      ? "mixed"
      : group.kind === "server"
        ? "direct"
        : "mixed";
    const existing = group.models.find(
      (entry) => entry.kind === "direct" && entry.model === option.model,
    );
    if (existing) {
      existing.isCurrent = existing.isCurrent || option.isCurrent;
      continue;
    }
    group.models.push({
      kind: "direct",
      provider,
      model: option.model,
      label: option.model,
      isCurrent: option.isCurrent,
    });
  }

  for (const option of serverOptions) {
    const provider = option.provider;
    const group = ensureGroup(provider, option.providerLabel || provider);
    group.kind = group.kind === "direct" ? "mixed" : "server";
    const existing = group.models.find(
      (entry) => entry.kind === "server" && entry.model === option.model,
    );
    if (existing) {
      existing.isCurrent = existing.isCurrent || option.isCurrent;
      continue;
    }
    group.models.push({
      kind: "server",
      provider,
      model: option.model,
      label: option.isCurrent
        ? `${option.modelLabel || option.model} (現在)`
        : option.modelLabel || option.model,
      isCurrent: option.isCurrent,
    });
  }

  for (const group of groups) {
    const hasBothRoutes =
      group.models.some((entry) => entry.kind === "direct") &&
      group.models.some((entry) => entry.kind === "server");
    if (hasBothRoutes) group.kind = "mixed";
    group.models.sort((a, b) => {
      const aCurrent = a.isCurrent ? 1 : 0;
      const bCurrent = b.isCurrent ? 1 : 0;
      if (aCurrent !== bCurrent) return bCurrent - aCurrent;
      // 同じ model が両 route にある場合、Direct を先に提示するが、
      // これは表示順だけであり選択対象の route は entry.kind を維持する。
      if (a.model === b.model && a.kind !== b.kind) {
        return a.kind === "direct" ? -1 : 1;
      }
      return a.label.localeCompare(b.label);
    });
    const routeByModel = new Map<string, Set<ModelPickerEntry["kind"]>>();
    for (const entry of group.models) {
      const routes = routeByModel.get(entry.model) ?? new Set();
      routes.add(entry.kind);
      routeByModel.set(entry.model, routes);
    }
    for (const entry of group.models) {
      const routes = routeByModel.get(entry.model);
      if (routes?.size === 2) {
        entry.routeLabel = entry.kind === "direct" ? "端末から直接" : "サーバー経由";
      }
    }
  }

  return groups;
}
