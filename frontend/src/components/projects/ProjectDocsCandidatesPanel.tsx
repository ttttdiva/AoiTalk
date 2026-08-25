"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Check, FileText, Loader2, RefreshCw, X } from "lucide-react";
import type { components } from "@/lib/api-types.gen";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

type DocsCandidate = components["schemas"]["DocsCandidateDTO"];
type DocsCandidateListResponse =
  components["schemas"]["DocsCandidateListResponse"];

type ProjectDocsCandidatesPanelProps = {
  projectId: string;
  canManageSettings: boolean;
};

type CandidateContent = NonNullable<DocsCandidate["content"]>;

function contentText(content: CandidateContent | undefined, key: string) {
  const value = content?.[key];
  return typeof value === "string" ? value.trim() : "";
}

function candidateContent(candidate: DocsCandidate) {
  const content = candidate.content;
  const title = contentText(content, "title");
  const body = contentText(content, "content");
  const section = contentText(content, "section_hint");
  return { title, body, section };
}

function errorDetail(body: unknown, fallback: string) {
  if (body && typeof body === "object" && "detail" in body) {
    const detail = (body as { detail?: unknown }).detail;
    if (typeof detail === "string" && detail.trim()) return detail;
  }
  return fallback;
}

async function readError(response: Response, fallback: string) {
  const body = await response.json().catch(() => null);
  return errorDetail(body, fallback);
}

/**
 * Shows the bounded, Project-scoped Docs review queue.  The browser only
 * talks to the Next BFF; raw evidence and transcript-like fields are never
 * rendered here even if an unexpected provider payload reaches `content`.
 */
export function ProjectDocsCandidatesPanel({
  projectId,
  canManageSettings,
}: ProjectDocsCandidatesPanelProps) {
  const [items, setItems] = useState<DocsCandidate[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [pendingId, setPendingId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const requestGenerationRef = useRef(0);
  const requestControllerRef = useRef<AbortController | null>(null);

  const load = useCallback(async () => {
    const generation = ++requestGenerationRef.current;
    requestControllerRef.current?.abort();
    const controller = new AbortController();
    requestControllerRef.current = controller;
    setLoading(true);
    setError(null);

    try {
      const params = new URLSearchParams({
        status: "proposed",
        limit: "100",
        offset: "0",
      });
      const response = await fetch(
        `/api/projects/${encodeURIComponent(projectId)}/docs-candidates?${params.toString()}`,
        { credentials: "include", signal: controller.signal },
      );
      if (!response.ok) {
        throw new Error(await readError(response, "Docs候補を取得できませんでした"));
      }
      const body = (await response.json()) as DocsCandidateListResponse;
      if (controller.signal.aborted || generation !== requestGenerationRef.current) {
        return;
      }
      setItems(Array.isArray(body.items) ? body.items : []);
      setTotal(typeof body.total === "number" ? body.total : body.items?.length ?? 0);
    } catch (cause) {
      if (
        controller.signal.aborted ||
        generation !== requestGenerationRef.current
      ) {
        return;
      }
      setError(cause instanceof Error ? cause.message : "Docs候補を取得できませんでした");
    } finally {
      if (!controller.signal.aborted && generation === requestGenerationRef.current) {
        setLoading(false);
      }
    }
  }, [projectId]);

  useEffect(() => {
    void load();
    return () => {
      requestGenerationRef.current += 1;
      requestControllerRef.current?.abort();
    };
  }, [load]);

  const mutate = useCallback(
    async (candidate: DocsCandidate, action: "approve" | "reject") => {
      if (!canManageSettings || pendingId) return;
      setPendingId(candidate.id);
      setError(null);
      try {
        const response = await fetch(
          `/api/projects/${encodeURIComponent(projectId)}/docs-candidates/${encodeURIComponent(candidate.id)}/${action}`,
          {
            method: "POST",
            credentials: "include",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ version: candidate.version }),
          },
        );
        if (response.status === 409) {
          await load();
          setError("この候補は別の操作で更新されました。最新状態を再読み込みしました。");
          return;
        }
        if (!response.ok) {
          throw new Error(
            await readError(
              response,
              action === "approve"
                ? "Docs候補を承認できませんでした"
                : "Docs候補を却下できませんでした",
            ),
          );
        }
        await load();
      } catch (cause) {
        setError(cause instanceof Error ? cause.message : "Docs候補の更新に失敗しました");
      } finally {
        setPendingId(null);
      }
    },
    [canManageSettings, load, pendingId, projectId],
  );

  return (
    <Card className="border-border bg-card shadow-none" data-testid="project-docs-candidates-panel">
      <CardHeader className="border-b border-border">
        <div className="flex items-center justify-between gap-3">
          <CardTitle className="flex items-center gap-2 text-base font-semibold">
            <FileText className="size-4" />
            Docs候補レビュー
            <Badge variant="secondary">{total}</Badge>
          </CardTitle>
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={() => void load()}
            disabled={loading}
            aria-label="Docs候補を更新"
          >
            <RefreshCw className={`mr-2 size-4${loading ? " animate-spin" : ""}`} />
            更新
          </Button>
        </div>
        {!canManageSettings ? (
          <p className="text-xs text-muted-foreground">
            Docs候補の承認・却下にはmanage_settings権限が必要です。
          </p>
        ) : null}
      </CardHeader>
      <CardContent className="space-y-3 pt-5">
        {error ? (
          <div role="alert" className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm">
            {error}
          </div>
        ) : null}
        {loading && items.length === 0 ? (
          <p className="flex items-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="size-4 animate-spin" />
            Docs候補を読み込み中…
          </p>
        ) : items.length === 0 ? (
          <p className="text-sm text-muted-foreground">確認待ちのDocs候補はありません。</p>
        ) : (
          <ul className="space-y-3">
            {items.map((candidate) => {
              const content = candidateContent(candidate);
              const busy = pendingId === candidate.id;
              return (
                <li key={candidate.id} className="rounded-md border border-border p-3" data-testid={`docs-candidate-${candidate.id}`}>
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div className="min-w-0 space-y-1">
                      {content.title ? <h3 className="break-words text-sm font-semibold">{content.title}</h3> : null}
                      {content.section ? <p className="text-xs text-muted-foreground">セクション: {content.section}</p> : null}
                      {content.body ? <p className="whitespace-pre-wrap break-words text-sm text-muted-foreground">{content.body}</p> : null}
                    </div>
                    <div className="flex shrink-0 flex-wrap items-center gap-1.5 text-xs">
                      <Badge variant="outline">confidence {Math.round(candidate.confidence * 100)}%</Badge>
                      <Badge variant="outline">importance {candidate.importance}</Badge>
                      <Badge variant="secondary">{candidate.status}</Badge>
                    </div>
                  </div>
                  {canManageSettings ? (
                    <div className="mt-3 flex flex-wrap gap-2 border-t border-border pt-3">
                      <Button type="button" size="sm" onClick={() => void mutate(candidate, "approve")} disabled={busy || pendingId !== null}>
                        {busy ? <Loader2 className="mr-2 size-4 animate-spin" /> : <Check className="mr-2 size-4" />}
                        承認
                      </Button>
                      <Button type="button" size="sm" variant="outline" onClick={() => void mutate(candidate, "reject")} disabled={busy || pendingId !== null}>
                        {busy ? <Loader2 className="mr-2 size-4 animate-spin" /> : <X className="mr-2 size-4" />}
                        却下
                      </Button>
                    </div>
                  ) : null}
                </li>
              );
            })}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}
