"use client";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import type {
  FreeTokenRuntimeSettings,
  LlamaCppRuntimeSettings,
  LocalRuntimeStatus,
  LocalRuntimeTask,
  ManagedLocalRuntimeProfile,
} from "./llm-model-section-types";

function runtimeLabel(runtime: string | null | undefined): string {
  if (runtime === "freetoken") return "FreeToken";
  if (runtime === "llama_cpp") return "llama.cpp";
  return runtime || "Managed local runtime";
}

function statusLabel(status: string): string {
  switch (status) {
    case "installed":
    case "ready":
    case "succeeded":
      return "準備済み";
    case "preparing":
    case "running":
    case "queued":
      return "準備中";
    case "missing":
    case "runtime_missing":
    case "model_missing":
      return "未準備";
    case "installer_required":
      return "手動インストーラーが必要";
    case "manual_required":
    case "manual_model_required":
      return "手動準備が必要";
    case "unsupported":
      return "非対応";
    case "failed":
      return "失敗";
    case "not_checked":
      return "未確認";
    case "external":
      return "外部サーバー";
    default:
      return status || "不明";
  }
}

function statusVariant(
  status: string,
): "default" | "secondary" | "destructive" | "outline" {
  if (["installed", "ready", "succeeded"].includes(status)) return "default";
  if (["failed", "unsupported"].includes(status)) return "destructive";
  if (["preparing", "running", "queued"].includes(status)) return "secondary";
  return "outline";
}

function booleanSettingLabel(value: unknown): string {
  if (typeof value === "boolean") return value ? "ON" : "OFF";
  if (typeof value === "number") return value !== 0 ? "ON" : "OFF";
  const normalized = String(value ?? "").trim().toLowerCase();
  if (["1", "true", "yes", "on"].includes(normalized)) return "ON";
  if (["0", "false", "no", "off"].includes(normalized)) return "OFF";
  return "未設定";
}

function redactLocalPathToken(token: string): string {
  const separator = token.indexOf("=");
  const prefix = separator >= 0 ? token.slice(0, separator + 1) : "";
  const value = separator >= 0 ? token.slice(separator + 1) : token;
  if (
    /^(?:[a-z]:[\\/]|\\\\|~[\\/]|\/|file:)/i.test(value)
  ) {
    return `${prefix}<path redacted>`;
  }
  return token;
}

function safeExtraArgs(value: FreeTokenRuntimeSettings["extra_args"]): string {
  const values = Array.isArray(value)
    ? value.map((item) => String(item))
    : String(value ?? "").split(/\r?\n/);
  const rendered = values
    .map((item) => item.trim())
    .filter(Boolean)
    .map((item) =>
      item
        .split(/\s+/)
        .map(redactLocalPathToken)
        .join(" "),
    );
  return rendered.length ? rendered.join(" ") : "なし";
}

function profileFormat(profile: ManagedLocalRuntimeProfile | null): string {
  if (!profile) return "";
  if ("model_format" in profile && profile.model_format) {
    return profile.model_format;
  }
  if ("gguf_filename" in profile && profile.gguf_filename) {
    const quantization = profile.quantization?.trim();
    return quantization ? `GGUF · ${quantization}` : "GGUF";
  }
  return "";
}

export function ManagedLocalRuntimePanel({
  status,
  task,
  loading,
  preparing,
  runtimeSettings,
  runtimeProfile,
  onStart,
}: {
  status: LocalRuntimeStatus | null;
  task: LocalRuntimeTask | null;
  loading: boolean;
  preparing: boolean;
  runtimeSettings?: FreeTokenRuntimeSettings | LlamaCppRuntimeSettings | null;
  runtimeProfile?: ManagedLocalRuntimeProfile | null;
  onStart: () => void | Promise<void>;
}) {
  const effective = task && (!task.done || task.status === "failed")
    ? task
    : status ?? task;
  if (!effective && !loading) return null;

  const effectiveStatus = effective?.status ?? "missing";
  const runtime = effective?.runtime ?? runtimeProfile?.runtime ?? null;
  const legacyRuntimeStatus = effective?.runtime_installed
    ? "installed"
    : ["installer_required", "unsupported", "runtime_missing"].includes(effectiveStatus)
      ? effectiveStatus
      : "missing";
  const legacyArtifactStatus = effective?.model_installed
    ? "installed"
    : [
        "model_missing",
        "manual_model_required",
        "failed",
        "preparing",
        "running",
        "queued",
      ].includes(effectiveStatus)
      ? effectiveStatus
      : "missing";
  const runtimeInstall = effective?.runtime_install ?? {
    runtime,
    installed: effective?.runtime_installed === true,
    status: legacyRuntimeStatus,
    installer_required: effectiveStatus === "installer_required",
  };
  const modelArtifact = effective?.model_artifact ?? {
    installed: effective?.model_installed === true,
    status: legacyArtifactStatus,
    managed: runtime !== null,
    percent: effective?.percent ?? 0,
  };
  const server = effective?.server ?? {
    status: effectiveStatus === "external" ? "external" : "not_checked",
    ready: effectiveStatus === "external" ? false : null,
  };
  const actions = effective?.actions ?? {
    prepare: Boolean(
      effective?.prepare_supported
      && effective?.done !== false
      && effective?.prepared !== true
    ),
    retry: Boolean(
      effective?.prepare_supported && effectiveStatus === "failed"
    ),
  };
  const runtimeStatus = runtimeInstall.status ?? legacyRuntimeStatus;
  const runtimeDistribution = String(
    runtimeInstall.distribution
      ?? effective?.runtime_distribution
      ?? (runtimeProfile && "runtime_distribution" in runtimeProfile
        ? runtimeProfile.runtime_distribution
        : undefined)
      ?? "",
  ).trim();
  const artifactStatus = modelArtifact.status ?? legacyArtifactStatus;
  const serverStatus = server.status ?? "not_checked";
  const runtimeInstalled = runtimeInstall.installed === true;
  const modelInstalled = modelArtifact.installed === true;
  const auxiliaryArtifacts = modelArtifact.auxiliary_artifacts ?? [];
  const prepared = effective?.prepared === true;
  const canPrepare = Boolean(
    prepared || actions.prepare || actions.retry,
  );
  const percent = Math.max(
    0,
    Math.min(
      100,
      Number(modelArtifact.percent ?? effective?.percent ?? 0),
    ),
  );
  const version = runtimeInstall.version ?? effective?.runtime_version;
  const build = runtimeInstall.build ?? effective?.runtime_build;
  const versionText = [
    version ? String(version) : "",
    build !== undefined && build !== null ? `b${build}` : "",
  ].filter(Boolean).join(" · ");
  const reason = effective?.reason || effective?.error || "";
  const sourceRepository = String(
    runtimeProfile?.source_repository ?? "",
  ).trim();
  const format = profileFormat(runtimeProfile ?? null);
  const isFreeToken = runtime === "freetoken";
  const host = String(runtimeSettings?.host ?? "").trim() || "未設定";
  const port = runtimeSettings?.port === undefined
    || runtimeSettings.port === null
    || String(runtimeSettings.port).trim() === ""
    ? "未設定"
    : String(runtimeSettings.port);
  const extraArgs = safeExtraArgs(runtimeSettings?.extra_args);
  const failed = effective?.status === "failed" || Boolean(effective?.error);
  const buttonLabel = preparing
    ? "準備中…"
    : prepared
      ? "実行"
      : runtimeInstalled && !modelInstalled
        ? "モデルをダウンロードして実行"
        : "準備して実行";

  return (
    <Card
      size="sm"
      className="rounded-md border-sky-500/30 bg-card py-0"
      aria-label={`${runtimeLabel(runtime)} managed runtime`}
      aria-busy={loading || preparing}
    >
      <CardHeader className="border-b px-3 py-3">
        <CardTitle className="flex flex-wrap items-center gap-2 text-sm">
          <span>{runtimeLabel(runtime)}</span>
          <Badge variant={statusVariant(effectiveStatus)}>
            {loading ? "確認中" : statusLabel(effectiveStatus)}
          </Badge>
        </CardTitle>
        <CardDescription className="text-xs">
          選択中モデルは準備が完了するまで現在の実行モデルへ切り替えません。
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3 px-3 py-3" aria-live="polite">
        <dl className="grid gap-2 text-xs sm:grid-cols-3">
          <div className="space-y-1 rounded border p-2">
            <dt className="text-muted-foreground">Runtime</dt>
            <dd>
              <Badge variant={statusVariant(runtimeStatus)}>
                {statusLabel(runtimeStatus)}
              </Badge>
            </dd>
            <dd className="break-words text-muted-foreground">
              version: {versionText || "未取得"}
            </dd>
          </div>
          <div className="space-y-1 rounded border p-2">
            <dt className="text-muted-foreground">Model artifact</dt>
            <dd>
              <Badge variant={statusVariant(artifactStatus)}>
                {statusLabel(artifactStatus)}
              </Badge>
            </dd>
          </div>
          <div className="space-y-1 rounded border p-2">
            <dt className="text-muted-foreground">Server</dt>
            <dd>
              <Badge variant={statusVariant(serverStatus)}>
                {server.ready === true ? "ready" : statusLabel(serverStatus)}
              </Badge>
            </dd>
          </div>
        </dl>

        {(sourceRepository || format) && (
          <dl className="grid gap-2 rounded border p-2 text-xs sm:grid-cols-2">
            {sourceRepository && (
              <div className="min-w-0 space-y-0.5">
                <dt className="text-muted-foreground">Model source</dt>
                <dd className="break-words font-medium">{sourceRepository}</dd>
              </div>
            )}
            {format && (
              <div className="space-y-0.5">
                <dt className="text-muted-foreground">Format</dt>
                <dd className="font-medium">{format}</dd>
              </div>
            )}
          </dl>
        )}

        {(runtimeDistribution || auxiliaryArtifacts.length > 0) && (
          <dl className="grid gap-2 rounded border p-2 text-xs sm:grid-cols-2">
            {runtimeDistribution && (
              <div className="space-y-0.5">
                <dt className="text-muted-foreground">Runtime distribution</dt>
                <dd className="font-medium">{runtimeDistribution}</dd>
              </div>
            )}
            {auxiliaryArtifacts.length > 0 && (
              <div className="space-y-0.5">
                <dt className="text-muted-foreground">Auxiliary artifacts</dt>
                <dd className="space-y-0.5 font-medium">
                  {auxiliaryArtifacts.map((artifact) => (
                    <div key={`${artifact.id ?? artifact.filename}-status`}>
                      {artifact.filename ?? artifact.id ?? "artifact"}
                      {artifact.required ? " · required" : ""}
                      {artifact.status ? ` · ${artifact.status}` : ""}
                    </div>
                  ))}
                </dd>
              </div>
            )}
          </dl>
        )}

        {isFreeToken && (
          <dl
            className="grid gap-2 rounded border p-2 text-xs sm:grid-cols-2"
            aria-label="FreeToken runtime settings"
          >
            <div>
              <dt className="text-muted-foreground">Host</dt>
              <dd className="font-medium">{host}</dd>
            </div>
            <div>
              <dt className="text-muted-foreground">Port</dt>
              <dd className="font-medium">{port}</dd>
            </div>
            <div>
              <dt className="text-muted-foreground">Auto start</dt>
              <dd className="font-medium">
                {booleanSettingLabel(runtimeSettings?.auto_start)}
              </dd>
            </div>
            <div className="min-w-0">
              <dt className="text-muted-foreground">Additional args</dt>
              <dd className="break-words font-medium">{extraArgs}</dd>
            </div>
          </dl>
        )}

        {(preparing || (task && !task.done)) && (
          <div className="space-y-1">
            <div
              className="h-2 overflow-hidden rounded bg-muted"
              role="progressbar"
              aria-label="モデル準備進捗"
              aria-valuemin={0}
              aria-valuemax={100}
              aria-valuenow={percent}
            >
              <div
                className="h-full bg-primary transition-all"
                style={{ width: `${percent}%` }}
              />
            </div>
            <p className="text-right text-xs text-muted-foreground">{percent}%</p>
          </div>
        )}

        {reason && (
          <p
            className={failed ? "text-xs text-destructive" : "text-xs text-muted-foreground"}
            role={failed ? "alert" : "status"}
          >
            {reason}
          </p>
        )}

        <Button
          type="button"
          size="sm"
          onClick={() => void onStart()}
          disabled={loading || preparing || !effective || !canPrepare}
          aria-busy={preparing}
        >
          {buttonLabel}
        </Button>
      </CardContent>
    </Card>
  );
}
