"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { AlertCircle, CheckCircle2, Clock3, Loader2, RefreshCw } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { components } from "@/lib/api-types.gen";

type ContextPackStatus = "fresh" | "stale" | "building" | "failed" | "pending";

type ContextPackStatusResponse = components["schemas"]["ContextPackStatusResponse"];

type ProjectContextPackStatusProps = {
  projectId: string;
  canManageSettings: boolean;
};

const POLL_INTERVAL_MS = 1500;
const MAX_POLL_ATTEMPTS = 8;

const STATUS_LABELS: Record<ContextPackStatus, string> = {
  fresh: "Fresh",
  stale: "Stale",
  building: "Building",
  failed: "Failed",
  pending: "Building",
};

function normalizeStatus(value: unknown): ContextPackStatus {
  switch (String(value ?? "stale").trim().toLowerCase()) {
    case "fresh":
      return "fresh";
    case "building":
    case "pending":
    case "running":
      return "building";
    case "failed":
      return "failed";
    default:
      return "stale";
  }
}

function isRebuildTerminalStatus(value: unknown) {
  const status = normalizeStatus(value);
  return status === "fresh" || status === "failed";
}

function errorDetail(body: unknown, fallback: string) {
  if (body && typeof body === "object" && "detail" in body) {
    const detail = (body as { detail?: unknown }).detail;
    if (typeof detail === "string" && detail.trim()) return detail;
  }
  return fallback;
}

async function readError(response: Response, fallback: string) {
  return errorDetail(await response.json().catch(() => null), fallback);
}

function formatGeneratedAt(value: string | null | undefined) {
  if (!value) return "未生成";
  const timestamp = new Date(value);
  return Number.isNaN(timestamp.valueOf()) ? value : timestamp.toLocaleString("ja-JP");
}

/**
 * Displays the derived Project Context Pack lifecycle.  The component talks
 * only to the Next BFF so browser callers never need the internal Python API
 * key or a second ACL implementation.
 */
export function ProjectContextPackStatus({
  projectId,
  canManageSettings,
}: ProjectContextPackStatusProps) {
  const [pack, setPack] = useState<ContextPackStatusResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [rebuilding, setRebuilding] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const requestGenerationRef = useRef(0);
  const requestControllerRef = useRef<AbortController | null>(null);
  const rebuildControllerRef = useRef<AbortController | null>(null);
  const pollTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pollAttemptsRef = useRef(0);

  const clearPoll = useCallback(() => {
    if (pollTimerRef.current !== null) {
      clearTimeout(pollTimerRef.current);
      pollTimerRef.current = null;
    }
  }, []);

  const load = useCallback(async () => {
    const generation = ++requestGenerationRef.current;
    requestControllerRef.current?.abort();
    const controller = new AbortController();
    requestControllerRef.current = controller;
    setLoading(true);
    setError(null);

    try {
      const response = await fetch(
        `/api/projects/${encodeURIComponent(projectId)}/context-pack`,
        { credentials: "include", signal: controller.signal },
      );
      if (!response.ok) {
        throw new Error(await readError(response, "Project Context Packを取得できませんでした"));
      }
      const body = (await response.json()) as ContextPackStatusResponse;
      if (controller.signal.aborted || generation !== requestGenerationRef.current) return body;
      setPack(body);
      return body;
    } catch (cause) {
      if (controller.signal.aborted || generation !== requestGenerationRef.current) return null;
      setError(cause instanceof Error ? cause.message : "Project Context Packを取得できませんでした");
      return null;
    } finally {
      if (!controller.signal.aborted && generation === requestGenerationRef.current) {
        setLoading(false);
      }
    }
  }, [projectId]);

  const schedulePoll = useCallback(() => {
    clearPoll();
    if (pollAttemptsRef.current >= MAX_POLL_ATTEMPTS) {
      setRebuilding(false);
      return;
    }
    pollTimerRef.current = setTimeout(async () => {
      pollTimerRef.current = null;
      pollAttemptsRef.current += 1;
      const next = await load();
      if (next && !isRebuildTerminalStatus(next.status)) {
        schedulePoll();
      } else {
        setRebuilding(false);
      }
    }, POLL_INTERVAL_MS);
  }, [clearPoll, load]);

  useEffect(() => {
    clearPoll();
    pollAttemptsRef.current = 0;
    void load();
    return () => {
      requestGenerationRef.current += 1;
      requestControllerRef.current?.abort();
      rebuildControllerRef.current?.abort();
      clearPoll();
    };
  }, [clearPoll, load]);

  const rebuild = useCallback(async () => {
    if (!canManageSettings || rebuilding) return;
    clearPoll();
    setRebuilding(true);
    setError(null);
    pollAttemptsRef.current = 0;
    const controller = new AbortController();
    rebuildControllerRef.current?.abort();
    rebuildControllerRef.current = controller;
    try {
      const response = await fetch(
        `/api/projects/${encodeURIComponent(projectId)}/context-pack/rebuild`,
        {
          method: "POST",
          credentials: "include",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({}),
          signal: controller.signal,
        },
      );
      if (!response.ok && response.status !== 202) {
        throw new Error(await readError(response, "Project Context Packを再構築できませんでした"));
      }
      setPack((current) => ({ ...(current ?? {}), status: "building", stale: true }));
      const next = await load();
      if (next && !isRebuildTerminalStatus(next.status)) {
        schedulePoll();
      } else {
        setRebuilding(false);
      }
    } catch (cause) {
      if (controller.signal.aborted) return;
      setRebuilding(false);
      setError(cause instanceof Error ? cause.message : "Project Context Packの再構築に失敗しました");
    } finally {
      if (rebuildControllerRef.current === controller) {
        rebuildControllerRef.current = null;
      }
    }
  }, [canManageSettings, clearPoll, load, projectId, rebuilding, schedulePoll]);

  const status = normalizeStatus(pack?.status);
  const statusIcon =
    status === "fresh" ? <CheckCircle2 className="size-4" /> :
      status === "failed" ? <AlertCircle className="size-4" /> :
        status === "building" ? <Loader2 className="size-4 animate-spin" /> :
          <Clock3 className="size-4" />;

  return (
    <Card className="border-border bg-card shadow-none" data-testid="project-context-pack-status">
      <CardHeader className="border-b border-border">
        <div className="flex items-center justify-between gap-3">
          <CardTitle className="flex items-center gap-2 text-base font-semibold">
            <RefreshCw className="size-4" />
            Project Context Pack
          </CardTitle>
          <Badge variant={status === "fresh" ? "secondary" : "outline"}>
            <span className="mr-1 inline-flex">{statusIcon}</span>
            {STATUS_LABELS[status]}
          </Badge>
        </div>
        {!canManageSettings ? (
          <p className="text-xs text-muted-foreground">
            再構築にはmanage_settings権限が必要です。現在の状態のみ表示しています。
          </p>
        ) : null}
      </CardHeader>
      <CardContent className="space-y-3 pt-5">
        {error ? (
          <div role="alert" className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm">
            {error}
          </div>
        ) : null}
        {loading && !pack ? (
          <p className="flex items-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="size-4 animate-spin" />
            Project Context Packを読み込み中…
          </p>
        ) : (
          <>
            <dl className="grid gap-2 text-sm sm:grid-cols-2">
              <div>
                <dt className="text-xs text-muted-foreground">generated_at</dt>
                <dd data-testid="context-pack-generated-at">{formatGeneratedAt(pack?.generated_at)}</dd>
              </div>
              <div>
                <dt className="text-xs text-muted-foreground">状態</dt>
                <dd>{pack?.stale || status === "stale" ? "stale" : STATUS_LABELS[status]}</dd>
              </div>
            </dl>
            {canManageSettings ? (
              <Button type="button" size="sm" onClick={() => void rebuild()} disabled={rebuilding}>
                {rebuilding ? <Loader2 className="mr-2 size-4 animate-spin" /> : <RefreshCw className="mr-2 size-4" />}
                再構築
              </Button>
            ) : null}
          </>
        )}
      </CardContent>
    </Card>
  );
}
