"use client";

import type { Dispatch, SetStateAction } from "react";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import {
  reservedLlamaCppExtraArg,
  type LlamaCppRuntimeProfile,
  type LlamaCppRuntimeSettings,
  type LlamaCppSettingsDraft,
} from "./llm-model-section-types";

type DraftField = keyof LlamaCppSettingsDraft;

export function LlamaCppRuntimePanel({
  selectedModelId,
  runtimeProfile,
  runtimeSettings,
  draft,
  setDraft,
  saving,
  error,
  onSave,
}: {
  selectedModelId: string;
  runtimeProfile?: LlamaCppRuntimeProfile | null;
  runtimeSettings?: LlamaCppRuntimeSettings | null;
  draft: LlamaCppSettingsDraft;
  setDraft: Dispatch<SetStateAction<LlamaCppSettingsDraft>>;
  saving: boolean;
  error: string | null;
  onSave: () => void;
}) {
  const profile = runtimeProfile ?? null;
  const settings = runtimeSettings ?? null;
  const managedRuntime = Boolean(
    profile
      || String(settings?.runtime ?? settings?.server_profile ?? "")
        .trim()
        .toLowerCase()
        .replace(".", "_") === "llama_cpp",
  );
  const runtimeState = String(settings?.runtime_state ?? "").trim();
  const runtimeError = String(settings?.runtime_error ?? "").trim();
  const servedAlias = profile?.served_alias?.trim() || selectedModelId.trim();
  const aliasLocked = profile?.alias_locked === true && Boolean(profile?.served_alias?.trim());
  const requiredArgs = (profile?.required_args ?? []).map((item) => String(item).trim()).filter(Boolean);
  const ggufFilenames = profile?.gguf_filenames ?? [];
  const ggufShardCount = profile?.gguf_shard_count ?? (ggufFilenames.length > 0 ? ggufFilenames.length : undefined);
  const mtpProfile = profile?.mtp ?? null;
  const selectedIdentity = String(
    profile?.profile_id ?? profile?.served_alias ?? selectedModelId,
  ).trim().toLowerCase();
  const runtimeIdentity = String(
    settings?.profile_id
      ?? settings?.runtime_profile?.profile_id
      ?? settings?.runtime_profile?.served_alias
      ?? settings?.model_alias
      ?? "",
  ).trim().toLowerCase();
  const runtimeHasComputedMtpProjection = Boolean(
    settings
    && [
      settings.mtp_supported,
      settings.mtp_available,
      settings.mtp_status,
      settings.mtp_reason,
      settings.mtp_artifact_path,
      settings.mtp_resolved_model_path,
      settings.mtp_variant_model_path,
      settings.mtp_mode,
    ].some((value) => value !== undefined && value !== null),
  );
  // Provider-level settings can lag one render behind a model switch.  Do
  // not let a previous profile's computed MTP state make the new model look
  // supported; profile metadata remains the source of truth until the
  // backend returns a matching runtime projection.
  const mtpSettings = !runtimeIdentity && runtimeHasComputedMtpProjection
    ? null
    : runtimeIdentity && selectedIdentity && runtimeIdentity !== selectedIdentity
      ? null
      : settings;
  const runtimeMtpMode = String(mtpSettings?.mtp_mode ?? "").trim().toLowerCase();
  const runtimeMtpStatus = String(mtpSettings?.mtp_status ?? "").trim().toLowerCase();
  const boolValue = (value: unknown, fallback: boolean): boolean => {
    if (value === undefined || value === null || value === "") return fallback;
    if (typeof value === "boolean") return value;
    if (typeof value === "number") return value !== 0;
    return ["1", "true", "yes", "on"].includes(String(value).trim().toLowerCase());
  };
  const mtpDeclared = Boolean(mtpProfile)
    || boolValue(mtpSettings?.mtp_supported, false)
    || boolValue(mtpSettings?.mtp_enabled, false)
    || Boolean(runtimeMtpMode && runtimeMtpMode !== "unavailable")
    || Boolean(runtimeMtpStatus && !["unavailable", "not_applicable", "unsupported"].includes(runtimeMtpStatus));
  const mtpSupported = boolValue(mtpSettings?.mtp_supported, mtpProfile?.supported === true);
  const mtpEnabled = draft.mtp_enabled
    ?? mtpProfile?.default_enabled === true;
  const mtpStatusRaw = runtimeMtpStatus;
  const mtpArtifactPath = String(
      mtpSettings?.mtp_artifact_path
      ?? mtpSettings?.mtp_resolved_model_path
      ?? mtpSettings?.mtp_model_path
      ?? "",
  ).trim();
  const mtpVariantModelPath = String(
    mtpSettings?.mtp_variant_model_path ?? "",
  ).trim();
  const mtpAvailable = mtpSettings?.mtp_available !== undefined && mtpSettings?.mtp_available !== null
    ? boolValue(mtpSettings.mtp_available, false)
    : mtpStatusRaw === "unavailable" || mtpStatusRaw === "not_applicable"
      ? false
      : mtpStatusRaw === "ready"
        ? true
        : mtpSupported
          && String(mtpProfile?.mode ?? "").trim().toLowerCase() !== "embedded"
          && (Boolean(mtpArtifactPath)
            || String(mtpProfile?.mode ?? "").trim().toLowerCase() !== "companion");
  const mtpStatus = !mtpEnabled
    ? "disabled"
    : mtpAvailable
      ? "enabled / available"
      : "enabled / unavailable (fallback to standard decoding)";
  const mtpReason = [
    mtpSettings?.mtp_reason,
    mtpProfile?.reason,
    mtpProfile?.ui_notice,
    mtpProfile?.compatibility,
  ]
    .map((value) => String(value ?? "").trim())
    .find(Boolean) ?? "";
  const mtpStatusNotice = mtpStatusRaw && mtpStatusRaw !== "ready"
    ? `${mtpStatus} (${mtpStatusRaw})`
    : mtpStatus;
  const capabilityLabels = [
    profile?.capabilities?.reasoning ? "reasoning" : null,
    profile?.capabilities?.tools ? "tools" : null,
    profile?.capabilities?.media?.image ? "image" : null,
    profile?.capabilities?.media?.audio ? "audio" : null,
  ].filter((item): item is string => Boolean(item));
  const reservedExtraArg = draft.extra_args
    .split(/\r?\n/)
    .map((item) => item.trim())
    .map(reservedLlamaCppExtraArg)
    .find(Boolean) ?? null;
  const update = (field: DraftField, value: string | boolean) => {
    setDraft((current) => ({ ...current, [field]: value }));
  };

  return (
    <section
      aria-label="llama.cpp runtime設定"
      className="space-y-3 rounded-md border border-sky-500/30 bg-sky-50/30 p-3 dark:bg-sky-950/10"
    >
      <div className="space-y-1">
        <h3 className="text-xs font-semibold">llama.cpp / llama-server runtime</h3>
        <p className="text-[10px] text-muted-foreground">
          既存の <code>/v1</code> 互換ローカル provider を使います。設定保存時に、選択中のモデルと一緒に
          backendへ渡し、<code>auto_start</code> が有効なら既存の自動起動経路を利用します。
        </p>
      </div>

      {runtimeState && runtimeState !== "ready" && runtimeState !== "unmanaged" && (
        <div
          role="status"
          className="space-y-1 rounded border border-amber-500/40 bg-amber-50/70 p-2 text-[10px] dark:bg-amber-950/20"
        >
          <p className="font-medium">
            runtime状態: {runtimeState === "missing_model_path"
              ? "GGUFが未設定（自動検出できません）"
              : runtimeState === "model_path_not_found"
                ? "指定したGGUFが見つかりません"
                : runtimeState === "executable_not_found"
                  ? "llama-serverが見つかりません"
                  : runtimeState === "manual"
                    ? "外部の手動起動サーバーへ接続"
                    : runtimeState}
          </p>
          {runtimeError && <p>{runtimeError}</p>}
          {settings?.minimum_build !== undefined && settings.minimum_build !== null && (
            <p>必要なminimum build: <code>b{settings.minimum_build}</code></p>
          )}
        </div>
      )}

      {profile && (
        <div className="space-y-1 rounded border border-amber-500/40 bg-amber-50/70 p-2 text-[10px] dark:bg-amber-950/20">
          <p className="font-medium">選択モデルの llama.cpp プロファイル</p>
          {profile.ui_notice && <p>{profile.ui_notice}</p>}
          <div className="space-y-0.5">
            {profile.gguf_filename && <p>GGUF primary: <code>{profile.gguf_filename}</code></p>}
            {ggufShardCount !== undefined && <p>GGUF shard count: <code>{ggufShardCount}</code></p>}
            {ggufFilenames.length > 0 && (
              <p>GGUF shards: <code>{ggufFilenames.map((filename) => `${filename}${filename === profile.gguf_filename ? " (primary)" : ""}`).join(", ")}</code></p>
            )}
            {profile.gguf_repository_subdir && <p>repository subdir: <code>{profile.gguf_repository_subdir}</code></p>}
            {profile.quantization && <p>量子化: <code>{profile.quantization}</code></p>}
            {profile.runtime_distribution && (
              <p>runtime distribution: <code>{profile.runtime_distribution}</code></p>
            )}
            {profile.source_revision && (
              <p>source revision: <code>{profile.source_revision}</code></p>
            )}
            {profile.served_alias && <p>served alias: <code>{profile.served_alias}</code>{aliasLocked ? "（固定）" : ""}</p>}
            {(profile.source_repository || profile.source_url) && (
              <p>
                source: {profile.source_url
                  ? <a className="underline" href={profile.source_url} target="_blank" rel="noreferrer">{profile.source_repository || profile.source_url}</a>
                  : <code>{profile.source_repository}</code>}
              </p>
            )}
            {profile.minimum_llama_cpp_build !== undefined && (
              <p>minimum llama.cpp build: <code>{profile.minimum_llama_cpp_build}</code></p>
            )}
            {profile.required_llama_cpp_commit && (
              <p>required llama.cpp commit: <code>{profile.required_llama_cpp_commit}</code></p>
            )}
            {profile.reasoning_tools_minimum_llama_cpp_build !== undefined && (
              <p>reasoning/tools minimum build: <code>{profile.reasoning_tools_minimum_llama_cpp_build}</code></p>
            )}
            {profile.native_context_size !== undefined && (
              <p>native context: <code>{profile.native_context_size}</code></p>
            )}
            {profile.default_context_size !== undefined && (
              <p>default context: <code>{profile.default_context_size}</code></p>
            )}
            {profile.default_gpu_layers !== undefined && (
              <p>default GPU layers: <code>{profile.default_gpu_layers}</code></p>
            )}
            {profile.auxiliary_artifacts?.map((artifact) => (
              <p key={`${artifact.id ?? artifact.filename}-auxiliary`}>
                {artifact.kind ?? "auxiliary"}: <code>{artifact.filename}</code>
                {artifact.required ? "（required）" : ""}
                {artifact.installed !== undefined
                  ? ` / ${artifact.installed ? "ready" : "missing"}`
                  : ""}
              </p>
            ))}
            {profile.sampling_defaults && (
              <p>
                sampling: <code>{[
                  profile.sampling_defaults.temperature !== undefined
                    ? `temp=${profile.sampling_defaults.temperature}` : null,
                  profile.sampling_defaults.top_p !== undefined
                    ? `top_p=${profile.sampling_defaults.top_p}` : null,
                  profile.sampling_defaults.top_k !== undefined
                    ? `top_k=${profile.sampling_defaults.top_k}` : null,
                ].filter(Boolean).join(", ")}</code>
              </p>
            )}
            {profile.jinja_required && <p>chat template: <code>Jinja (--jinja)</code></p>}
            {profile.chat_template && <p>chat template: <code>{profile.chat_template}</code></p>}
            {profile.reasoning_format && <p>reasoning format: <code>{profile.reasoning_format}</code></p>}
            {profile.reasoning_parser && <p>reasoning parser: <code>{profile.reasoning_parser}</code></p>}
            {requiredArgs.length > 0 && <p>required args: <code>{requiredArgs.join(" ")}</code></p>}
            {capabilityLabels.length > 0 && <p>capabilities: <code>{capabilityLabels.join(", ")}</code></p>}
            {mtpProfile && (
              <>
                <p>
                  MTP profile: <code>{mtpProfile.mode || "declared"}</code>
                  {mtpProfile.artifact_filename
                    ? <> · artifact: <code>{mtpProfile.artifact_filename}</code></>
                    : mtpProfile.companion_filenames?.length
                      ? <> · artifacts: <code>{mtpProfile.companion_filenames.join(", ")}</code></>
                      : null}
                </p>
                {mtpProfile.minimum_llama_cpp_build !== undefined && (
                  <p>MTP minimum llama.cpp build: <code>{mtpProfile.minimum_llama_cpp_build}</code></p>
                )}
                {mtpProfile.required_llama_cpp_commit && (
                  <p>MTP required llama.cpp commit: <code>{mtpProfile.required_llama_cpp_commit}</code></p>
                )}
                {mtpProfile.embedded_variant?.primary_filename && (
                  <p>MTP embedded variant primary: <code>{mtpProfile.embedded_variant.primary_filename}</code></p>
                )}
              </>
            )}
          </div>
        </div>
      )}

      {mtpDeclared && (
        <div
          role="status"
          aria-label="MTP runtime status"
          className="space-y-1 rounded border border-violet-500/40 bg-violet-50/70 p-2 text-[10px] dark:bg-violet-950/20"
        >
          <p className="font-medium">MTP status: {mtpStatusNotice}</p>
          <p>support: {mtpSupported ? "supported" : "not available for this model"}</p>
          {mtpReason && <p>MTP reason: {mtpReason}</p>}
          {!mtpSupported && mtpEnabled && !mtpReason && (
            <p>MTP artifact is unavailable; the base model will use standard decoding.</p>
          )}
          {mtpVariantModelPath && runtimeMtpMode === "embedded" && (
            <p>MTP variant path: <code>{mtpVariantModelPath}</code></p>
          )}
        </div>
      )}

      <div className="grid gap-3 md:grid-cols-2">
        <div className="space-y-1">
          <Label className="text-xs" htmlFor="llama-cpp-executable">実行ファイル</Label>
          <Input
            id="llama-cpp-executable"
            aria-label="llama.cpp executable"
            value={draft.executable}
            onChange={(event) => update("executable", event.target.value)}
            placeholder={typeof navigator !== "undefined" && navigator.userAgent.includes("Windows") ? "llama-server.exe" : "llama-server"}
            disabled={saving}
          />
          <p className="text-[10px] text-muted-foreground">空欄なら環境変数またはPATHからの自動解決に任せます。</p>
        </div>
        <div className="space-y-1">
          <Label className="text-xs" htmlFor="llama-cpp-model-path">GGUF model path</Label>
          <Input
            id="llama-cpp-model-path"
            aria-label="llama.cpp model path"
            value={draft.model_path}
            onChange={(event) => update("model_path", event.target.value)}
            placeholder={profile?.gguf_filename || "C:\\models\\model.gguf"}
            disabled={saving}
          />
          <p className="text-[10px] text-muted-foreground">
            {managedRuntime
              ? profile
                ? "登録済みmanaged profileです。空欄でもbackendの既存自動検出規則を使います。検出できない場合は外部サーバー扱いにせず、実行不可として保存・起動時に案内します。"
                : "managed llama.cpp runtimeです。GGUF pathを空欄にすると自動起動できないため、指定pathまたは既存の自動検出対象を確認してください。"
              : "GGUF pathを空欄のまま保存すると、起動済みの外部OpenAI互換サーバーとして現在のBase URLを維持します。"}
          </p>
        </div>
        <div className="space-y-1">
          <Label className="text-xs" htmlFor="llama-cpp-model-alias">served model alias</Label>
          <Input
            id="llama-cpp-model-alias"
            aria-label="llama.cpp model alias"
            value={draft.model_alias}
            onChange={(event) => update("model_alias", event.target.value)}
            placeholder={servedAlias || "served-model"}
            readOnly={aliasLocked}
            disabled={saving}
          />
          {aliasLocked && <p className="text-[10px] text-muted-foreground">プロファイルでserved aliasが固定されています。</p>}
        </div>
        <div className="space-y-1">
          <Label className="text-xs" htmlFor="llama-cpp-host">Host</Label>
          <Input
            id="llama-cpp-host"
            aria-label="llama.cpp host"
            value={draft.host}
            onChange={(event) => update("host", event.target.value)}
            placeholder="127.0.0.1"
            disabled={saving}
          />
        </div>
      </div>

      {mtpDeclared && (
        <div className="space-y-2 rounded border border-violet-500/30 bg-violet-50/30 p-2 dark:bg-violet-950/10">
          <label className="flex items-center gap-2 text-xs">
            <Checkbox
              aria-label="MTP / Multi-Token Prediction"
              checked={mtpEnabled}
              onCheckedChange={(checked) => update("mtp_enabled", checked === true)}
              disabled={saving}
            />
            <span>MTP / Multi-Token Prediction</span>
          </label>
          {mtpProfile?.mode === "companion" && (
            <p className="text-[10px] text-muted-foreground">
              互換性が確認された候補だけを既存のモデル保存領域から自動検出します。見つからない場合も、本体は通常の非MTPモードで起動します。
            </p>
          )}
        </div>
      )}

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <div className="space-y-1">
          <Label className="text-xs" htmlFor="llama-cpp-port">Port</Label>
          <Input
            id="llama-cpp-port"
            aria-label="llama.cpp port"
            type="number"
            min={1}
            max={65535}
            step={1}
            value={draft.port}
            onChange={(event) => update("port", event.target.value)}
            disabled={saving}
          />
        </div>
        <div className="space-y-1">
          <Label className="text-xs" htmlFor="llama-cpp-context-size">Context size</Label>
          <Input
            id="llama-cpp-context-size"
            aria-label="llama.cpp context size"
            type="number"
            min={1}
            step={1}
            value={draft.context_size}
            onChange={(event) => update("context_size", event.target.value)}
            disabled={saving}
          />
        </div>
        <div className="space-y-1">
          <Label className="text-xs" htmlFor="llama-cpp-gpu-layers">GPU layers</Label>
          <Input
            id="llama-cpp-gpu-layers"
            aria-label="llama.cpp GPU layers"
            type="text"
            placeholder={String(profile?.default_gpu_layers ?? "999")}
            value={draft.gpu_layers}
            onChange={(event) => update("gpu_layers", event.target.value)}
            disabled={saving}
          />
        </div>
        <div className="space-y-1">
          <Label className="text-xs" htmlFor="llama-cpp-readiness-timeout">Readiness timeout (seconds)</Label>
          <Input
            id="llama-cpp-readiness-timeout"
            aria-label="llama.cpp readiness timeout seconds"
            type="number"
            min={0.1}
            step={0.1}
            value={draft.readiness_timeout_seconds}
            onChange={(event) => update("readiness_timeout_seconds", event.target.value)}
            disabled={saving}
          />
        </div>
      </div>

      <div className="space-y-1">
        <Label className="text-xs" htmlFor="llama-cpp-extra-args">追加引数（1行1引数）</Label>
        <Textarea
          id="llama-cpp-extra-args"
          aria-label="llama.cpp extra args"
          rows={3}
          value={draft.extra_args}
          onChange={(event) => update("extra_args", event.target.value)}
          placeholder={'--flash-attn\non'}
          disabled={saving}
          className="min-h-16 text-xs"
        />
        <p className="text-[10px] text-muted-foreground">
          空行を除き、各行をそのままargvの1要素として送信します。シェル展開やコマンド連結は行いません。
        </p>
        {reservedExtraArg && (
          <p role="alert" className="text-[10px] text-destructive">
            {reservedExtraArg} はruntimeが管理する引数のため指定できません。Host/port等の上の項目を使ってください。
          </p>
        )}
      </div>

      <label className="flex items-center gap-2 text-xs">
        <Checkbox
          aria-label="llama.cpp自動起動"
          checked={draft.auto_start}
          onCheckedChange={(checked) => update("auto_start", checked === true)}
          disabled={saving}
        />
        <span>モデル選択時に llama-server を自動起動する</span>
      </label>

      {error && (
        <p role="alert" className="rounded border border-destructive/40 bg-destructive/5 p-2 text-xs text-destructive">
          {error}
        </p>
      )}
      <div className="flex flex-wrap items-center gap-3">
        <Button
          size="sm"
          variant="outline"
          onClick={onSave}
          disabled={saving || !selectedModelId.trim()}
        >
          llama.cpp設定を保存
        </Button>
        <span className="text-[10px] text-muted-foreground">
          Base URL / API key は既存の接続設定から変更できます。
        </span>
      </div>
    </section>
  );
}
