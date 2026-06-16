"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  Download,
  ExternalLink,
  Package,
  RefreshCw,
  Trash2,
} from "lucide-react";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { Button, buttonVariants } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import {
  appFactoryApi,
  type AppFactoryArtifact,
} from "@/lib/app-factory-api";
import { cn } from "@/lib/utils";

function formatFileSize(bytes?: number): string {
  if (typeof bytes !== "number" || !Number.isFinite(bytes)) return "-";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function formatCreatedAt(value?: string): string {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("ja-JP", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function kindLabel(kind: string): string {
  if (kind === "bat_macro") return "BAT Macro";
  if (kind === "hosted_web") return "AoiTalk WebUI";
  if (kind === "local_web") return "Local WebUI";
  return kind;
}

export default function AppFactoryPage() {
  const [artifacts, setArtifacts] = useState<AppFactoryArtifact[]>([]);
  const [loading, setLoading] = useState(true);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await appFactoryApi.listArtifacts(100);
      setArtifacts(data.artifacts);
    } catch (err) {
      const message = err instanceof Error ? err.message : "Load failed";
      setError(message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const counts = useMemo(() => {
    return artifacts.reduce(
      (acc, artifact) => {
        acc.total += 1;
        if (artifact.kind === "bat_macro") acc.macros += 1;
        else acc.web += 1;
        return acc;
      },
      { total: 0, web: 0, macros: 0 },
    );
  }, [artifacts]);

  const deleteArtifact = useCallback(
    async (artifact: AppFactoryArtifact) => {
      if (!window.confirm(`Delete ${artifact.title}?`)) return;
      setDeletingId(artifact.artifact_id);
      try {
        await appFactoryApi.deleteArtifact(artifact.artifact_id);
        setArtifacts((current) =>
          current.filter((item) => item.artifact_id !== artifact.artifact_id),
        );
        toast.success("Artifact deleted");
      } catch (err) {
        toast.error(err instanceof Error ? err.message : "Delete failed");
      } finally {
        setDeletingId(null);
      }
    },
    [],
  );

  return (
    <div className="mx-auto flex w-full max-w-6xl flex-col gap-4 p-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-normal">App Factory</h1>
          <p className="text-sm text-muted-foreground">
            Generated WebUI apps and macro packages from chat.
          </p>
        </div>
        <Button variant="outline" onClick={() => void refresh()} disabled={loading}>
          <RefreshCw className={cn("mr-2 size-4", loading && "animate-spin")} />
          Refresh
        </Button>
      </div>

      <div className="grid gap-3 sm:grid-cols-3">
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium">Total</CardTitle>
          </CardHeader>
          <CardContent className="text-2xl font-semibold">{counts.total}</CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium">WebUI</CardTitle>
          </CardHeader>
          <CardContent className="text-2xl font-semibold">{counts.web}</CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium">Macros</CardTitle>
          </CardHeader>
          <CardContent className="text-2xl font-semibold">{counts.macros}</CardContent>
        </Card>
      </div>

      {error && (
        <div className="rounded-md border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive">
          {error}
        </div>
      )}

      {loading ? (
        <div className="space-y-3">
          {Array.from({ length: 4 }).map((_, index) => (
            <Skeleton key={index} className="h-28 w-full rounded-md" />
          ))}
        </div>
      ) : artifacts.length === 0 ? (
        <Card>
          <CardContent className="py-12 text-center text-sm text-muted-foreground">
            No generated artifacts yet.
          </CardContent>
        </Card>
      ) : (
        <div className="space-y-3">
          {artifacts.map((artifact) => (
            <Card key={artifact.artifact_id}>
              <CardContent className="flex flex-col gap-3 p-4 lg:flex-row lg:items-center">
                <div className="flex min-w-0 flex-1 items-start gap-3">
                  <span className="flex size-10 shrink-0 items-center justify-center rounded-md bg-muted text-muted-foreground">
                    <Package className="size-5" />
                  </span>
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <h2 className="truncate text-base font-semibold">
                        {artifact.title}
                      </h2>
                      <Badge variant="secondary">{kindLabel(artifact.kind)}</Badge>
                      {!artifact.download_available && (
                        <Badge variant="destructive">Missing ZIP</Badge>
                      )}
                    </div>
                    <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-xs text-muted-foreground">
                      <span>{artifact.artifact_id}</span>
                      <span>{formatCreatedAt(artifact.created_at)}</span>
                      <span>{formatFileSize(artifact.download_size_bytes)}</span>
                      <span>{artifact.files.length} files</span>
                    </div>
                    {artifact.warnings.length > 0 && (
                      <div className="mt-2 flex items-start gap-1.5 text-xs text-amber-700 dark:text-amber-300">
                        <AlertTriangle className="mt-0.5 size-3.5 shrink-0" />
                        <span className="line-clamp-2">
                          {artifact.warnings.join(" / ")}
                        </span>
                      </div>
                    )}
                  </div>
                </div>
                <div className="flex shrink-0 flex-wrap gap-2">
                  <a
                    href={
                      artifact.download_available
                        ? appFactoryApi.downloadUrl(artifact.artifact_id)
                        : undefined
                    }
                    download
                    aria-disabled={!artifact.download_available}
                    tabIndex={artifact.download_available ? undefined : -1}
                    className={cn(
                      buttonVariants({ variant: "outline", size: "sm" }),
                      !artifact.download_available &&
                        "pointer-events-none opacity-50",
                    )}
                  >
                    <Download className="mr-2 size-4" />
                    Download
                  </a>
                  {artifact.preview_available && (
                    <a
                      href={appFactoryApi.previewUrl(artifact.artifact_id)}
                      target="_blank"
                      rel="noopener noreferrer"
                      className={buttonVariants({
                        variant: "outline",
                        size: "sm",
                      })}
                    >
                      <ExternalLink className="mr-2 size-4" />
                      Preview
                    </a>
                  )}
                  <Button
                    variant="ghost"
                    size="sm"
                    className="text-destructive hover:text-destructive"
                    disabled={deletingId === artifact.artifact_id}
                    onClick={() => void deleteArtifact(artifact)}
                  >
                    <Trash2 className="mr-2 size-4" />
                    Delete
                  </Button>
                </div>
              </CardContent>
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}
