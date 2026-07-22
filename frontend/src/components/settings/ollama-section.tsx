"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import useSWR from "swr";
import {
  Bot,
  ChevronDown,
  ChevronUp,
  Download,
  Loader2,
  RefreshCcw,
} from "lucide-react";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Separator } from "@/components/ui/separator";
import { formatBytes } from "@/lib/utils";

interface OllamaStatus {
  available: boolean;
  base_url: string;
  version?: string;
  error?: string;
}

interface OllamaModel {
  name?: string;
  model?: string;
  size?: number;
  modified_at?: string;
  digest?: string;
  details?: {
    parameter_size?: string;
    quantization_level?: string;
    family?: string;
  };
}

interface OllamaModelsResponse {
  success: boolean;
  base_url: string;
  models: OllamaModel[];
  status: OllamaStatus;
  error?: string;
}

interface OllamaPullTask {
  task_id: string;
  model: string;
  status: string;
  message?: string;
  completed?: number;
  total?: number;
  percent?: number;
  done: boolean;
  error?: string | null;
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

function modelName(model: OllamaModel): string {
  return model.name || model.model || "";
}

export function OllamaSection() {
  const [expanded, setExpanded] = useState(false);
  // Ollamaモデル一覧（サーバー状態）は SWR で管理。取得タイミングは従来どおり
  // 呼び出し側（トグル/更新/pull完了後）で駆動するため自動 revalidation は無効化する。
  // 取得失敗時は従来同様に直前値を保持する。
  const dataRef = useRef<OllamaModelsResponse | null>(null);
  const { data = null, mutate: mutateModels } = useSWR<OllamaModelsResponse | null>(
    "settings/ollama-models",
    async () => {
      try {
        return await pyFetch<OllamaModelsResponse>("/ollama/models");
      } catch (error) {
        toast.error(
          error instanceof Error ? error.message : "Failed to load Ollama models",
        );
        return dataRef.current;
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
  dataRef.current = data;
  const [modelInput, setModelInput] = useState("gemma4:e4b");
  const [loading, setLoading] = useState(false);
  const [pulling, setPulling] = useState(false);
  const [task, setTask] = useState<OllamaPullTask | null>(null);

  const installedNames = useMemo(
    () => new Set((data?.models ?? []).map(modelName).filter(Boolean)),
    [data],
  );

  const loadModels = useCallback(async () => {
    setLoading(true);
    try {
      await mutateModels();
    } finally {
      setLoading(false);
    }
  }, [mutateModels]);

  useEffect(() => {
    if (expanded && !data) void loadModels();
  }, [expanded, data, loadModels]);

  useEffect(() => {
    if (!task || task.done) return;
    const interval = window.setInterval(async () => {
      try {
        const next = await pyFetch<OllamaPullTask>(
          `/ollama/pull/${encodeURIComponent(task.task_id)}`,
        );
        setTask(next);
        if (next.done) {
          setPulling(false);
          if (next.error) {
            toast.error(next.error);
          } else {
            toast.success(`${next.model} downloaded`);
            void loadModels();
          }
        }
      } catch (error) {
        setPulling(false);
        const message =
          error instanceof Error ? error.message : "Failed to poll Ollama pull";
        setTask((current) =>
          current?.task_id === task.task_id
            ? { ...current, status: "error", message, done: true, error: message }
            : current,
        );
        toast.error(message);
      }
    }, 1000);
    return () => window.clearInterval(interval);
  }, [loadModels, task]);

  const startPull = useCallback(async () => {
    const model = modelInput.trim();
    if (!model) return;
    setPulling(true);
    try {
      const started = await pyFetch<OllamaPullTask>("/ollama/pull", {
        method: "POST",
        body: JSON.stringify({ model }),
      });
      setTask(started);
      toast.success(`Started downloading ${model}`);
    } catch (error) {
      setPulling(false);
      toast.error(
        error instanceof Error ? error.message : "Failed to start download",
      );
    }
  }, [modelInput]);

  const status = data?.status;
  const percent = Math.max(0, Math.min(100, task?.percent ?? 0));

  return (
    <Card size="sm">
      <CardHeader
        className="cursor-pointer select-none"
        onClick={() => setExpanded((value) => !value)}
      >
        <CardTitle className="flex items-center justify-between gap-3 text-sm">
          <span className="flex min-w-0 items-center gap-2">
            <Bot className="size-4" />
            <span>Ollama</span>
            {status && (
              <Badge variant={status.available ? "default" : "secondary"}>
                {status.available
                  ? status.version
                    ? `v${status.version}`
                    : "available"
                  : "offline"}
              </Badge>
            )}
          </span>
          {expanded ? (
            <ChevronUp className="size-4 shrink-0" />
          ) : (
            <ChevronDown className="size-4 shrink-0" />
          )}
        </CardTitle>
      </CardHeader>
      {expanded && (
        <CardContent className="space-y-4">
          <div className="flex flex-wrap items-end gap-2">
            <div className="min-w-64 flex-1 space-y-1">
              <Label className="text-xs">Model tag</Label>
              <Input
                value={modelInput}
                onChange={(event) => setModelInput(event.target.value)}
                placeholder="llama3.2:3b"
                className="h-8"
              />
            </div>
            <Button
              size="sm"
              onClick={startPull}
              disabled={pulling || !modelInput.trim()}
            >
              {pulling ? (
                <Loader2 className="mr-1 size-3 animate-spin" />
              ) : (
                <Download className="mr-1 size-3" />
              )}
              Pull
            </Button>
            <Button
              size="sm"
              variant="outline"
              onClick={loadModels}
              disabled={loading}
            >
              <RefreshCcw
                className={`mr-1 size-3 ${loading ? "animate-spin" : ""}`}
              />
              Refresh
            </Button>
          </div>

          {status && !status.available && (
            <p className="text-xs text-destructive">
              Ollama is not reachable at {status.base_url}
              {status.error ? `: ${status.error}` : ""}
            </p>
          )}

          {task && (
            <div className="space-y-2 rounded border p-3">
              <div className="flex items-center justify-between gap-3 text-xs">
                <span className="font-medium">{task.model}</span>
                <Badge variant={task.error ? "destructive" : "secondary"}>
                  {task.status}
                </Badge>
              </div>
              <div className="h-2 overflow-hidden rounded bg-muted">
                <div
                  className="h-full bg-primary transition-all"
                  style={{ width: `${percent}%` }}
                />
              </div>
              <div className="flex justify-between gap-3 text-xs text-muted-foreground">
                <span>{task.message || task.status}</span>
                <span>
                  {percent}% {formatBytes(task.completed)} /{" "}
                  {formatBytes(task.total)}
                </span>
              </div>
              {task.error && (
                <p className="text-xs text-destructive">{task.error}</p>
              )}
            </div>
          )}

          <Separator />

          <div className="space-y-2">
            <div className="flex items-center justify-between gap-3">
              <p className="text-xs font-medium">Installed models</p>
              <Badge variant="secondary">{data?.models.length ?? 0}</Badge>
            </div>
            {loading && !data ? (
              <p className="text-xs text-muted-foreground">Loading...</p>
            ) : data?.models.length ? (
              <div className="space-y-2">
                {data.models.map((model) => {
                  const name = modelName(model);
                  return (
                    <div
                      key={name}
                      className="flex items-center justify-between gap-3 rounded border px-3 py-2"
                    >
                      <div className="min-w-0">
                        <p className="truncate text-xs font-medium">{name}</p>
                        <p className="text-xs text-muted-foreground">
                          {model.details?.parameter_size || "-"} /{" "}
                          {model.details?.quantization_level || "-"} /{" "}
                          {formatBytes(model.size)}
                        </p>
                      </div>
                      <Badge
                        variant={
                          installedNames.has(name) ? "default" : "secondary"
                        }
                      >
                        installed
                      </Badge>
                    </div>
                  );
                })}
              </div>
            ) : (
              <p className="text-xs text-muted-foreground">
                No Ollama models found.
              </p>
            )}
          </div>
        </CardContent>
      )}
    </Card>
  );
}
