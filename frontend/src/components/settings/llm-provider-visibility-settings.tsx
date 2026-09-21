"use client";

import { useEffect, useState } from "react";
import { Loader2 } from "lucide-react";
import { toast } from "sonner";
import { Checkbox } from "@/components/ui/checkbox";
import {
  isProviderHidden,
  normalizeHiddenProviderIds,
  normalizeProviderId,
} from "@/lib/llm-provider-visibility";
import type { LlmProviderCatalog } from "./llm-model-section-types";

function providerVisibilityLabel(provider: LlmProviderCatalog): string {
  if (provider.selection_kind !== "routing_profile" && provider.id !== "routing-profile") {
    return provider.label;
  }

  const configuredModel = provider.configured_model?.trim();
  return (
    provider.models.find((model) => model.id === configuredModel)?.label?.trim() ||
    provider.models[0]?.label?.trim() ||
    "無料Team"
  );
}

type GlobalSettingsPayload = {
  settings?: Record<string, unknown>;
};

/**
 * Render the global provider visibility controls. The catalog is deliberately
 * not filtered here: an admin must always be able to find and re-enable a
 * provider that is currently hidden. The backend's existing /api/settings
 * administrator gate is authoritative; the disabled control is only a UX
 * affordance for non-admins.
 */
export function LlmProviderVisibilitySettings({
  providers,
  globalVisibility,
  isAdmin = true,
}: {
  providers: LlmProviderCatalog[];
  globalVisibility?: unknown;
  isAdmin?: boolean;
}) {
  const [hiddenProviderIds, setHiddenProviderIds] = useState<string[]>(() =>
    normalizeHiddenProviderIds(globalVisibility),
  );
  const [saving, setSaving] = useState(false);
  const [loaded, setLoaded] = useState(globalVisibility !== undefined);

  useEffect(() => {
    if (globalVisibility === undefined) return;
    setHiddenProviderIds(normalizeHiddenProviderIds(globalVisibility));
    setLoaded(true);
  }, [globalVisibility]);

  useEffect(() => {
    let active = true;
    fetch("/api/python-proxy/settings", { credentials: "include" })
      .then(async (response) => {
        if (!response.ok) throw new Error(`API Error: ${response.status}`);
        return (await response.json()) as GlobalSettingsPayload;
      })
      .then((payload) => {
        if (!active) return;
        setHiddenProviderIds(
          normalizeHiddenProviderIds(payload.settings ?? payload),
        );
        setLoaded(true);
      })
      .catch(() => {
        // The catalog metadata remains a valid initial snapshot. Keep the
        // controls usable if this auxiliary GET is unavailable.
        if (active) setLoaded(true);
      });
    return () => {
      active = false;
    };
  }, []);

  async function handleProviderVisibilityChange(
    providerId: string,
    checked: boolean | "indeterminate",
  ) {
    if (saving || !isAdmin) return;
    const normalizedId = normalizeProviderId(providerId);
    if (!normalizedId) return;

    const next = new Set(hiddenProviderIds);
    if (checked === true) next.delete(normalizedId);
    else next.add(normalizedId);
    const nextIds = Array.from(next);
    const previousIds = hiddenProviderIds;
    setHiddenProviderIds(nextIds);

    setSaving(true);
    try {
      const response = await fetch("/api/python-proxy/settings", {
        method: "PATCH",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          key: "llm_provider_visibility.hidden_provider_ids",
          value: nextIds,
        }),
      });
      if (!response.ok) {
        const detail = (await response.json().catch(() => null)) as
          | { detail?: string }
          | null;
        throw new Error(detail?.detail || `API Error: ${response.status}`);
      }
    } catch (error) {
      setHiddenProviderIds(previousIds);
      toast.error(
        error instanceof Error
          ? error.message
          : "プロバイダー表示設定を保存できませんでした",
      );
    } finally {
      setSaving(false);
    }
  }

  return (
    <details className="rounded-md border">
      <summary className="flex cursor-pointer items-center justify-between gap-2 p-3 text-xs font-medium">
        <span className="min-w-0">
          <span className="block truncate">ヘッダーのLLMプロバイダー表示</span>
          <span className="mt-1 block text-[10px] text-muted-foreground">
            管理者が全ユーザー共通の表示を管理
          </span>
        </span>
        {saving && (
          <span className="inline-flex shrink-0 items-center gap-1 text-[10px] text-muted-foreground">
            <Loader2 className="size-3 animate-spin" />
            保存中...
          </span>
        )}
      </summary>

      <div className="space-y-3 border-t p-3">
        <p className="text-[10px] text-muted-foreground">
          チェックを外したプロバイダーは通常のチャット選択肢から非表示になります。
          接続設定、APIキー、現在のモデル設定、API利用権限は変更されません。
          {isAdmin ? "" : "（表示設定の変更は管理者のみ可能です。）"}
        </p>

        <div className="grid gap-2 sm:grid-cols-2">
          {providers.map((provider) => {
            const hidden = isProviderHidden(provider.id, {
              hidden_provider_ids: hiddenProviderIds,
            });
            const label = providerVisibilityLabel(provider);
            return (
              <div
                key={provider.id}
                className="flex items-start gap-2 rounded border px-2.5 py-2 text-xs"
              >
                <Checkbox
                  checked={!hidden}
                  onCheckedChange={(checked) =>
                    void handleProviderVisibilityChange(provider.id, checked)
                  }
                  disabled={!loaded || saving || !isAdmin}
                  aria-label={`${label}を表示`}
                  className="mt-0.5"
                />
                <span className="min-w-0">
                  <span className="block truncate font-medium">{label}</span>
                  <span className="block truncate text-[10px] text-muted-foreground">
                    {provider.id === "routing-profile"
                      ? "routing-profile（仮想ルーティング）"
                      : provider.id}
                  </span>
                </span>
              </div>
            );
          })}
        </div>
      </div>
    </details>
  );
}
