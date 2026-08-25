"use client";

import { useState } from "react";
import { Loader2 } from "lucide-react";
import { toast } from "sonner";
import { Checkbox } from "@/components/ui/checkbox";
import { useUserSettings } from "@/contexts/user-settings-context";
import {
  LLM_PROVIDER_VISIBILITY_KEY,
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

export function LlmProviderVisibilitySettings({
  providers,
}: {
  providers: LlmProviderCatalog[];
}) {
  const { settings, patch } = useUserSettings();
  const [saving, setSaving] = useState(false);

  async function handleProviderVisibilityChange(
    providerId: string,
    checked: boolean | "indeterminate",
  ) {
    if (saving) return;
    const normalizedId = normalizeProviderId(providerId);
    if (!normalizedId) return;

    const hiddenProviderIds = new Set(normalizeHiddenProviderIds(settings));
    if (checked === true) hiddenProviderIds.delete(normalizedId);
    else hiddenProviderIds.add(normalizedId);

    setSaving(true);
    try {
      await patch({
        [LLM_PROVIDER_VISIBILITY_KEY]: {
          hidden_provider_ids: Array.from(hiddenProviderIds),
        },
      });
    } catch (error) {
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
          <span className="block truncate">
            ヘッダーのLLMプロバイダー表示
          </span>
          <span className="mt-1 block text-[10px] text-muted-foreground">
            非表示にするプロバイダーを管理
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
          チェックを外したプロバイダーは、ヘッダーのLLMエンジン選択から非表示になります。
          接続設定、APIキー、現在のモデル設定は削除されません。
        </p>

        <div className="grid gap-2 sm:grid-cols-2">
          {providers.map((provider) => {
            const hidden = isProviderHidden(provider.id, settings);
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
                  disabled={saving}
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
