"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  BookOpen,
  Link2,
  Loader2,
  Plus,
  Search,
  Unlink,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { AppSelect } from "@/components/ui/app-select";

type RelationType = "related" | "reference";

export type ProjectKnowledgeItem = {
  id?: string | null;
  node_id: string;
  title: string;
  relation_type: "related" | "reference" | "canonical";
  priority: number;
  project_id?: string | null;
  docs_library_id?: string | null;
};

type ProjectKnowledgeResponse = {
  canonical?: ProjectKnowledgeItem[];
  related?: ProjectKnowledgeItem[];
};

type DocsSearchResult = {
  id: string;
  title: string;
  project_id?: string | null;
  parent_title?: string | null;
};

type ProjectKnowledgePanelProps = {
  projectId: string;
  projectName?: string;
  /** Members can inspect the index; only manage_settings users can mutate it. */
  canManageSettings?: boolean;
};

function errorMessage(response: Response, fallback: string): Promise<string> {
  return response
    .json()
    .then((body: unknown) => {
      if (body && typeof body === "object" && "detail" in body) {
        const detail = (body as { detail?: unknown }).detail;
        if (typeof detail === "string" && detail.trim()) return detail;
      }
      return fallback;
    })
    .catch(() => fallback);
}

async function parseError(response: Response, fallback: string): Promise<never> {
  throw new Error(await errorMessage(response, fallback));
}

/**
 * Project Knowledge is deliberately an index, not a second Docs editor.
 * Canonical Project Information is shown as read-only; explicit reusable
 * references are attached/detached through the project Knowledge BFF.
 */
export function ProjectKnowledgePanel({
  projectId,
  projectName,
  canManageSettings = false,
}: ProjectKnowledgePanelProps) {
  const [knowledge, setKnowledge] = useState<ProjectKnowledgeResponse>({});
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [pendingNodeId, setPendingNodeId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [searchResults, setSearchResults] = useState<DocsSearchResult[]>([]);
  const [searching, setSearching] = useState(false);
  const [selectedNode, setSelectedNode] = useState<DocsSearchResult | null>(null);
  const [relationType, setRelationType] = useState<RelationType>("related");
  const [priority, setPriority] = useState("100");
  const [priorityDrafts, setPriorityDrafts] = useState<Record<string, string>>({});

  const canonical = useMemo(() => knowledge.canonical ?? [], [knowledge.canonical]);
  const related = useMemo(() => knowledge.related ?? [], [knowledge.related]);
  const attachedNodeIds = useMemo(
    () => new Set([...canonical, ...related].map((item) => item.node_id)),
    [canonical, related],
  );

  const loadKnowledge = useCallback(async (signal?: AbortSignal) => {
    setLoading(true);
    setError(null);
    try {
      const response = await fetch(
        `/api/projects/${encodeURIComponent(projectId)}/knowledge`,
        { credentials: "include", signal },
      );
      if (!response.ok) await parseError(response, "Project Knowledgeを取得できませんでした");
      const body = (await response.json()) as ProjectKnowledgeResponse;
      setKnowledge({ canonical: body.canonical ?? [], related: body.related ?? [] });
    } catch (cause) {
      if (cause instanceof DOMException && cause.name === "AbortError") return;
      setError(cause instanceof Error ? cause.message : "Project Knowledgeを取得できませんでした");
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    const controller = new AbortController();
    void loadKnowledge(controller.signal);
    return () => controller.abort();
  }, [loadKnowledge]);

  useEffect(() => {
    const nextDrafts: Record<string, string> = {};
    for (const item of related) nextDrafts[item.node_id] = String(item.priority);
    setPriorityDrafts(nextDrafts);
  }, [related]);

  useEffect(() => {
    if (!canManageSettings) {
      setSearchResults([]);
      return;
    }
    const normalizedQuery = query.trim();
    if (!normalizedQuery) {
      setSearchResults([]);
      setSearching(false);
      return;
    }

    const controller = new AbortController();
    const timer = window.setTimeout(async () => {
      setSearching(true);
      try {
        const params = new URLSearchParams({ q: normalizedQuery, limit: "8" });
        const response = await fetch(`/api/docs/search?${params.toString()}`, {
          credentials: "include",
          signal: controller.signal,
        });
        if (!response.ok) {
          setSearchResults([]);
          return;
        }
        const body = (await response.json()) as { results?: DocsSearchResult[] };
        if (!controller.signal.aborted) {
          setSearchResults(
            (Array.isArray(body.results) ? body.results : []).filter(
              (item) => item.id && item.title && !attachedNodeIds.has(item.id),
            ),
          );
        }
      } catch (cause) {
        if (!(cause instanceof DOMException && cause.name === "AbortError")) {
          setSearchResults([]);
        }
      } finally {
        if (!controller.signal.aborted) setSearching(false);
      }
    }, 250);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [attachedNodeIds, canManageSettings, query]);

  const attach = useCallback(async () => {
    if (!selectedNode || !canManageSettings || saving) return;
    const normalizedPriority = Number(priority);
    if (!Number.isInteger(normalizedPriority) || normalizedPriority < 0 || normalizedPriority > 1_000_000) {
      setError("優先度は0〜1,000,000の整数で指定してください");
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const response = await fetch(
        `/api/projects/${encodeURIComponent(projectId)}/knowledge`,
        {
          method: "POST",
          credentials: "include",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            node_id: selectedNode.id,
            relation_type: relationType,
            priority: normalizedPriority,
          }),
        },
      );
      if (!response.ok) await parseError(response, "KnowledgeNodeを追加できませんでした");
      setSelectedNode(null);
      setQuery("");
      setPriority("100");
      await loadKnowledge();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "KnowledgeNodeを追加できませんでした");
    } finally {
      setSaving(false);
    }
  }, [canManageSettings, loadKnowledge, priority, projectId, relationType, saving, selectedNode]);

  const detach = useCallback(async (nodeId: string) => {
    if (!canManageSettings || saving || pendingNodeId) return;
    setPendingNodeId(nodeId);
    setError(null);
    try {
      const response = await fetch(
        `/api/projects/${encodeURIComponent(projectId)}/knowledge/${encodeURIComponent(nodeId)}`,
        { method: "DELETE", credentials: "include" },
      );
      if (!response.ok) await parseError(response, "KnowledgeNodeの参照を解除できませんでした");
      await loadKnowledge();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "KnowledgeNodeの参照を解除できませんでした");
    } finally {
      setPendingNodeId(null);
    }
  }, [canManageSettings, loadKnowledge, pendingNodeId, projectId, saving]);

  /**
   * The API intentionally exposes attach/detach rather than a mutable PATCH
   * for this first relation surface. Updating a priority therefore performs
   * a bounded detach/attach pair and reloads the authoritative index.
   */
  const updatePriority = useCallback(async (item: ProjectKnowledgeItem) => {
    if (!canManageSettings || saving || pendingNodeId) return;
    const nextPriority = Number(priorityDrafts[item.node_id] ?? item.priority);
    if (!Number.isInteger(nextPriority) || nextPriority < 0 || nextPriority > 1_000_000) {
      setError("優先度は0〜1,000,000の整数で指定してください");
      setPriorityDrafts((previous) => ({ ...previous, [item.node_id]: String(item.priority) }));
      return;
    }
    if (nextPriority === item.priority) return;

    setPendingNodeId(item.node_id);
    setError(null);
    try {
      const deleteResponse = await fetch(
        `/api/projects/${encodeURIComponent(projectId)}/knowledge/${encodeURIComponent(item.node_id)}`,
        { method: "DELETE", credentials: "include" },
      );
      if (!deleteResponse.ok) await parseError(deleteResponse, "優先度を更新できませんでした");
      const attachResponse = await fetch(
        `/api/projects/${encodeURIComponent(projectId)}/knowledge`,
        {
          method: "POST",
          credentials: "include",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            node_id: item.node_id,
            relation_type: item.relation_type === "reference" ? "reference" : "related",
            priority: nextPriority,
          }),
        },
      );
      if (!attachResponse.ok) await parseError(attachResponse, "優先度を更新できませんでした");
      await loadKnowledge();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "優先度を更新できませんでした");
      await loadKnowledge();
    } finally {
      setPendingNodeId(null);
    }
  }, [canManageSettings, loadKnowledge, pendingNodeId, priorityDrafts, projectId, saving]);

  return (
    <Card className="border-border bg-card shadow-none" data-testid="project-knowledge-panel">
      <CardHeader className="border-b border-border">
        <CardTitle className="flex items-center gap-2 text-base font-semibold">
          <Link2 className="size-4" />
          Project Knowledge
          {projectName ? <span className="truncate text-sm font-normal text-muted-foreground">{projectName}</span> : null}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-6 pt-5">
        {error ? (
          <div role="alert" className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm">
            {error}
          </div>
        ) : null}

        <section aria-labelledby="project-knowledge-canonical-heading" className="space-y-2">
          <div className="flex items-center justify-between gap-2">
            <h3 id="project-knowledge-canonical-heading" className="text-sm font-semibold">Canonical Project Information</h3>
            <Badge variant="outline">read-only</Badge>
          </div>
          {loading ? (
            <p className="flex items-center gap-2 text-sm text-muted-foreground"><Loader2 className="size-4 animate-spin" />読み込み中…</p>
          ) : canonical.length === 0 ? (
            <p className="text-sm text-muted-foreground">参照可能な正本Docsはありません。</p>
          ) : (
            <ul className="space-y-2">
              {canonical.map((item) => (
                <li key={item.node_id} className="flex items-center justify-between gap-3 rounded-md border border-border px-3 py-2 text-sm">
                  <Link href={`/docs/${encodeURIComponent(item.node_id)}`} className="flex min-w-0 items-center gap-2 truncate hover:text-primary">
                    <BookOpen className="size-4 shrink-0 text-muted-foreground" />
                    <span className="truncate">{item.title}</span>
                  </Link>
                  <span className="shrink-0 text-xs text-muted-foreground">canonical</span>
                </li>
              ))}
            </ul>
          )}
        </section>

        <section aria-labelledby="project-knowledge-related-heading" className="space-y-3">
          <div className="flex items-center justify-between gap-2">
            <h3 id="project-knowledge-related-heading" className="text-sm font-semibold">Related Knowledge</h3>
            {!canManageSettings ? <Badge variant="outline">read-only</Badge> : null}
          </div>

          {canManageSettings ? (
            <div className="space-y-3 rounded-md border border-dashed border-border p-3" data-testid="project-knowledge-attach-form">
              <label className="space-y-1 text-sm font-medium" htmlFor="project-knowledge-node-search">
                <span>Docsを検索して参照を追加</span>
                <div className="relative">
                  <Search className="pointer-events-none absolute left-2.5 top-2.5 size-4 text-muted-foreground" />
                  <Input
                    id="project-knowledge-node-search"
                    aria-label="Search Docs nodes"
                    value={query}
                    onChange={(event) => setQuery(event.target.value)}
                    placeholder="タイトルまたは本文を検索…"
                    className="pl-8"
                  />
                </div>
              </label>
              {searching ? <p className="text-xs text-muted-foreground">検索中…</p> : null}
              {searchResults.length > 0 ? (
                <ul className="max-h-48 space-y-1 overflow-auto" aria-label="Docs search results">
                  {searchResults.map((result) => (
                    <li key={result.id}>
                      <button
                        type="button"
                        className={`w-full rounded px-2 py-1.5 text-left text-sm hover:bg-accent ${selectedNode?.id === result.id ? "bg-accent" : ""}`}
                        onClick={() => setSelectedNode(result)}
                      >
                        <span className="block truncate">{result.title}</span>
                        {result.parent_title ? <span className="block truncate text-xs text-muted-foreground">{result.parent_title}</span> : null}
                      </button>
                    </li>
                  ))}
                </ul>
              ) : null}
              {selectedNode ? (
                <div className="grid gap-3 sm:grid-cols-[minmax(0,1fr)_8rem_7rem_auto] sm:items-end">
                  <div className="min-w-0 rounded border border-border px-2.5 py-2 text-sm">
                    <p className="truncate font-medium">{selectedNode.title}</p>
                    <p className="truncate text-xs text-muted-foreground">{selectedNode.id}</p>
                  </div>
                  <label className="space-y-1 text-xs text-muted-foreground" htmlFor="project-knowledge-relation">
                    Relation
                    <AppSelect
                      id="project-knowledge-relation"
                      aria-label="Relation type"
                      value={relationType}
                      onValueChange={(value) => setRelationType(value as RelationType)}
                      className="h-9 w-full rounded-md border border-input bg-background px-2 text-sm text-foreground"
                    >
                      <option value="related">related</option>
                      <option value="reference">reference</option>
                    </AppSelect>
                  </label>
                  <label className="space-y-1 text-xs text-muted-foreground" htmlFor="project-knowledge-priority">
                    Priority
                    <Input
                      id="project-knowledge-priority"
                      aria-label="Priority"
                      type="number"
                      min={0}
                      max={1_000_000}
                      step={1}
                      value={priority}
                      onChange={(event) => setPriority(event.target.value)}
                    />
                  </label>
                  <Button type="button" onClick={() => void attach()} disabled={saving}>
                    {saving ? <Loader2 className="size-4 animate-spin" /> : <Plus className="size-4" />}
                    追加
                  </Button>
                </div>
              ) : null}
            </div>
          ) : (
            <p className="text-sm text-muted-foreground">このプロジェクトのメンバーはKnowledgeを閲覧できます。編集にはmanage_settings権限が必要です。</p>
          )}

          {related.length === 0 ? (
            <p className="text-sm text-muted-foreground">関連付けられたKnowledgeNodeはありません。</p>
          ) : (
            <ul className="space-y-2">
              {related.map((item) => {
                const busy = pendingNodeId === item.node_id;
                return (
                  <li key={item.node_id} className="grid gap-2 rounded-md border border-border px-3 py-2 sm:grid-cols-[minmax(0,1fr)_7rem_auto] sm:items-center">
                    <Link href={`/docs/${encodeURIComponent(item.node_id)}`} className="flex min-w-0 items-center gap-2 truncate text-sm hover:text-primary">
                      <BookOpen className="size-4 shrink-0 text-muted-foreground" />
                      <span className="truncate">{item.title}</span>
                      <Badge variant="secondary" className="shrink-0 text-[10px]">{item.relation_type}</Badge>
                    </Link>
                    <label className="flex items-center gap-2 text-xs text-muted-foreground" htmlFor={`project-knowledge-priority-${item.node_id}`}>
                      Priority
                      <Input
                        id={`project-knowledge-priority-${item.node_id}`}
                        aria-label={`Priority for ${item.title}`}
                        type="number"
                        min={0}
                        max={1_000_000}
                        step={1}
                        value={priorityDrafts[item.node_id] ?? String(item.priority)}
                        disabled={!canManageSettings || busy}
                        onChange={(event) => setPriorityDrafts((previous) => ({ ...previous, [item.node_id]: event.target.value }))}
                        onBlur={() => void updatePriority(item)}
                        onKeyDown={(event) => {
                          if (event.key === "Enter") {
                            event.preventDefault();
                            void updatePriority(item);
                          }
                        }}
                        className="h-8 w-20"
                      />
                    </label>
                    {canManageSettings ? (
                      <Button type="button" variant="ghost" size="sm" disabled={busy || saving} onClick={() => void detach(item.node_id)}>
                        {busy ? <Loader2 className="size-4 animate-spin" /> : <Unlink className="size-4" />}
                        解除
                      </Button>
                    ) : null}
                  </li>
                );
              })}
            </ul>
          )}
        </section>
      </CardContent>
    </Card>
  );
}
