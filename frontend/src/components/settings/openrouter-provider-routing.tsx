"use client";

import { useEffect, useMemo, useState } from "react";
import { Loader2, MoveDown, MoveUp } from "lucide-react";
import { toast } from "sonner";
import { AppSelect } from "@/components/ui/app-select";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Label } from "@/components/ui/label";
import { pyFetch } from "./llm-model-section-types";

export type OpenRouterProviderRoutingMode = "auto" | "order" | "only";

export type OpenRouterProviderRouting = {
  only?: string[];
  order?: string[];
  allow_fallbacks?: boolean;
  zdr?: boolean;
};

export type OpenRouterProviderCandidate = {
  slug: string;
  label: string;
};

type OpenRouterProviderRoutingResponse = {
  model: string;
  provider?: OpenRouterProviderRouting;
  providers?: OpenRouterProviderCandidate[];
};

export function providerRoutingState(policy: OpenRouterProviderRouting | undefined) {
  const normalized = policy ?? {};
  const only = normalized.only?.filter(Boolean) ?? [];
  const order = normalized.order?.filter(Boolean) ?? [];
  return {
    mode: (only.length ? "only" : order.length ? "order" : "auto") as OpenRouterProviderRoutingMode,
    selected: only.length ? only : order,
    allowFallbacks: normalized.allow_fallbacks ?? true,
    zdr: normalized.zdr === true,
  };
}

export function buildProviderRoutingPayload(
  mode: OpenRouterProviderRoutingMode,
  selected: string[],
  allowFallbacks: boolean,
  zdr: boolean,
): OpenRouterProviderRouting {
  const unique = selected.filter((slug, index) => slug && selected.indexOf(slug) === index);
  const payload: OpenRouterProviderRouting = {};
  if (mode === "only" && unique.length) payload.only = unique;
  if (mode === "order" && unique.length) payload.order = unique;
  if (mode !== "auto") payload.allow_fallbacks = allowFallbacks;
  else if (!allowFallbacks) payload.allow_fallbacks = false;
  if (zdr) payload.zdr = true;
  return payload;
}

function moveItem(items: string[], index: number, delta: number): string[] {
  const nextIndex = index + delta;
  if (index < 0 || nextIndex < 0 || nextIndex >= items.length) return items;
  const next = [...items];
  [next[index], next[nextIndex]] = [next[nextIndex], next[index]];
  return next;
}

export function OpenRouterProviderRouting({ model }: { model: string }) {
  const [mode, setMode] = useState<OpenRouterProviderRoutingMode>("auto");
  const [selected, setSelected] = useState<string[]>([]);
  const [allowFallbacks, setAllowFallbacks] = useState(true);
  const [zdr, setZdr] = useState(false);
  const [candidates, setCandidates] = useState<OpenRouterProviderCandidate[]>([]);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (!model.trim()) return;
    let active = true;
    setLoading(true);
    void pyFetch<OpenRouterProviderRoutingResponse>(
      `/llm/openrouter/provider-routing?model=${encodeURIComponent(model)}`,
    )
      .then((data) => {
        if (!active) return;
        const state = providerRoutingState(data.provider);
        const apiCandidates = data.providers ?? [];
        const known = new Set(apiCandidates.map((item) => item.slug));
        const savedCandidates = state.selected
          .filter((slug) => !known.has(slug))
          .map((slug) => ({ slug, label: slug }));
        setCandidates([...savedCandidates, ...apiCandidates]);
        setMode(state.mode);
        setSelected(state.selected);
        setAllowFallbacks(state.allowFallbacks);
        setZdr(state.zdr);
      })
      .catch((error) => {
        if (!active) return;
        setCandidates([]);
        setMode("auto");
        setSelected([]);
        setAllowFallbacks(true);
        setZdr(false);
        toast.error(error instanceof Error ? error.message : "OpenRouter provider候補を取得できませんでした");
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [model]);

  const candidateBySlug = useMemo(
    () => new Map(candidates.map((candidate) => [candidate.slug, candidate])),
    [candidates],
  );

  const toggleProvider = (slug: string, checked: boolean) => {
    setSelected((current) => {
      if (checked) return current.includes(slug) ? current : [...current, slug];
      return current.filter((item) => item !== slug);
    });
  };

  const save = async () => {
    if ((mode === "only" || mode === "order") && selected.length === 0) {
      toast.error("providerを1つ以上選択してください");
      return;
    }
    setSaving(true);
    try {
      const provider = buildProviderRoutingPayload(mode, selected, allowFallbacks, zdr);
      await pyFetch("/llm/openrouter/provider-routing", {
        method: "PATCH",
        body: JSON.stringify({ model, provider }),
      });
      toast.success("OpenRouter上流provider設定を保存しました");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "OpenRouter provider設定を保存できませんでした");
    } finally {
      setSaving(false);
    }
  };

  if (!model.trim()) return null;

  return (
    <div className="space-y-3 rounded-md border p-3">
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="text-xs font-medium">OpenRouter上流プロバイダー</div>
          <p className="text-[10px] text-muted-foreground">
            {model} の設定。未設定ならOpenRouterの自動ルーティングを使用します。
          </p>
        </div>
        {loading && <Loader2 className="size-3 animate-spin text-muted-foreground" />}
      </div>

      <div className="max-w-sm space-y-1">
        <Label className="text-xs">ルーティング</Label>
        <AppSelect
          value={mode}
          onChange={(event) => setMode(event.target.value as OpenRouterProviderRoutingMode)}
          disabled={loading || saving}
          className="h-8 w-full rounded-lg border border-input bg-transparent px-2.5 text-sm outline-none dark:bg-input/30"
        >
          <option value="auto">自動選択</option>
          <option value="order">特定プロバイダーを優先</option>
          <option value="only">特定プロバイダーだけを使用</option>
        </AppSelect>
      </div>

      <div className="space-y-2">
        <div className="text-[10px] text-muted-foreground">
          OpenRouter endpoint APIから取得したslug（表示名ではなくslugを保存）
        </div>
        {candidates.length ? (
          <div className="grid gap-1 md:grid-cols-2">
            {candidates.map((candidate) => (
              <Label key={candidate.slug} className="flex items-center gap-2 rounded border px-2 py-1.5 text-xs">
                <Checkbox
                  checked={selected.includes(candidate.slug)}
                  onCheckedChange={(checked) => toggleProvider(candidate.slug, checked === true)}
                  disabled={mode === "auto" || loading || saving}
                  aria-label={`${candidate.label} (${candidate.slug})`}
                />
                <span className="min-w-0 truncate">{candidate.label}</span>
                <span className="ml-auto truncate text-[10px] text-muted-foreground">{candidate.slug}</span>
              </Label>
            ))}
          </div>
        ) : (
          <p className="rounded border border-dashed p-2 text-[10px] text-muted-foreground">
            provider候補を取得できませんでした。OpenRouter API keyとモデルslugを確認してください。
          </p>
        )}
      </div>

      {mode !== "auto" && selected.length > 0 && (
        <div className="space-y-1 rounded border bg-muted/20 p-2">
          <div className="text-[10px] font-medium">適用順</div>
          {selected.map((slug, index) => (
            <div key={slug} className="flex items-center gap-2 text-xs">
              <span className="w-5 text-right text-muted-foreground">{index + 1}.</span>
              <span className="min-w-0 flex-1 truncate">{candidateBySlug.get(slug)?.label ?? slug}</span>
              <Button
                type="button"
                size="icon"
                variant="ghost"
                className="size-6"
                onClick={() => setSelected((current) => moveItem(current, index, -1))}
                disabled={index === 0 || saving}
                aria-label={`${slug}を上へ移動`}
              >
                <MoveUp className="size-3" />
              </Button>
              <Button
                type="button"
                size="icon"
                variant="ghost"
                className="size-6"
                onClick={() => setSelected((current) => moveItem(current, index, 1))}
                disabled={index === selected.length - 1 || saving}
                aria-label={`${slug}を下へ移動`}
              >
                <MoveDown className="size-3" />
              </Button>
            </div>
          ))}
        </div>
      )}

      <div className="flex flex-wrap gap-4 text-xs">
        <Label className="flex items-center gap-2">
          <Checkbox
            checked={allowFallbacks}
            onCheckedChange={(checked) => setAllowFallbacks(checked === true)}
            disabled={loading || saving}
          />
          フォールバックを許可
        </Label>
        <Label className="flex items-center gap-2">
          <Checkbox
            checked={zdr}
            onCheckedChange={(checked) => setZdr(checked === true)}
            disabled={loading || saving}
          />
          Zero Data Retention必須
        </Label>
      </div>

      <Button type="button" size="sm" variant="outline" onClick={() => void save()} disabled={loading || saving}>
        {saving && <Loader2 className="mr-1 size-3 animate-spin" />}
        上流provider設定を保存
      </Button>
    </div>
  );
}
