"use client";

import { useCallback, useRef, useState } from "react";
import useSWR from "swr";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useProject } from "@/contexts/project-context";
import {
  ChevronDown,
  ChevronUp,
  ExternalLink,
  FileSearch,
  FolderPlus,
  Globe,
  Loader2,
  Plug,
  RefreshCw,
  Search,
  Sparkles,
  Trash2,
} from "lucide-react";
import { toast } from "sonner";

type SourceStatus = "created" | "syncing" | "synced" | "error" | string;

interface KnowledgeSource {
  id: string;
  name: string;
  description: string | null;
  root_path: string;
  source_type: string;
  sync_mode: string;
  write_policy: string;
  status: SourceStatus;
  document_count: number;
  chunk_count: number;
  last_synced_at: string | null;
  error_message: string | null;
}

interface SettingsPayload {
  settings?: {
    knowledge?: { enabled?: boolean };
    search?: { knowledge_enabled?: boolean };
  };
}

interface SearchResult {
  score: number;
  url: string | null;
  source: KnowledgeSource;
  document: { id: string; path: string; title: string | null };
  chunk: { text: string; heading_path: string[] };
}

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

const STATUS_LABELS: Record<string, { label: string; variant: "default" | "secondary" | "destructive" | "outline" }> = {
  created: { label: "未同期", variant: "outline" },
  syncing: { label: "同期中", variant: "secondary" },
  synced: { label: "同期済み", variant: "default" },
  error: { label: "要確認", variant: "destructive" },
};

export function KnowledgeSourcesSection() {
  const { selectedProject, selectedProjectId } = useProject();
  const [expanded, setExpanded] = useState(false);
  // ナレッジソース一覧と検索有効フラグ（サーバー状態）は SWR で管理。取得タイミングは
  // 従来どおり呼び出し側（展開/各操作後）で駆動するため自動 revalidation は無効化する。
  const { data: sources = [], mutate: mutateSources } = useSWR<KnowledgeSource[]>(
    "settings/knowledge-sources",
    async () => {
      try {
        return (await pyFetch<{ sources: KnowledgeSource[] }>("/knowledge/sources"))
          .sources || [];
      } catch (error) {
        toast.error(error instanceof Error ? error.message : "ナレッジソースを取得できませんでした");
        return [];
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
  const knowledgeEnabledRef = useRef(false);
  const { data: knowledgeEnabled = false, mutate: mutateKnowledgeEnabled } = useSWR<boolean>(
    "settings/knowledge-enabled",
    async () => {
      try {
        const data = await pyFetch<SettingsPayload>("/settings");
        return (
          data.settings?.search?.knowledge_enabled ??
          data.settings?.knowledge?.enabled ??
          false
        );
      } catch {
        return knowledgeEnabledRef.current;
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
  knowledgeEnabledRef.current = knowledgeEnabled;
  const [loading, setLoading] = useState(false);
  const [savingEnabled, setSavingEnabled] = useState(false);
  const [creating, setCreating] = useState(false);
  const [busySourceId, setBusySourceId] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<SearchResult[]>([]);
  const [searching, setSearching] = useState(false);
  const [showGrowiForm, setShowGrowiForm] = useState(false);
  const [growiName, setGrowiName] = useState("");
  const [growiBaseUrl, setGrowiBaseUrl] = useState("");
  const [growiToken, setGrowiToken] = useState("");
  const [creatingGrowi, setCreatingGrowi] = useState(false);
  const [testingGrowi, setTestingGrowi] = useState(false);

  const fetchSources = useCallback(async () => {
    setLoading(true);
    try {
      await mutateSources();
    } finally {
      setLoading(false);
    }
  }, [mutateSources]);

  const handleExpand = useCallback(() => {
    const next = !expanded;
    setExpanded(next);
    if (next) {
      void mutateKnowledgeEnabled();
      void fetchSources();
    }
  }, [expanded, mutateKnowledgeEnabled, fetchSources]);

  const handleEnabledChange = useCallback(
    async (enabled: boolean) => {
      // 楽観的更新：切替後はローカルキャッシュを即時反映する。
      await mutateKnowledgeEnabled(enabled, { revalidate: false });
      setSavingEnabled(true);
      try {
        await pyFetch("/settings", {
          method: "PATCH",
          body: JSON.stringify({
            key: "search.knowledge_enabled",
            value: enabled,
          }),
        });
        toast.success("Knowledge検索設定を保存しました");
      } catch (error) {
        toast.error(error instanceof Error ? error.message : "設定を保存できませんでした");
        void mutateKnowledgeEnabled();
      } finally {
        setSavingEnabled(false);
      }
    },
    [mutateKnowledgeEnabled],
  );

  const handleCreate = useCallback(async () => {
    if (!selectedProjectId || !selectedProject) return;
    setCreating(true);
    try {
      await pyFetch("/knowledge/sources", {
        method: "POST",
        body: JSON.stringify({
          name: `${selectedProject.name} Workspace`,
          project_id: selectedProjectId,
          source_type: "project_workspace",
          write_policy: "propose_patch",
          auto_sync: true,
        }),
      });
      await fetchSources();
      toast.success("プロジェクトWorkspaceをKnowledge Sourceに同期しました");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "同期できませんでした");
    } finally {
      setCreating(false);
    }
  }, [fetchSources, selectedProject, selectedProjectId]);

  const handleTestGrowi = useCallback(async () => {
    if (!growiBaseUrl.trim() || !growiToken.trim()) return;
    setTestingGrowi(true);
    try {
      const result = await pyFetch<{ ok: boolean; detail?: { sample_count?: number } }>(
        "/knowledge/sources/growi/test",
        {
          method: "POST",
          body: JSON.stringify({
            base_url: growiBaseUrl.trim(),
            api_token: growiToken.trim(),
          }),
        },
      );
      toast.success(
        `GROWIに接続できました（ページ取得を確認: ${result.detail?.sample_count ?? 0}件）`,
      );
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "GROWIに接続できませんでした");
    } finally {
      setTestingGrowi(false);
    }
  }, [growiBaseUrl, growiToken]);

  const handleCreateGrowi = useCallback(async () => {
    if (!growiBaseUrl.trim() || !growiToken.trim()) return;
    setCreatingGrowi(true);
    try {
      await pyFetch("/knowledge/sources", {
        method: "POST",
        body: JSON.stringify({
          name: growiName.trim() || "社内Wiki (GROWI)",
          source_type: "growi",
          base_url: growiBaseUrl.trim(),
          api_token: growiToken.trim(),
          auto_sync: true,
        }),
      });
      setGrowiName("");
      setGrowiBaseUrl("");
      setGrowiToken("");
      setShowGrowiForm(false);
      await fetchSources();
      toast.success("GROWIをナレッジソースに登録し、同期を開始しました");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "GROWIを登録できませんでした");
    } finally {
      setCreatingGrowi(false);
    }
  }, [fetchSources, growiBaseUrl, growiName, growiToken]);

  const runSourceAction = useCallback(
    async (sourceId: string, action: "sync" | "organize" | "delete") => {
      setBusySourceId(sourceId);
      try {
        if (action === "delete") {
          await pyFetch(`/knowledge/sources/${sourceId}`, { method: "DELETE" });
          toast.success("Knowledge Sourceを削除しました");
        } else if (action === "sync") {
          await pyFetch(`/knowledge/sources/${sourceId}/sync`, { method: "POST" });
          toast.success("同期しました");
        } else {
          const result = await pyFetch<{ suggestion_count: number }>(
            `/knowledge/sources/${sourceId}/organize`,
            {
              method: "POST",
              body: JSON.stringify({ dry_run: false }),
            },
          );
          toast.success(`${result.suggestion_count}件の整備候補を作成しました`);
        }
        await fetchSources();
      } catch (error) {
        toast.error(error instanceof Error ? error.message : "処理に失敗しました");
      } finally {
        setBusySourceId(null);
      }
    },
    [fetchSources],
  );

  const handleSearch = useCallback(async () => {
    if (!query.trim()) return;
    setSearching(true);
    try {
      const data = await pyFetch<{ results: SearchResult[] }>("/knowledge/search", {
        method: "POST",
        body: JSON.stringify({
          query: query.trim(),
          project_id: selectedProjectId,
          top_k: 5,
        }),
      });
      setResults(data.results || []);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "検索できませんでした");
    } finally {
      setSearching(false);
    }
  }, [query, selectedProjectId]);

  return (
    <Card size="sm">
      <CardHeader className="cursor-pointer select-none" onClick={handleExpand}>
        <CardTitle className="flex items-center justify-between gap-3 text-sm">
          <span className="flex min-w-0 items-center gap-2">
            <FileSearch className="size-4" />
            ナレッジソース
            {sources.length > 0 && (
              <Badge variant="secondary">{sources.length}</Badge>
            )}
          </span>
          {expanded ? <ChevronUp className="size-4" /> : <ChevronDown className="size-4" />}
        </CardTitle>
      </CardHeader>

      {expanded && (
        <CardContent className="space-y-4">
          <div className="flex flex-wrap items-center justify-between gap-3 rounded-md border p-3">
            <Label className="flex items-center gap-2 text-sm">
              <Checkbox
                checked={knowledgeEnabled}
                disabled={savingEnabled}
                onCheckedChange={(checked) => handleEnabledChange(checked === true)}
              />
              AgentのKnowledge検索を有効にする
            </Label>
            <div className="flex flex-wrap gap-2">
              <Button
                size="sm"
                variant="outline"
                onClick={handleCreate}
                disabled={creating || !selectedProjectId}
              >
                {creating ? <Loader2 className="mr-2 size-4 animate-spin" /> : <FolderPlus className="mr-2 size-4" />}
                Workspace同期
              </Button>
              <Button
                size="sm"
                variant="outline"
                onClick={() => setShowGrowiForm((prev) => !prev)}
              >
                <Globe className="mr-2 size-4" />
                GROWI追加
              </Button>
            </div>
          </div>

          {showGrowiForm && (
            <div className="space-y-3 rounded-md border p-3">
              <p className="text-sm font-medium">社内Wiki (GROWI) を接続</p>
              <div className="space-y-2">
                <Label className="text-xs text-muted-foreground">表示名</Label>
                <Input
                  value={growiName}
                  onChange={(event) => setGrowiName(event.target.value)}
                  placeholder="社内Wiki (GROWI)"
                />
                <Label className="text-xs text-muted-foreground">
                  GROWIのURL（社内ネットワークから到達できるベースURL）
                </Label>
                <Input
                  value={growiBaseUrl}
                  onChange={(event) => setGrowiBaseUrl(event.target.value)}
                  placeholder="https://wiki.example.co.jp"
                />
                <Label className="text-xs text-muted-foreground">
                  APIトークン（GROWIの個人設定 &gt; API Token で発行）
                </Label>
                <Input
                  type="password"
                  value={growiToken}
                  onChange={(event) => setGrowiToken(event.target.value)}
                  placeholder="GROWI API Token"
                  autoComplete="off"
                />
              </div>
              <div className="flex flex-wrap gap-2">
                <Button
                  size="sm"
                  variant="outline"
                  onClick={handleTestGrowi}
                  disabled={testingGrowi || !growiBaseUrl.trim() || !growiToken.trim()}
                >
                  {testingGrowi ? <Loader2 className="mr-2 size-4 animate-spin" /> : <Plug className="mr-2 size-4" />}
                  接続テスト
                </Button>
                <Button
                  size="sm"
                  onClick={handleCreateGrowi}
                  disabled={creatingGrowi || !growiBaseUrl.trim() || !growiToken.trim()}
                >
                  {creatingGrowi ? <Loader2 className="mr-2 size-4 animate-spin" /> : <Globe className="mr-2 size-4" />}
                  追加して同期
                </Button>
              </div>
            </div>
          )}

          <div className="flex gap-2">
            <Input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") void handleSearch();
              }}
              placeholder="文書、見出し、固有名詞を検索"
            />
            <Button onClick={handleSearch} disabled={searching || !query.trim()}>
              {searching ? <Loader2 className="mr-2 size-4 animate-spin" /> : <Search className="mr-2 size-4" />}
              検索
            </Button>
          </div>

          {results.length > 0 && (
            <div className="space-y-2 rounded-md border p-3">
              {results.map((result) => (
                <div key={`${result.document.id}-${result.chunk.text.slice(0, 20)}`} className="space-y-1 border-b pb-2 last:border-b-0 last:pb-0">
                  <div className="flex flex-wrap items-center gap-2 text-sm font-medium">
                    {result.url ? (
                      <a
                        href={result.url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="inline-flex items-center gap-1 text-primary hover:underline"
                      >
                        {result.document.title || result.document.path}
                        <ExternalLink className="size-3" />
                      </a>
                    ) : (
                      <span>{result.document.title || result.document.path}</span>
                    )}
                    <Badge variant="outline">{result.source.name}</Badge>
                  </div>
                  <p className="line-clamp-2 text-xs text-muted-foreground">
                    {result.chunk.text}
                  </p>
                </div>
              ))}
            </div>
          )}

          {loading ? (
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              <Loader2 className="size-4 animate-spin" />
              読み込み中
            </div>
          ) : sources.length === 0 ? (
            <div className="rounded-md border border-dashed p-4 text-sm text-muted-foreground">
              ナレッジソースは未登録です。
            </div>
          ) : (
            <div className="space-y-2">
              {sources.map((source) => {
                const status = STATUS_LABELS[source.status] || {
                  label: source.status,
                  variant: "outline" as const,
                };
                const busy = busySourceId === source.id;
                return (
                  <div key={source.id} className="space-y-2 rounded-md border p-3">
                    <div className="flex flex-wrap items-start justify-between gap-3">
                      <div className="min-w-0 space-y-1">
                        <div className="flex flex-wrap items-center gap-2">
                          <span className="font-medium">{source.name}</span>
                          <Badge variant={status.variant}>{status.label}</Badge>
                          {source.source_type === "project_workspace" && (
                            <Badge variant="secondary">Workspace</Badge>
                          )}
                          {source.source_type === "growi" && (
                            <Badge variant="secondary">GROWI</Badge>
                          )}
                          <Badge variant="outline">{source.write_policy}</Badge>
                        </div>
                        <p className="break-all text-xs text-muted-foreground">
                          {source.root_path}
                        </p>
                      </div>
                      <div className="flex flex-wrap gap-2">
                        <Button
                          size="sm"
                          variant="outline"
                          disabled={busy}
                          onClick={() => runSourceAction(source.id, "sync")}
                        >
                          {busy ? <Loader2 className="mr-2 size-4 animate-spin" /> : <RefreshCw className="mr-2 size-4" />}
                          Sync
                        </Button>
                        <Button
                          size="sm"
                          variant="outline"
                          disabled={busy}
                          onClick={() => runSourceAction(source.id, "organize")}
                        >
                          <Sparkles className="mr-2 size-4" />
                          整備候補
                        </Button>
                        <Button
                          size="sm"
                          variant="ghost"
                          disabled={busy}
                          onClick={() => runSourceAction(source.id, "delete")}
                        >
                          <Trash2 className="mr-2 size-4" />
                          削除
                        </Button>
                      </div>
                    </div>
                    <div className="flex flex-wrap gap-3 text-xs text-muted-foreground">
                      <span>{source.document_count} docs</span>
                      <span>{source.chunk_count} chunks</span>
                      {source.last_synced_at && (
                        <span>last sync: {new Date(source.last_synced_at).toLocaleString()}</span>
                      )}
                    </div>
                    {source.error_message && (
                      <p className="rounded bg-destructive/10 p-2 text-xs text-destructive">
                        {source.error_message}
                      </p>
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </CardContent>
      )}

    </Card>
  );
}
