"use client";

import { useCallback, useEffect, useState } from "react";
import { Loader2, Save, Settings2 } from "lucide-react";
import {
  KNOWLEDGE_CAPTURE_MODE_OPTIONS,
  normalizeKnowledgeCaptureMode,
  type KnowledgeCaptureMode,
} from "@/lib/knowledge-capture";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

type ProjectKnowledgeCaptureSettingsProps = {
  projectId: string;
  canManageSettings: boolean;
};

function errorDetail(body: unknown, fallback: string): string {
  if (body && typeof body === "object" && "detail" in body) {
    const detail = (body as { detail?: unknown }).detail;
    if (typeof detail === "string" && detail.trim()) return detail;
  }
  return fallback;
}

async function readError(response: Response, fallback: string): Promise<string> {
  return errorDetail(await response.json().catch(() => null), fallback);
}

export function ProjectKnowledgeCaptureSettings({
  projectId,
  canManageSettings,
}: ProjectKnowledgeCaptureSettingsProps) {
  const [mode, setMode] = useState<KnowledgeCaptureMode>("suggest");
  const [loading, setLoading] = useState(canManageSettings);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  const load = useCallback(async () => {
    if (!canManageSettings) return;
    setLoading(true);
    setError(null);
    setSaved(false);
    try {
      const response = await fetch(
        `/api/projects/${encodeURIComponent(projectId)}/knowledge-capture/settings`,
        { credentials: "include", cache: "no-store" },
      );
      if (!response.ok) {
        throw new Error(
          await readError(response, "ナレッジ化設定を取得できませんでした"),
        );
      }
      setMode(normalizeKnowledgeCaptureMode(await response.json()));
    } catch (cause) {
      setError(
        cause instanceof Error
          ? cause.message
          : "ナレッジ化設定を取得できませんでした",
      );
    } finally {
      setLoading(false);
    }
  }, [canManageSettings, projectId]);

  useEffect(() => {
    void load();
  }, [load]);

  const save = useCallback(async () => {
    if (!canManageSettings || saving) return;
    setSaving(true);
    setError(null);
    setSaved(false);
    try {
      const response = await fetch(
        `/api/projects/${encodeURIComponent(projectId)}/knowledge-capture/settings`,
        {
          method: "PATCH",
          credentials: "include",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ mode }),
        },
      );
      if (!response.ok) {
        throw new Error(
          await readError(response, "ナレッジ化設定を保存できませんでした"),
        );
      }
      setMode(normalizeKnowledgeCaptureMode(await response.json()));
      setSaved(true);
    } catch (cause) {
      setError(
        cause instanceof Error
          ? cause.message
          : "ナレッジ化設定を保存できませんでした",
      );
    } finally {
      setSaving(false);
    }
  }, [canManageSettings, mode, projectId, saving]);

  return (
    <Card
      className="border-border bg-card shadow-none"
      data-testid="project-knowledge-capture-settings"
    >
      <CardHeader className="border-b border-border">
        <CardTitle className="flex items-center gap-2 text-base font-semibold">
          <Settings2 className="size-4" />
          解決ナレッジ化
        </CardTitle>
        <p className="text-sm text-muted-foreground">
          解決した作業から、再利用できる手順をこのProjectのDocs候補として整理します。
        </p>
      </CardHeader>
      <CardContent className="space-y-4 pt-5">
        {!canManageSettings ? (
          <p className="text-sm text-muted-foreground">
            この設定の変更にはmanage_settings権限が必要です。
          </p>
        ) : loading ? (
          <p className="flex items-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="size-4 animate-spin" />
            設定を読み込み中…
          </p>
        ) : (
          <>
            <fieldset className="space-y-3" disabled={saving}>
              <legend className="text-sm font-medium">保存の動作</legend>
              {KNOWLEDGE_CAPTURE_MODE_OPTIONS.map((option) => (
                <label
                  key={option.value}
                  className={`flex cursor-pointer gap-3 rounded-md border p-3 transition-colors ${
                    mode === option.value
                      ? "border-primary bg-primary/5"
                      : "border-border hover:bg-muted/40"
                  }`}
                >
                  <input
                    type="radio"
                    name={`knowledge-capture-mode-${projectId}`}
                    value={option.value}
                    checked={mode === option.value}
                    onChange={() => setMode(option.value)}
                    className="mt-1"
                  />
                  <span className="min-w-0">
                    <span className="block text-sm font-medium">
                      {option.label}
                    </span>
                    <span className="mt-0.5 block text-xs text-muted-foreground">
                      {option.description}
                    </span>
                  </span>
                </label>
              ))}
            </fieldset>
            <div className="flex flex-wrap items-center gap-3">
              <Button type="button" size="sm" onClick={() => void save()} disabled={saving}>
                {saving ? (
                  <Loader2 className="mr-2 size-4 animate-spin" />
                ) : (
                  <Save className="mr-2 size-4" />
                )}
                保存
              </Button>
              {saved ? (
                <span className="text-xs text-muted-foreground" role="status">
                  保存しました。
                </span>
              ) : null}
            </div>
          </>
        )}
        {error ? (
          <div
            role="alert"
            className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm"
          >
            {error}
          </div>
        ) : null}
      </CardContent>
    </Card>
  );
}
