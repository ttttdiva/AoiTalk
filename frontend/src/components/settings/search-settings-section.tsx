"use client";

import { useCallback, useEffect, useState } from "react";
import useSWR from "swr";
import { ChevronDown, ChevronUp, Search } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";

interface SettingsPayload {
  settings?: {
    search?: { provider?: string; openai_model?: string };
  };
  schema?: Record<string, { type: string; values?: string[] }>;
}

const SEARCH_PROVIDER_LABELS: Record<string, string> = {
  openai: "OpenAI検索",
  local: "汎用Web検索",
};

const SEARCH_PROVIDER_DESCRIPTIONS: Record<string, string> = {
  openai:
    "OpenAI APIのHosted Searchを使います。確認モードでは実行前に許可確認を表示します。",
  local:
    "AoiTalk側の汎用Web検索を使います。SearXNG/Wikipediaなどを使い、OpenAI APIの検索ではありません。",
};

async function pyFetch<T = unknown>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api/python-proxy${path}`, {
    credentials: "include",
    headers: { "Content-Type": "application/json", ...init?.headers },
    ...init,
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(detail.detail || `API Error: ${res.status}`);
  }
  return res.json();
}

interface SearchSettingsState {
  provider: string | null;
  providerValues: string[];
  openaiModel: string | null;
}

const INITIAL_SEARCH_STATE: SearchSettingsState = {
  provider: null,
  providerValues: ["openai", "local"],
  openaiModel: null,
};

export function SearchSettingsSection() {
  const [expanded, setExpanded] = useState(false);
  // 検索設定（サーバー状態）は SWR で管理。取得タイミングは従来どおりマウント時に
  // 駆動するため自動 revalidation は無効化する。失敗時は stale 値と再試行を表示する。
  const { data = INITIAL_SEARCH_STATE, error: searchError, mutate: mutateSearch } = useSWR<SearchSettingsState>(
    "settings/search-provider",
    async () => {
      const payload = await pyFetch<SettingsPayload>("/settings");
      return {
        provider: payload.settings?.search?.provider ?? "openai",
        providerValues:
          payload.schema?.["search.provider"]?.values ?? ["openai", "local"],
        openaiModel: payload.settings?.search?.openai_model ?? null,
      };
    },
    {
      revalidateOnMount: false,
      revalidateOnFocus: false,
      revalidateOnReconnect: false,
      revalidateIfStale: false,
      keepPreviousData: true,
      dedupingInterval: 0,
    },
  );
  const { provider, providerValues, openaiModel } = data;
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [openaiModelInput, setOpenaiModelInput] = useState("");

  useEffect(() => {
    setOpenaiModelInput(openaiModel ?? "");
  }, [openaiModel, provider]);

  const loadSettings = useCallback(async () => {
    setLoading(true);
    try {
      await mutateSearch();
    } catch {
      // SWR exposes the error for the inline retry state below.
    } finally {
      setLoading(false);
    }
  }, [mutateSearch]);

  useEffect(() => {
    void loadSettings();
  }, [loadSettings]);

  const updateProvider = useCallback(async (value: string) => {
    // 楽観的更新：保存中はローカルキャッシュを即時反映する。
    await mutateSearch(
      (current = INITIAL_SEARCH_STATE) => ({ ...current, provider: value }),
      { revalidate: false },
    );
    setSaving(true);
    try {
      await pyFetch("/settings", {
        method: "PATCH",
        body: JSON.stringify({ key: "search.provider", value }),
      });
      toast.success("検索設定を保存しました");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "検索設定を保存できませんでした");
      void mutateSearch();
    } finally {
      setSaving(false);
    }
  }, [mutateSearch]);

  const saveOpenaiModel = useCallback(async () => {
    if (provider !== "openai") return;
    const value = openaiModelInput.trim();
    if (!value) return;
    const previousModel = data.openaiModel;

    await mutateSearch(
      (current = INITIAL_SEARCH_STATE) => ({ ...current, openaiModel: value }),
      { revalidate: false },
    );
    setSaving(true);
    try {
      await pyFetch("/settings", {
        method: "PATCH",
        body: JSON.stringify({ key: "search.openai_model", value }),
      });
      toast.success("検索設定を保存しました");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "検索設定を保存できませんでした");
      await mutateSearch(
        (current = INITIAL_SEARCH_STATE) => ({ ...current, openaiModel: previousModel }),
        { revalidate: false },
      );
      void mutateSearch();
    } finally {
      setSaving(false);
    }
  }, [data.openaiModel, mutateSearch, openaiModelInput, provider]);

  return (
    <Card size="sm" className="rounded-md border-border dark:border-[#333335] bg-card dark:bg-[#1a1a1b] py-0" data-settings-surface="search-settings">
      <CardHeader
        className="cursor-pointer select-none border-b border-border dark:border-[#333335] px-3 py-3 transition-colors hover:bg-muted dark:bg-[#242426]"
        role="button"
        tabIndex={0}
        aria-expanded={expanded}
        aria-controls="search-provider-content"
        onClick={() => setExpanded((value) => !value)}
        onKeyDown={(event) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            setExpanded((value) => !value);
          }
        }}
      >
        <CardTitle className="flex items-center justify-between gap-3 text-sm">
          <span className="flex min-w-0 items-center gap-2">
            <Search className="size-4" />
            <span>検索プロバイダ</span>
            <Badge variant="secondary">
              {provider ? (SEARCH_PROVIDER_LABELS[provider] ?? provider) : "読み込み中"}
            </Badge>
          </span>
          {expanded ? <ChevronUp className="size-4" /> : <ChevronDown className="size-4" />}
        </CardTitle>
      </CardHeader>
      {expanded && (
      <CardContent id="search-provider-content" className="space-y-3 px-3 py-3">
          {searchError ? (
            <div role="alert" className="space-y-2 text-sm text-destructive">
              <p>検索設定を取得できませんでした。{data.provider ? "前回の値を表示しています。" : ""}</p>
              <Button type="button" variant="outline" size="sm" onClick={() => void mutateSearch()}>
                再試行
              </Button>
            </div>
          ) : loading ? (
            <Skeleton className="h-8 w-64 rounded" />
          ) : (
            <>
              <div className="space-y-2">
                <Label className="text-xs">検索プロバイダ</Label>
                <Select
                  value={provider ?? ""}
                  onValueChange={(value) => {
                    if (typeof value === "string") void updateProvider(value);
                  }}
                  disabled={saving || !provider}
                >
                  <SelectTrigger className="w-full max-w-xs">
                    <span>{provider ? (SEARCH_PROVIDER_LABELS[provider] ?? provider) : "読み込み中"}</span>
                  </SelectTrigger>
                  <SelectContent>
                    {providerValues.map((value) => (
                      <SelectItem key={value} value={value}>
                        {SEARCH_PROVIDER_LABELS[value] ?? value}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              <p className="max-w-2xl text-xs text-muted-foreground">
                {provider ? (SEARCH_PROVIDER_DESCRIPTIONS[provider] ?? "") : ""}
              </p>
              {provider === "openai" && (
                <form
                  className="space-y-2"
                  onSubmit={(event) => {
                    event.preventDefault();
                    void saveOpenaiModel();
                  }}
                >
                  <Label htmlFor="search-openai-model" className="text-xs">
                    OpenAI検索モデル
                  </Label>
                  <div className="flex max-w-xl items-center gap-2">
                    <Input
                      id="search-openai-model"
                      value={openaiModelInput}
                      onChange={(event) => setOpenaiModelInput(event.target.value)}
                      disabled={saving}
                      className="h-8"
                    />
                    <Button
                      type="submit"
                      size="sm"
                      variant="outline"
                      disabled={saving || !openaiModelInput.trim()}
                    >
                      モデルを保存
                    </Button>
                  </div>
                </form>
              )}
            </>
          )}
        </CardContent>
      )}
    </Card>
  );
}
