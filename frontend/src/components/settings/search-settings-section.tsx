"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import useSWR from "swr";
import { ChevronDown, ChevronUp, Search } from "lucide-react";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
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
    search?: { provider?: string };
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
}

const INITIAL_SEARCH_STATE: SearchSettingsState = {
  provider: null,
  providerValues: ["openai", "local"],
};

export function SearchSettingsSection() {
  const [expanded, setExpanded] = useState(false);
  // 検索設定（サーバー状態）は SWR で管理。取得タイミングは従来どおりマウント時に
  // 駆動するため自動 revalidation は無効化する。取得失敗時は従来同様に直前値を保持する。
  const stateRef = useRef<SearchSettingsState>(INITIAL_SEARCH_STATE);
  const { data = INITIAL_SEARCH_STATE, mutate: mutateSearch } = useSWR<SearchSettingsState>(
    "settings/search-provider",
    async () => {
      try {
        const payload = await pyFetch<SettingsPayload>("/settings");
        return {
          provider: payload.settings?.search?.provider ?? "openai",
          providerValues:
            payload.schema?.["search.provider"]?.values ?? ["openai", "local"],
        };
      } catch (error) {
        toast.error(error instanceof Error ? error.message : "検索設定を取得できませんでした");
        return stateRef.current;
      }
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
  stateRef.current = data;
  const { provider, providerValues } = data;
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);

  const loadSettings = useCallback(async () => {
    setLoading(true);
    try {
      await mutateSearch();
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

  return (
    <Card size="sm">
      <CardHeader
        className="cursor-pointer select-none"
        onClick={() => setExpanded((value) => !value)}
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
        <CardContent className="space-y-3">
          {loading ? (
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
            </>
          )}
        </CardContent>
      )}
    </Card>
  );
}
