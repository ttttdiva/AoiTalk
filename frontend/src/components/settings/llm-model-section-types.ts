import { formatBytes } from "@/lib/utils";
import type { LlmDeploymentMetadata } from "@/lib/llm-provider-visibility";
export type { LlmDeploymentMetadata } from "@/lib/llm-provider-visibility";

export interface LlmModelOption {
  id: string;
  label: string;
  description?: string;
  installed?: boolean;
  source?: string;
  source_label?: string;
  base_url?: string;
  server?: string;
  server_label?: string;
  size?: number;
  details?: {
    parameter_size?: string;
    quantization_level?: string;
    family?: string;
    runtime?: string;
  };
  runtime?: string;
  /** Canonical model-specific runtime contract returned by the backend. */
  runtime_profile?: LlamaCppRuntimeProfile | null;
  /** Shallow MTP projection retained by newer catalog payloads. */
  mtp?: LlamaCppMtpProfile;
  context_length?: number;
  reasoning_effort_options?: string[];
  reasoning_effort_default?: string;
  reasoning_effort_supports_disable?: boolean;
  reasoning_effort_wire?: { transport?: string; path?: string };
  custom_current?: boolean;
  media?: { image?: boolean; audio?: boolean };
  selection_kind?: "static" | "routing_profile";
  routing_profile_id?: string;
}

/**
 * Model metadata consumed by the generic llama.cpp settings panel.
 *
 * This is deliberately data-only: adding a GGUF model should require a
 * catalog profile rather than a model-id branch in the UI.  Fields remain
 * optional for compatibility with older catalog snapshots and external
 * providers which do not expose a llama.cpp profile.
 */
export interface LlamaCppRuntimeProfile {
  profile_id?: string;
  runtime?: string;
  served_alias?: string;
  alias_locked?: boolean;
  source_repository?: string;
  source_url?: string;
  gguf_filename?: string;
  quantization?: string;
  native_context_size?: number | string;
  default_context_size?: number | string;
  minimum_llama_cpp_build?: number | string;
  reasoning_tools_minimum_llama_cpp_build?: number | string;
  required_args?: string[];
  jinja_required?: boolean;
  chat_template?: string;
  reasoning_format?: string;
  reasoning_parser?: string;
  reasoning_effort_options?: string[];
  reasoning_effort_default?: string;
  reasoning_effort_supports_disable?: boolean;
  reasoning_effort_wire?: { transport?: string; path?: string };
  /** Multi-token prediction metadata owned by the model profile. */
  mtp?: LlamaCppMtpProfile;
  capabilities?: {
    reasoning?: boolean;
    tools?: boolean;
    media?: {
      image?: boolean;
      audio?: boolean;
    };
  };
  ui_notice?: string;
}

/**
 * Data-driven MTP contract for a managed llama.cpp profile.
 *
 * `mode: companion` means llama-server needs a separate draft artifact;
 * `embedded` means the selected model contains the MTP data itself.  The
 * optional aliases keep the frontend tolerant of older catalog snapshots
 * while the canonical fields remain explicit and typed.
 */
export interface LlamaCppMtpProfile {
  supported?: boolean;
  default_enabled?: boolean;
  mode?: "embedded" | "companion" | string;
  artifact_filename?: string;
  companion_filenames?: string[];
  artifact_user_configurable?: boolean;
  artifact_required?: boolean;
  /** Legacy/provider projection aliases for the same companion path rule. */
  user_configurable?: boolean;
  required?: boolean;
  compatibility?: string;
  reason?: string;
  ui_notice?: string;
}

function recordValue(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

/**
 * Normalize the canonical profile while tolerating one older catalog shape
 * that exposed profile keys under `details`/provider settings.  This is a
 * field-name compatibility adapter, not model-specific logic.
 */
export function llamaCppRuntimeProfileFromValue(value: unknown): LlamaCppRuntimeProfile | null {
  const raw = recordValue(value);
  if (!raw) return null;
  const rawCapabilities = recordValue(raw.capabilities);
  const capabilities = {
    ...(rawCapabilities ?? {}),
    reasoning: rawCapabilities?.reasoning ?? raw.supports_reasoning,
    tools: rawCapabilities?.tools ?? raw.supports_tools,
    media: recordValue(rawCapabilities?.media)
      ?? recordValue(raw.media)
      ?? {
        image: raw.supports_image,
        audio: raw.supports_audio,
      },
  };
  const requiredArgs = Array.isArray(raw.required_args)
    ? raw.required_args.map((item) => String(item))
    : Array.isArray(raw.default_args)
      ? raw.default_args.map((item) => String(item))
      : undefined;
  const scalar = (item: unknown): string | number | undefined =>
    typeof item === "number" || typeof item === "string" ? item : undefined;
  const rawMtp = recordValue(raw.mtp);
  const mtp = rawMtp
    ? {
      ...(rawMtp as LlamaCppMtpProfile),
      supported: rawMtp.supported === undefined
        ? undefined
        : Boolean(rawMtp.supported),
      default_enabled: rawMtp.default_enabled === undefined
        ? undefined
        : Boolean(rawMtp.default_enabled),
      mode: rawMtp.mode === undefined ? undefined : String(rawMtp.mode),
      artifact_filename: rawMtp.artifact_filename === undefined
        ? undefined
        : String(rawMtp.artifact_filename),
      companion_filenames: Array.isArray(rawMtp.companion_filenames)
        ? rawMtp.companion_filenames.map((item) => String(item))
        : undefined,
      artifact_user_configurable: rawMtp.artifact_user_configurable === undefined
        ? (rawMtp.user_configurable === undefined ? undefined : Boolean(rawMtp.user_configurable))
        : Boolean(rawMtp.artifact_user_configurable),
      artifact_required: rawMtp.artifact_required === undefined
        ? (rawMtp.required === undefined ? undefined : Boolean(rawMtp.required))
        : Boolean(rawMtp.artifact_required),
      user_configurable: rawMtp.user_configurable === undefined
        ? undefined
        : Boolean(rawMtp.user_configurable),
      required: rawMtp.required === undefined ? undefined : Boolean(rawMtp.required),
      compatibility: rawMtp.compatibility === undefined ? undefined : String(rawMtp.compatibility),
      reason: rawMtp.reason === undefined ? undefined : String(rawMtp.reason),
      ui_notice: rawMtp.ui_notice === undefined ? undefined : String(rawMtp.ui_notice),
    } satisfies LlamaCppMtpProfile
    : undefined;
  const profile: LlamaCppRuntimeProfile = {
    ...(raw as LlamaCppRuntimeProfile),
    runtime: raw.runtime === undefined ? undefined : String(raw.runtime),
    profile_id: raw.profile_id === undefined && raw.id === undefined
      ? undefined
      : String(raw.profile_id ?? raw.id),
    served_alias: raw.served_alias === undefined ? undefined : String(raw.served_alias),
    alias_locked: raw.alias_locked === undefined ? undefined : Boolean(raw.alias_locked),
    source_repository: raw.source_repository === undefined
      ? (raw.repository === undefined ? undefined : String(raw.repository))
      : String(raw.source_repository),
    source_url: raw.source_url === undefined
      ? (raw.huggingface_repository === undefined ? undefined : String(raw.huggingface_repository))
      : String(raw.source_url),
    gguf_filename: raw.gguf_filename === undefined
      ? (raw.filename ?? raw.model_filename ?? raw.official_filename) === undefined
        ? undefined
        : String(raw.filename ?? raw.model_filename ?? raw.official_filename)
      : String(raw.gguf_filename),
    quantization: raw.quantization === undefined ? undefined : String(raw.quantization),
    native_context_size: scalar(raw.native_context_size ?? raw.native_context_length),
    default_context_size: scalar(raw.default_context_size),
    minimum_llama_cpp_build: scalar(raw.minimum_llama_cpp_build),
    reasoning_tools_minimum_llama_cpp_build: scalar(raw.reasoning_tools_minimum_llama_cpp_build),
    required_args: requiredArgs,
    jinja_required: raw.jinja_required === undefined ? undefined : Boolean(raw.jinja_required),
    chat_template: raw.chat_template === undefined ? undefined : String(raw.chat_template),
    reasoning_format: raw.reasoning_format === undefined ? undefined : String(raw.reasoning_format),
    reasoning_parser: raw.reasoning_parser === undefined ? undefined : String(raw.reasoning_parser),
    reasoning_effort_options: Array.isArray(raw.reasoning_effort_options)
      ? raw.reasoning_effort_options.map((item) => String(item))
      : undefined,
    reasoning_effort_default: raw.reasoning_effort_default === undefined
      ? undefined
      : String(raw.reasoning_effort_default),
    reasoning_effort_supports_disable: raw.reasoning_effort_supports_disable === undefined
      ? undefined
      : Boolean(raw.reasoning_effort_supports_disable),
    reasoning_effort_wire: recordValue(raw.reasoning_effort_wire) as LlamaCppRuntimeProfile["reasoning_effort_wire"],
    mtp,
    capabilities: capabilities as LlamaCppRuntimeProfile["capabilities"],
    ui_notice: raw.ui_notice === undefined ? undefined : String(raw.ui_notice),
  };
  return profile;
}

/** Resolve the model profile from canonical or legacy catalog locations. */
export function llamaCppRuntimeProfileForModel(
  option?: LlmModelOption | null,
  settings?: LlamaCppRuntimeSettings | null,
  modelId?: string,
): LlamaCppRuntimeProfile | null {
  const targetModel = String(modelId ?? option?.id ?? "").trim().toLowerCase();
  const optionProfile = llamaCppRuntimeProfileFromValue(
    option?.runtime_profile
      ?? (option?.mtp
        ? { profile_id: option.id, runtime: option.runtime, mtp: option.mtp }
        : undefined),
  );
  if (optionProfile) {
    const optionIdentities = [optionProfile.profile_id, optionProfile.served_alias]
      .map((item) => String(item ?? "").trim().toLowerCase())
      .filter(Boolean);
    // Canonical catalog entries normally carry an ID/served alias.  If one is
    // present, reject a stale profile accidentally attached to another option;
    // metadata-less legacy profiles remain usable for their own option.
    if (!targetModel || optionIdentities.length === 0 || optionIdentities.includes(targetModel)) {
      return optionProfile;
    }
    return null;
  }

  // An explicit non-llama runtime marker on the selected option is authoritative.
  // Do not let a provider-level llama.cpp object from the previous selection
  // leak into an external/static model which has no own runtime profile.
  const optionRuntime = String(
    option?.runtime ?? option?.details?.runtime ?? "",
  ).trim().toLowerCase().replace(".", "_");
  if (optionRuntime && optionRuntime !== "llama_cpp") return null;

  const matchesTarget = (
    profile: LlamaCppRuntimeProfile | null,
    raw?: Record<string, unknown> | null,
  ): boolean => {
    if (!profile || !targetModel) return false;
    const profileIds = [profile.profile_id, profile.served_alias]
      .map((item) => String(item ?? "").trim().toLowerCase())
      .filter(Boolean);
    // A declared profile identity is authoritative.  A stale user-editable
    // model_alias must not make a different persisted profile appear to match
    // the newly selected model.
    if (profileIds.length > 0) return profileIds.includes(targetModel);
    const rawAliases = [raw?.model_alias, raw?.served_alias]
      .map((item) => String(item ?? "").trim().toLowerCase())
      .filter(Boolean);
    return rawAliases.includes(targetModel);
  };

  const settingsRaw = recordValue(settings);
  const settingsProfile = llamaCppRuntimeProfileFromValue(
    settings?.runtime_profile ?? settings,
  );
  if (matchesTarget(settingsProfile, settingsRaw)) return settingsProfile;
  const settingsHasPersistedProfile = settings?.runtime_profile !== undefined
    && settings?.runtime_profile !== null;
  const optionAllowsAnonymousRuntime = !option
    || option.custom_current === true
    || option.source === "provider-configured";
  const settingsRuntime = String(
    settings?.runtime ?? settings?.server_profile ?? "",
  ).trim().toLowerCase().replace(".", "_");
  if (
    !settingsHasPersistedProfile
      && settingsRuntime === "llama_cpp"
      && settings?.model_path
      // `local-model` is the long-standing external OpenAI-compatible
      // sentinel.  Its provider settings may contain a stale managed runtime
      // object, but it must stay Base-URL-only in the UI and save payload.
      && targetModel !== "local-model"
      // Static/service-discovered options without a profile are not proof of
      // a managed llama.cpp runtime.  Anonymous settings are only accepted
      // for an explicit custom/provider-configured selection (or no option,
      // which represents a free-form model ID).
      && optionAllowsAnonymousRuntime
  ) {
    return settingsProfile;
  }

  const details = recordValue(option?.details);
  const legacyDetails = details && (
    "runtime" in details
      || "served_alias" in details
      || "filename" in details
      || "model_filename" in details
  )
    ? details
    : null;
  const detailsProfile = llamaCppRuntimeProfileFromValue(legacyDetails);
  return matchesTarget(detailsProfile, legacyDetails) ? detailsProfile : null;
}

export interface LlmProviderCatalog {
  id: string;
  label: string;
  /** Backend-marked availability; omitted for personal/legacy responses. */
  available?: boolean;
  disabled?: boolean;
  unavailable?: boolean;
  availability_reason?: string | null;
  models: LlmModelOption[];
  configured_model?: string;
  supports_custom_model: boolean;
  capabilities?: {
    supports_stream?: boolean;
    supports_tools?: boolean;
    supports_response_format?: boolean;
    supports_model_pull?: boolean;
    supports_model_delete?: boolean;
    supports_extra_body?: boolean;
  };
  settings?: {
    base_url?: string;
    api_key_configured?: boolean;
    api_key_placeholder?: string;
    reasoning_effort?: string;
    reasoning_effort_options?: string[];
    reasoning_effort_default?: string;
    reasoning_effort_supports_disable?: boolean;
    reasoning_effort_wire?: { transport?: string; path?: string };
    /** Generic llama.cpp/llama-server settings for the local provider. */
    llama_cpp?: LlamaCppRuntimeSettings;
    /** Read-only alias retained by older catalog payloads. */
    runtime_settings?: LlamaCppRuntimeSettings;
    /** Provider-level runtime profile for legacy/current catalog payloads. */
    runtime_profile?: LlamaCppRuntimeProfile | null;
    /** Active MTP projection; canonical inputs remain under llama_cpp. */
    mtp?: LlamaCppMtpProfile | null;
    mtp_enabled?: boolean | string | number | null;
    mtp_model_path?: string | null;
    mtp_supported?: boolean | string | number | null;
    mtp_available?: boolean | string | number | null;
    mtp_status?: string | null;
    mtp_reason?: string | null;
    mtp_artifact_path?: string | null;
    mtp_resolved_model_path?: string | null;
    mtp_mode?: string | null;
  };
  source: string;
  refreshed?: boolean;
  cached_at?: string | null;
  error?: string | null;
  selection_kind?: "static" | "routing_profile";
}

/**
 * The backend exposes these values under provider.settings.llama_cpp.  Keep
 * the metadata fields optional because the catalog also returns this object
 * as a descriptive runtime profile and older responses may omit some keys.
 */
export interface LlamaCppRuntimeSettings {
  runtime?: string;
  server_profile?: string;
  runtime_state?: "ready" | "manual" | "missing_model_path" | "model_path_not_found" | "executable_not_found" | "external" | "unmanaged" | string;
  runtime_error?: string | null;
  model_path_source?: "configured" | "environment" | "discovered" | "missing" | string;
  model_path_status?: "ok" | "missing" | "not_found" | "not_applicable" | string;
  executable_status?: "ok" | "not_found" | "not_checked" | "not_required" | "not_applicable" | string;
  minimum_build?: number | string | null;
  base_url?: string;
  executable?: string;
  model_path?: string;
  model_alias?: string;
  host?: string;
  port?: number | string;
  context_size?: number | string;
  gpu_layers?: number | string;
  extra_args?: Array<string | number> | string;
  auto_start?: boolean | string | number;
  readiness_timeout?: number | string;
  readiness_timeout_seconds?: number | string;
  reasoning_effort?: string | null;
  reasoning_effort_options?: string[];
  reasoning_effort_default?: string | null;
  reasoning_effort_supports_disable?: boolean;
  reasoning_effort_wire?: { transport?: string; path?: string } | null;
  model_filename?: string;
  minimum_llama_cpp_build?: number | string;
  runtime_profile?: LlamaCppRuntimeProfile | null;
  mtp?: LlamaCppMtpProfile | null;
  /** User inputs for the profile-owned MTP launcher contract. */
  mtp_enabled?: boolean | string | number | null;
  mtp_model_path?: string | null;
  /** Runtime projection; these do not make the base runtime unavailable. */
  mtp_supported?: boolean | string | number | null;
  mtp_available?: boolean | string | number | null;
  mtp_status?: string | null;
  mtp_reason?: string | null;
  mtp_artifact_path?: string | null;
  mtp_resolved_model_path?: string | null;
  mtp_mode?: string | null;
}

/** Controlled form values. Numeric fields stay strings while editing so an
 * empty value can be represented; they are parsed and validated on save. */
export type LlamaCppSettingsDraft = {
  executable: string;
  model_path: string;
  model_alias: string;
  host: string;
  port: string;
  context_size: string;
  gpu_layers: string;
  extra_args: string;
  auto_start: boolean;
  readiness_timeout_seconds: string;
  /** Profile-driven MTP toggle.  Absent on legacy non-MTP drafts. */
  mtp_enabled?: boolean;
};

export const DEFAULT_LLAMA_CPP_SETTINGS_DRAFT: LlamaCppSettingsDraft = {
  executable: "",
  model_path: "",
  model_alias: "",
  host: "127.0.0.1",
  port: "8080",
  context_size: "131072",
  gpu_layers: "999",
  extra_args: "",
  auto_start: true,
  readiness_timeout_seconds: "180",
  mtp_enabled: false,
};

function formNumber(value: unknown, fallback: string): string {
  if (value === undefined || value === null || String(value).trim() === "") return fallback;
  return String(value);
}

function formBoolean(value: unknown, fallback: boolean): boolean {
  if (value === undefined || value === null || value === "") return fallback;
  if (typeof value === "boolean") return value;
  if (typeof value === "number") return value !== 0;
  return ["1", "true", "yes", "on"].includes(String(value).trim().toLowerCase());
}

/** Return the profile's default MTP state without enabling legacy profiles. */
export function llamaCppMtpDefaultEnabled(
  profile?: LlamaCppRuntimeProfile | null,
): boolean {
  // A profile may intentionally default the preference to ON while marking
  // the current artifact as unavailable (for example a NO-NEXTN variant).
  // The runtime then falls back to normal decoding without treating the base
  // model as unavailable.
  return profile?.mtp?.default_enabled === true;
}

/** Convert the backend catalog object into safe controlled input values. */
export function llamaCppDraftFromSettings(
  settings?: LlamaCppRuntimeSettings | null,
  modelId?: string,
  runtimeProfile?: LlamaCppRuntimeProfile | null,
): LlamaCppSettingsDraft {
  const source = settings ?? {};
  const profile = runtimeProfile
    ?? source.runtime_profile
    ?? (source.mtp
      ? { runtime: source.runtime, mtp: source.mtp }
      : null);
  const selectedProfile = llamaCppRuntimeProfileFromValue(profile);
  const persistedProfile = llamaCppRuntimeProfileFromValue(source.runtime_profile);
  const selectedIdentity = String(
    selectedProfile?.profile_id ?? selectedProfile?.served_alias ?? "",
  ).trim().toLowerCase();
  const persistedIdentity = String(
    persistedProfile?.profile_id ?? persistedProfile?.served_alias ?? "",
  ).trim().toLowerCase();
  const selectedAlias = String(selectedProfile?.served_alias ?? "").trim().toLowerCase();
  const persistedAlias = String(source.model_alias ?? "").trim().toLowerCase();
  const selectedFilename = String(selectedProfile?.gguf_filename ?? "").trim().toLowerCase();
  const persistedFilename = String(source.model_path ?? "")
    .split(/[\\/]/)
    .pop()
    ?.trim()
    .toLowerCase() ?? "";
  const selectedModelIdentity = String(modelId ?? "").trim().toLowerCase();
  const profileMismatch = Boolean(
    selectedIdentity
      ? ((persistedIdentity && persistedIdentity !== selectedIdentity)
        || (!persistedIdentity && selectedAlias && persistedAlias && persistedAlias !== selectedAlias)
        || (!persistedIdentity && selectedFilename && persistedFilename && persistedFilename !== selectedFilename))
      : (persistedIdentity && selectedModelIdentity && persistedIdentity !== selectedModelIdentity),
  );
  // A provider-level runtime object is shared across model selections in old
  // catalog payloads.  Never carry profile-owned values (GGUF path, alias,
  // context, or extra args) from a different profile into the new model.
  const effectiveSource: LlamaCppRuntimeSettings = profileMismatch
    ? {
      ...source,
      model_path: undefined,
      model_alias: undefined,
      context_size: undefined,
      extra_args: undefined,
      runtime_profile: undefined,
      mtp_enabled: undefined,
      mtp_artifact_path: undefined,
    }
    : source;
  const timeout = effectiveSource.readiness_timeout_seconds ?? effectiveSource.readiness_timeout;
  const profileRequiredArgs = new Set(
    (selectedProfile?.required_args ?? []).map((item) => String(item).trim()),
  );
  const extraArgValues = Array.isArray(effectiveSource.extra_args)
    ? effectiveSource.extra_args.map((item) => String(item))
    : String(effectiveSource.extra_args ?? "").split(/\r?\n/);
  // Required profile args are runtime-owned (not user overrides).  Keep them
  // visible in the metadata notice while omitting them from editable extras;
  // otherwise a required `--jinja` would trip the reserved-flag validator.
  const extraArgs = extraArgValues
    .filter((item) => !profileRequiredArgs.has(item.trim()))
    .join("\n");
  const modelAlias = String(
    effectiveSource.model_alias
      ?? selectedProfile?.served_alias
      ?? (modelId?.trim() || DEFAULT_LLAMA_CPP_SETTINGS_DRAFT.model_alias),
  );
  const profileContext = selectedProfile?.default_context_size ?? selectedProfile?.native_context_size;
  const draft: LlamaCppSettingsDraft = {
    executable: String(effectiveSource.executable ?? DEFAULT_LLAMA_CPP_SETTINGS_DRAFT.executable),
    // The profile filename is guidance/placeholder metadata, not a path in
    // the user's filesystem.  Never persist it as an automatic model path.
    model_path: String(effectiveSource.model_path ?? DEFAULT_LLAMA_CPP_SETTINGS_DRAFT.model_path),
    model_alias: modelAlias,
    host: String(effectiveSource.host ?? DEFAULT_LLAMA_CPP_SETTINGS_DRAFT.host),
    port: formNumber(effectiveSource.port, DEFAULT_LLAMA_CPP_SETTINGS_DRAFT.port),
    context_size: formNumber(
      effectiveSource.context_size ?? profileContext,
      DEFAULT_LLAMA_CPP_SETTINGS_DRAFT.context_size,
    ),
    gpu_layers: formNumber(effectiveSource.gpu_layers, DEFAULT_LLAMA_CPP_SETTINGS_DRAFT.gpu_layers),
    extra_args: extraArgs,
    auto_start: effectiveSource.auto_start === undefined
      ? DEFAULT_LLAMA_CPP_SETTINGS_DRAFT.auto_start
      : effectiveSource.auto_start === true
        || effectiveSource.auto_start === 1
        || (typeof effectiveSource.auto_start === "string"
          && ["1", "true", "yes", "on"].includes(effectiveSource.auto_start.trim().toLowerCase())),
    readiness_timeout_seconds: formNumber(
      timeout,
      DEFAULT_LLAMA_CPP_SETTINGS_DRAFT.readiness_timeout_seconds,
    ),
    mtp_enabled: selectedProfile?.mtp
      ? formBoolean(
        effectiveSource.mtp_enabled,
        llamaCppMtpDefaultEnabled(selectedProfile),
      )
      : false,
  };
  return draft;
}

export type LlamaCppSettingsPayload = {
  executable: string;
  model_path: string;
  model_alias: string;
  host: string;
  port: number;
  context_size: number;
  gpu_layers: number;
  extra_args: string[];
  auto_start: boolean;
  /** Canonical backend key; the catalog also returns *_seconds for display. */
  readiness_timeout: number;
  /** Profile-owned speculative decoding controls. */
  mtp_enabled?: boolean;
};

/** Resolve the canonical OpenAI-compatible URL owned by llama.cpp host/port. */
export function llamaCppBaseUrlFromPayload(payload: Pick<LlamaCppSettingsPayload, "host" | "port">): string {
  let host = payload.host.trim() || "127.0.0.1";
  // Wildcard bind addresses are not usable by the client/readiness probe;
  // mirror the backend's loopback normalization for the persisted base URL.
  if (["0.0.0.0", "::", "[::]", "::0"].includes(host)) host = "127.0.0.1";
  const urlHost = host.includes(":") && !host.startsWith("[") ? `[${host}]` : host;
  return `http://${urlHost}:${payload.port}/v1`;
}

const LLAMA_CPP_RESERVED_EXTRA_FLAGS = new Set([
  "--model",
  "-m",
  "--alias",
  "-a",
  "--host",
  "--port",
  "--ctx-size",
  "-c",
  "--n-ctx",
  "--n-gpu-layers",
  "-ngl",
  "--jinja",
  "--no-jinja",
  "--spec-type",
  "--spec-draft-model",
  "--model-draft",
  "-md",
  "--spec-draft-hf",
  "--spec-draft-n-max",
]);

/** Return the runtime-owned flag that must not be overridden by extra_args. */
export function reservedLlamaCppExtraArg(value: string): string | null {
  const token = value.trim();
  if (!token) return null;
  const flag = token.split(/\s+/, 1)[0].split("=", 1)[0].toLowerCase();
  return LLAMA_CPP_RESERVED_EXTRA_FLAGS.has(flag) ? flag : null;
}

/** Existing local profiles own their launcher; do not expose generic llama.cpp controls for them. */
export function shouldShowLlamaCppRuntimePanel(
  modelId: string,
  option?: LlmModelOption | null,
  settings?: LlamaCppRuntimeSettings | null,
): boolean {
  if (!modelId.trim()) return false;
  const profile = llamaCppRuntimeProfileForModel(option, settings, modelId);
  const profileRuntime = String(
    profile?.runtime
      ?? "",
  ).trim().toLowerCase().replace(".", "_");
  const legacyRuntime = String(
    option?.runtime
      ?? option?.details?.runtime
      ?? "",
  ).trim().toLowerCase().replace(".", "_");
  if (profileRuntime && profileRuntime !== "llama_cpp") return false;
  if (legacyRuntime && legacyRuntime !== "llama_cpp") return false;
  if (profileRuntime === "llama_cpp" || legacyRuntime === "llama_cpp") return true;
  // A persisted model path/alias is the legacy managed-runtime marker.  Do
  // not infer a runtime from model IDs or from an external Base URL alone.
  // Provider-level settings are accepted only after their profile identity
  // matched the selected model in llamaCppRuntimeProfileForModel above.
  return Boolean(
    profile
      && settings
      && (settings.model_path || settings.model_alias)
      && !option?.runtime_profile,
  );
}

function parseInteger(value: string, label: string, allowNegative = false): number {
  const normalized = value.trim();
  if (!/^-?\d+$/.test(normalized)) {
    throw new Error(`${label} は整数で指定してください`);
  }
  const parsed = Number(normalized);
  if (!Number.isSafeInteger(parsed) || (!allowNegative && parsed < 0)) {
    throw new Error(`${label} の値が範囲外です`);
  }
  return parsed;
}

function parsePositiveInteger(value: string, label: string): number {
  const parsed = parseInteger(value, label);
  if (parsed <= 0) throw new Error(`${label} は1以上で指定してください`);
  return parsed;
}

/**
 * Parse a draft without invoking a shell parser. Each non-empty line is one
 * argv element, so arguments containing spaces remain a single predictable
 * value and cannot accidentally execute shell syntax.
 */
export function llamaCppPayloadFromDraft(
  draft: LlamaCppSettingsDraft,
): LlamaCppSettingsPayload {
  const port = parsePositiveInteger(draft.port, "llama.cpp port");
  if (port > 65535) throw new Error("llama.cpp port は65535以下で指定してください");
  const contextSize = parsePositiveInteger(draft.context_size, "context size");
  const gpuLayers = parseInteger(draft.gpu_layers, "GPU layers", true);
  const timeoutText = draft.readiness_timeout_seconds.trim();
  if (!/^(?:\d+\.?\d*|\.\d+)$/.test(timeoutText)) {
    throw new Error("readiness timeout は正数で指定してください");
  }
  const timeout = Number(timeoutText);
  if (!Number.isFinite(timeout) || timeout <= 0) {
    throw new Error("readiness timeout は正数で指定してください");
  }
  const extraArgs = draft.extra_args
    .split(/\r?\n/)
    .map((item) => item.trim())
    .filter(Boolean);
  const reserved = extraArgs.map(reservedLlamaCppExtraArg).find(Boolean);
  if (reserved) {
    throw new Error(`${reserved} はllama.cppが管理するため追加引数に指定できません`);
  }
  const payload: LlamaCppSettingsPayload = {
    executable: draft.executable.trim(),
    model_path: draft.model_path.trim(),
    model_alias: draft.model_alias.trim(),
    host: draft.host.trim() || "127.0.0.1",
    port,
    context_size: contextSize,
    gpu_layers: gpuLayers,
    extra_args: extraArgs,
    auto_start: draft.auto_start,
    readiness_timeout: timeout,
  };
  // Keep legacy callers/source snapshots byte-for-byte compatible when they
  // do not carry the new optional draft fields.  The controlled UI draft
  // always has mtp_enabled, so an explicit OFF round-trips as false.
  if (draft.mtp_enabled !== undefined) payload.mtp_enabled = draft.mtp_enabled;
  return payload;
}

export interface LlmModelCatalogResponse {
  current: {
    provider: string;
    model: string;
  };
  providers: LlmProviderCatalog[];
  deployment?: LlmDeploymentMetadata | null;
}

export interface LlmEngineResponse {
  success?: boolean;
  provider: string;
  model: string;
  deployment?: LlmDeploymentMetadata | null;
  message?: string;
}

export type SpeechRecognitionSettings = {
  current_engine?: string;
  engines?: Record<string, { model?: string }>;
};

export interface SettingsPayload {
  settings?: {
    agent_team?: {
      schema_version?: number;
      orchestration_mode?: "standard" | "director";
      delegation_enabled?: boolean;
      teams?: Record<string, AgentTeamTeam>;
      subagents?: Record<string, AgentTeamSubagent>;
    };
    chatgpt_web?: {
      profile_dir?: string;
      response_timeout_seconds?: number;
      max_rounds_per_turn?: number;
    };
    model_routing?: ModelRoutingSettings;
    mage_vl?: MageVLSettings;
    speech_recognition?: SpeechRecognitionSettings;
    external_model_privacy?: ExternalModelPrivacySettings;
  };
}

export interface ExternalModelPrivacySettings {
  mode?: "direct" | "protected" | "local_only";
  review_policy?: "never" | "high_risk" | "always";
  notify?: boolean;
  semantic_redaction_enabled?: boolean;
  local_provider?: "ollama" | "sglang" | "openai_compatible_local" | string;
  local_model?: string;
  redaction_terms?: string[];
  trusted_local_hosts?: string[];
  raw_media_policy?: "block" | "confirm";
  cache_enabled?: boolean;
}

export interface ModelRoutingSettings {
  classes?: {
    heavy?: ModelRouteSettings;
    light?: ModelRouteSettings;
    vision?: ModelRouteSettings & { base_url?: string; api_key?: string };
    audio?: ModelRouteSettings & {
      engine?: "speech_recognition" | "llm" | "off";
      base_url?: string;
      api_key?: string;
    };
    clip_ingest?: ModelRouteSettings & { base_url?: string; api_key?: string };
    video?: ModelRouteSettings & { base_url?: string; api_key?: string };
  };
  media?: {
    image_mode?: "auto" | "always" | "off";
    video_mode?: "auto" | "off";
  };
  overrides?: Record<string, ModelRouteSettings>;
}

export interface MageVLSettings {
  enabled?: boolean;
  managed?: boolean;
  preload_on_start?: boolean;
  model?: string;
  base_url?: string;
  api_key?: string;
  api_key_configured?: boolean;
  server_command?: string;
  startup_timeout_seconds?: number;
  max_video_bytes?: number;
  max_video_duration_seconds?: number;
  video_backend?: "frames";
  codec_engine?: "traditional" | "neural";
  num_frames?: number;
  max_pixels?: number;
  max_new_tokens?: number;
  state?: {
    state?: string;
    error?: string;
    load_wait_ms?: number;
    owned_process?: boolean;
    pid?: number | null;
  };
}

export interface ModelRouteSettings {
  key?: string;
  member_key?: string;
  enabled?: boolean;
  provider?: string;
  model?: string;
  mode?: string;
  reasoning_effort?: string;
  external?: boolean;
  label?: string;
  role?: string;
  runner?: string;
  inherit?: boolean;
  scalable?: boolean;
  default_instances?: number;
  max_instances?: number;
  tools?: string[];
  group_id?: string;
  effective_provider?: string;
  effective_model?: string;
  effective_effort?: string;
  effort_policy?: EffortPolicy;
  effort?: string;
}

export type EffortPolicy = "inherit" | "same" | "lower" | "explicit" | "default";

export function effortPolicyLabel(policy: EffortPolicy): string {
  return policy === "inherit"
    ? "グループを継承"
    : policy === "same"
    ? "メインと同じ"
    : policy === "lower"
      ? "メインより1段階低い"
      : policy === "explicit"
        ? "明示指定"
        : "モデル既定";
}

export type AgentTeamActivationMode = "always" | "contextual" | "manual";

export interface AgentTeamActivation {
  mode: AgentTeamActivationMode;
  contexts: string[];
}

export type ExecutionRouteEffortPolicy = "same" | "lower" | "explicit" | "default";

export interface ExecutionRoute {
  inherit_model: boolean;
  provider: string;
  model: string;
  effort_policy: ExecutionRouteEffortPolicy;
  effort: string;
}

export interface TeamExecutionProfile {
  profile_id: string;
  name: string;
  enabled: boolean;
  default_route: ExecutionRoute;
  overrides: Record<string, ExecutionRoute>;
}

export interface AgentTeamTeam {
  team_id: string;
  name: string;
  description: string;
  enabled: boolean;
  sort_order: number;
  activation: AgentTeamActivation;
  subagent_ids: string[];
  execution_profiles: Record<string, TeamExecutionProfile>;
}

export interface AgentTeamSubagent {
  subagent_id: string;
  name: string;
  description: string;
  instructions: string;
  enabled: boolean;
  capability_ids: string[];
  scalable: boolean;
  default_instances: number;
  max_instances: number;
  max_workspace_access: "none" | "read" | "write";
  allow_cli_native_tools: boolean;
}

/** Canonical persisted Agent Team schema.  GET-only metadata is deliberately
 * represented by AgentTeamConfigEnvelope rather than this type. */
export interface AgentTeamConfig {
  schema_version: 3;
  delegation_enabled: boolean;
  orchestration_mode: "standard" | "director";
  teams: Record<string, AgentTeamTeam>;
  subagents: Record<string, AgentTeamSubagent>;
}

export interface AgentTeamCapabilityInfo {
  id: string;
  family?: string;
  access?: string;
  native?: boolean;
  label?: string;
  description?: string;
}

export interface AgentTeamConfigEnvelope extends AgentTeamConfig {
  /** Read-only catalog/effective route data returned by GET. */
  capability_catalog?: Record<string, AgentTeamCapabilityInfo>;
  main_effective_route?: {
    provider?: string;
    model?: string;
    effort?: string;
    reasoning_effort?: string;
  };
  effective_route?: Record<string, unknown>;
  provider_model_options?: Record<string, unknown>;
}

export function emptyAgentTeamConfig(): AgentTeamConfig {
  const subagent = (
    subagent_id: string,
    name: string,
    description: string,
    instructions: string,
    max_workspace_access: AgentTeamSubagent["max_workspace_access"],
    allow_cli_native_tools = false,
    capability_ids: string[] = [],
    scalable = true,
    max_instances = 4,
  ): AgentTeamSubagent => ({
    subagent_id,
    name,
    description,
    instructions,
    enabled: true,
    capability_ids,
    scalable,
    default_instances: 1,
    max_instances,
    max_workspace_access,
    allow_cli_native_tools,
  });
  const subagents: Record<string, AgentTeamSubagent> = {
    general_worker: subagent("general_worker", "汎用作業", "特定分野へ固定しない一般作業。比較、整理、要約、補助調査、小規模なファイル作業等。", "CLI Agentを選択した場合はnative toolsを利用できます。", "write", true, ["workspace_read", "workspace_write", "repo_map", "aoi_tools"], true, 4),
    general_researcher: subagent("general_researcher", "汎用調査", "Web、Docs、Project、Workspace等を横断した調査。", "原則read-onlyで調査結果を整理します。", "read", false, ["workspace_read", "repo_map", "web_read", "aoi_tools"], true, 6),
    docs_operator: subagent("docs_operator", "Docs操作", "Docsノードの検索、読み取り、整理、再構成、更新。", "canonical nodeを確認し、曖昧な対象を推測して更新しない。書き込み前に対象を確認する。AoiTalk high-level Docs toolsのみ使用し、AoiTalk DBを直接触らせない。", "none", false, ["docs_read", "docs_write"], true, 4),
    project_operator: subagent("project_operator", "案件・タスク操作", "Projects、Tasks、Calendar、WBS、Record Tables、課題管理、案件情報などProject管理系AoiTalk data。", "AoiTalk application dataはhigh-level tools経由で操作します。", "read", false, ["project_read", "project_write", "aoi_tools"], true, 4),
    workspace_operator: subagent("workspace_operator", "Workspace操作", "Workspaces / ファイラー上のファイル認識、ファイル検索、ファイル読込、複数ファイル操作、workspace整理、必要な変更。", "CLI providerの場合はnative filesystem/search/edit/shell等を利用できます。", "write", true, ["workspace_read", "workspace_write", "repo_map", "command_execute", "aoi_tools"], true, 4),
  };
  const addSubagent = (subagent_id: string, name: string, description: string, instructions: string, access: AgentTeamSubagent["max_workspace_access"], capability_ids: string[], scalable: boolean, max_instances: number, allow_cli_native_tools: boolean) => {
    subagents[subagent_id] = { subagent_id, name, description, instructions, enabled: true, capability_ids, scalable, default_instances: 1, max_instances, max_workspace_access: access, allow_cli_native_tools };
  };
  addSubagent("code_explorer", "コード調査", "コードベース、依存関係、データフロー、既存実装等を調査。", "コードを調査して簡潔な構造を返します。ファイルを変更しません。", "read", ["workspace_read", "repo_map"], true, 6, true);
  addSubagent("architecture_planner", "設計", "実装方針、責務境界、影響範囲等を整理。", "実装方針を分析し、限定された計画を提案します。", "read", ["workspace_read", "repo_map"], true, 4, true);
  addSubagent("code_implementer", "実装", "実際に変更を行うAgent。", "割り当てられた変更をworkspace sandboxで実装します。", "write", ["workspace_read", "workspace_write", "repo_map", "command_execute", "aoi_tools"], true, 4, true);
  addSubagent("code_reviewer", "コードレビュー", "diff、コード、関連状態をレビューする。", "read-onlyで変更をレビューし、対応が必要な指摘を返します。", "read", ["workspace_read", "repo_map"], true, 4, true);
  addSubagent("story_writer", "執筆", "Story contextを読み、本文や設定資料を作成・更新。", "Story contextを読み、本文や設定資料を作成・更新します。", "none", ["story_read", "story_write"], false, 1, false);
  addSubagent("story_consistency_reviewer", "設定整合性レビュー", "世界設定、時系列、キャラクター設定、過去シーン、用語、既存Story情報の整合性を確認。", "read-onlyでStory情報の整合性を確認します。", "none", ["story_read"], true, 4, false);
  addSubagent("character_voice_reviewer", "キャラクター・口調レビュー", "キャラクターの人格、性格、口調、設定、既存発言との整合性を確認。", "read-onlyでキャラクターの人格・口調との整合性を確認します。", "none", ["story_read"], true, 4, false);
  addSubagent("story_import", "Story取り込み", "Story素材取り込み。", "既存のStory素材を取り込み、正規化します。", "none", ["story_read", "story_import"], false, 1, false);
  return {
    schema_version: 3,
    delegation_enabled: false,
    orchestration_mode: "standard",
    teams: {
      general: {
        team_id: "general",
        name: "General",
        description: "AoiTalkの通常利用を担う常用Team。",
        enabled: true,
        sort_order: 10,
        activation: { mode: "always", contexts: [] },
        subagent_ids: ["general_worker", "general_researcher", "docs_operator", "project_operator", "workspace_operator"],
        execution_profiles: {},
      },
      app_development: {
        team_id: "app_development",
        name: "App Development",
        description: "AoiTalkアプリ開発の調査、設計、実装、レビューを担うTeam。",
        enabled: true,
        sort_order: 20,
        activation: { mode: "contextual", contexts: ["app_development"] },
        subagent_ids: ["code_explorer", "architecture_planner", "code_implementer", "code_reviewer"],
        execution_profiles: {},
      },
      story: {
        team_id: "story",
        name: "Story",
        description: "Story contextでの執筆、整合性確認、取り込みを担うTeam。",
        enabled: true,
        sort_order: 30,
        activation: { mode: "contextual", contexts: ["story"] },
        subagent_ids: ["story_writer", "story_consistency_reviewer", "character_voice_reviewer", "story_import", "general_worker"],
        execution_profiles: {},
      },
    },
    subagents,
  };
}

export function emptyExecutionRoute(): ExecutionRoute {
  return {
    inherit_model: true,
    provider: "",
    model: "",
    effort_policy: "same",
    effort: "",
  };
}

export function canonicalExecutionRoute(value: Partial<ExecutionRoute> | null | undefined): ExecutionRoute {
  const raw = value ?? {};
  const inheritModel = raw.inherit_model !== false;
  if (!inheritModel) {
    // An explicit provider/model route can deliberately defer to that model's
    // own default.  Keep this separate from a raw catalog effort; otherwise a
    // read/modify/write cycle would silently turn model default into an
    // arbitrary explicit value.  Legacy same/lower payloads are interpreted as
    // explicit only when they still carry an effort, and otherwise as default.
    const effort = String(raw.effort || "").trim();
    const effortPolicy = raw.effort_policy === "default"
      ? "default"
      : raw.effort_policy === "explicit" || effort
        ? "explicit"
        : "default";
    return {
      inherit_model: false,
      provider: String(raw.provider || "").trim(),
      model: String(raw.model || "").trim(),
      effort_policy: effortPolicy,
      effort: effortPolicy === "explicit" ? effort : "",
    };
  }
  const effort = String(raw.effort || "").trim();
  const knownPolicy =
    raw.effort_policy === "same" ||
    raw.effort_policy === "lower" ||
    raw.effort_policy === "explicit" ||
    raw.effort_policy === "default";
  const effortPolicy: ExecutionRouteEffortPolicy = knownPolicy && raw.effort_policy ? raw.effort_policy : "same";
  return {
    inherit_model: true,
    provider: "",
    model: "",
    effort_policy: effortPolicy,
    effort: effortPolicy === "explicit" ? effort : "",
  };
}

export function canonicalTeamExecutionProfile(
  id: string,
  value: Partial<TeamExecutionProfile> | null | undefined,
  allowedSubagentIds?: Iterable<string>,
): TeamExecutionProfile {
  const raw = value ?? {};
  const allowed = allowedSubagentIds == null ? null : new Set(Array.from(allowedSubagentIds, String));
  return {
    profile_id: String(raw.profile_id || id),
    name: String(raw.name || ""),
    enabled: raw.enabled !== false,
    default_route: canonicalExecutionRoute(raw.default_route),
    overrides: Object.fromEntries(
      Object.entries(raw.overrides ?? {})
        .filter(([subagentId]) => allowed == null || allowed.has(String(subagentId)))
        .map(([subagentId, route]) => [
          String(subagentId),
          canonicalExecutionRoute(route),
        ]),
    ),
  };
}

/** Strip all GET-only metadata before a canonical PUT. */
export function canonicalAgentTeamConfig(value: AgentTeamConfig | AgentTeamConfigEnvelope): AgentTeamConfig {
  return {
    schema_version: 3,
    delegation_enabled: Boolean(value.delegation_enabled),
    orchestration_mode: value.orchestration_mode === "director" ? "director" : "standard",
    teams: Object.fromEntries(
      Object.entries(value.teams ?? {}).map(([id, team]) => [id, {
        team_id: String(team.team_id || id),
        name: String(team.name || ""),
        description: String(team.description || ""),
        enabled: team.enabled !== false,
        sort_order: Number.isFinite(Number(team.sort_order)) ? Number(team.sort_order) : 0,
        activation: {
          mode: team.activation?.mode === "contextual" || team.activation?.mode === "manual" ? team.activation.mode : "always",
          contexts: Array.isArray(team.activation?.contexts) ? team.activation.contexts.map(String) : [],
        },
        subagent_ids: Array.isArray(team.subagent_ids) ? team.subagent_ids.map(String) : [],
        execution_profiles: Object.fromEntries(
          Object.entries(team.execution_profiles ?? {}).map(([profileId, profile]) => [
            profileId,
            canonicalTeamExecutionProfile(profileId, profile, team.subagent_ids),
          ]),
        ),
      }]),
    ),
    subagents: Object.fromEntries(
      Object.entries(value.subagents ?? {}).map(([id, subagent]) => [id, {
        subagent_id: String(subagent.subagent_id || id),
        name: String(subagent.name || ""),
        description: String(subagent.description || ""),
        instructions: String(subagent.instructions || ""),
        enabled: subagent.enabled !== false,
        capability_ids: Array.isArray(subagent.capability_ids) ? subagent.capability_ids.map(String) : [],
        scalable: Boolean(subagent.scalable),
        default_instances: Math.max(1, Number(subagent.default_instances) || 1),
        max_instances: Math.max(1, Number(subagent.max_instances) || 1),
        max_workspace_access: subagent.max_workspace_access === "write" || subagent.max_workspace_access === "read" ? subagent.max_workspace_access : "none",
        allow_cli_native_tools: Boolean(subagent.allow_cli_native_tools),
      }]),
    ),
  };
}

export interface OllamaPullTask {
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

export interface OllamaDeleteResponse {
  success: boolean;
  model: string;
}

export type ProviderDraft = {
  model: string;
  customModel: string;
};

export type ProviderSettingsDraft = {
  base_url?: string;
  api_key?: string;
  reasoning_effort?: string;
  llama_cpp?: LlamaCppSettingsPayload;
};

export type ModelClassDraft = {
  provider: string;
  model: string;
  customModel: string;
  mode: string;
  baseUrl: string;
  apiKey: string;
  effortPolicy: EffortPolicy;
  engine?: "speech_recognition" | "llm" | "off";
  inherit?: boolean;
};

export const EXTERNAL_AGENT_PROVIDERS = new Set([
  "openai",
  "openrouter",
  "deepseek",
  "deepinfra",
  "gemini",
  "kimi",
  "antigravity-cli",
  "claude-cli",
  "codex-cli",
  "grok-cli",
]);

export const CONNECTION_SETTINGS_PROVIDERS = new Set([
  "ollama",
  "openai_compatible_local",
  "openrouter",
  "deepseek",
  "deepinfra",
  "sglang",
  "kimi",
]);

export const REASONING_EFFORT_PROVIDERS = new Set([
  "codex-cli",
  "claude-cli",
  "deepseek",
  "deepinfra",
  "kimi",
]);

export const API_KEY_REQUIRED_PROVIDERS = new Set([
  "openai",
  "gemini",
  "openrouter",
  "deepseek",
  "deepinfra",
  "kimi",
]);

export const MODEL_PAGE_SIZE = 24;

export async function pyFetch<T = unknown>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api/python-proxy${path}`, {
    credentials: "include",
    headers: { "Content-Type": "application/json", ...init?.headers },
    ...init,
  });
  if (!res.ok) {
    const detail: unknown = await res.json().catch(() => ({ detail: res.statusText }));
    const asRecord = detail && typeof detail === "object" ? detail as Record<string, unknown> : {};
    const raw = asRecord.detail ?? asRecord.message ?? asRecord.errors;
    const message = typeof raw === "string"
      ? raw
      : raw
        ? JSON.stringify(raw)
        : res.statusText;
    throw new Error(message || `API Error: ${res.status}`);
  }
  return res.json();
}

export function providerHint(providerId: string): string {
  switch (providerId) {
    case "codex-cli":
      return "Codex CLI は --model を受け付けます。候補はCLIから取得した一覧ではなく、未掲載モデルは直接入力してください。";
    case "claude-cli":
      return "Claude Code は alias とフルモデル名を受け付けます。候補はCLIから取得した一覧ではありません。";
    case "antigravity-cli":
      return "Antigravity CLI は --model でモデルを指定します。候補は agy models から取得した一覧ではありません。";
    case "grok-cli":
      return "Grok Build CLI はローカルの grok 認証を使います。未認証の場合は `grok login` を実行してください。AoiTalkから --always-approve は付けません。";
    case "sglang":
      return "SGLang は Hugging Face の model path または /v1/models のIDを使います。";
    case "openai_compatible_local":
      return "llama-server、exo、MLX LM などの /v1/chat/completions 互換APIを指定します。候補にBase URLがある場合は自動で反映します。";
    case "openrouter":
      return "OpenRouter は公開 Models API から候補を取得します。";
    case "deepseek":
      return "DeepSeek は公式の OpenAI互換APIを使います。API keyを設定してください。Base URLは通常 /v1 不要です。";
    case "deepinfra":
      return "DeepInfra は公式の OpenAI互換 Chat Completions APIを使います。API key、推論モード、必要ならBase URLを設定してください。モデル候補は /v1/models から取得します。";
    case "kimi":
      return "Kimi は Moonshot AI の OpenAI互換APIを使います。API keyを設定してください。";
    case "ollama":
      return "Ollama はインストール済みモデルと Pull 候補を分けて表示します。";
    default:
      return "プロバイダーが受け付けるモデルIDを指定します。";
  }
}

export function modelSourceLabel(item: LlmModelOption): string | null {
  if (item.source_label) return item.source_label;
  if (item.installed) return "インストール済み";
  if (item.custom_current) return "現在の設定";
  return null;
}

export function modelSummary(item: LlmModelOption): string {
  if (item.description) return item.description;
  if (item.server_label && item.base_url) return `${item.server_label} ${item.base_url}`;
  if (item.context_length) return `context ${item.context_length}`;
  if (item.details) {
    return `${item.details.parameter_size || "-"} / ${item.details.quantization_level || "-"} / ${formatBytes(item.size)}`;
  }
  return item.id;
}

export function providerSourceLabel(source: string): string {
  switch (source) {
    case "remote":
      return "API取得";
    case "cached":
      return "前回取得";
    case "installed":
      return "インストール確認済み";
    case "cli-suggested":
      return "CLI候補";
    case "platform-suggested":
      return "OS候補";
    case "static-suggested":
      return "静的候補";
    case "static":
      return "静的候補";
    default:
      return source || "候補";
  }
}

export function providerSelection(provider: LlmProviderCatalog | null | undefined): ProviderDraft {
  const firstModel = provider?.models[0]?.id ?? "";
  const configuredModel = provider?.configured_model?.trim();
  if (!configuredModel) {
    return { model: firstModel, customModel: "" };
  }
  if (provider?.models.some((item) => item.id === configuredModel)) {
    return { model: configuredModel, customModel: "" };
  }
  return { model: firstModel, customModel: configuredModel };
}

export function reasoningEffortOptionsForModel(
  provider: LlmProviderCatalog | null | undefined,
  modelId: string,
): string[] {
  const modelOptions = provider?.models.find((item) => item.id === modelId)?.reasoning_effort_options;
  if (modelOptions?.length) return modelOptions;
  return provider?.settings?.reasoning_effort_options ?? [];
}

export function modelOptionSettings(option: LlmModelOption | null | undefined): ProviderSettingsDraft | undefined {
  if (!option?.base_url) return undefined;
  return { base_url: option.base_url };
}

export function defaultModeForOptions(options: string[] | undefined, preferred = "medium"): string {
  const values = options ?? [];
  if (!values.length) return preferred;
  if (values.includes(preferred)) return preferred;
  if (values.includes("fast")) return "fast";
  if (values.includes("medium")) return "medium";
  return values[0];
}

/** Keep a current effort when the catalog still accepts it; otherwise use the catalog default. */
export function resolveCatalogEffort(currentEffort: string, options: readonly string[] | undefined): string {
  const values = [...new Set((options ?? []).filter(Boolean))];
  if (!values.length) return "";
  if (currentEffort && values.includes(currentEffort)) return currentEffort;
  return defaultModeForOptions(values);
}

export function buildClassDraft(
  route: (ModelRouteSettings & { base_url?: string; api_key?: string; engine?: "speech_recognition" | "llm" | "off" }) | undefined,
  providers: LlmProviderCatalog[] | undefined,
): ModelClassDraft {
  const routeProvider = route?.provider || "";
  const routeModel = route?.model || "";
  const providerCatalog = providers?.find((item) => item.id === routeProvider);
  const selection = routeProvider && routeModel
    ? providerSelection({ ...(providerCatalog ?? { id: routeProvider, label: routeProvider, models: [], supports_custom_model: true, source: "static" }), configured_model: routeModel })
    : { model: "", customModel: "" };
  return {
    provider: routeProvider,
    model: selection.model,
    customModel: selection.customModel || (!providerCatalog && routeModel ? routeModel : ""),
    mode: route?.effort || route?.mode || route?.reasoning_effort || "",
    baseUrl: route?.base_url || "",
    apiKey: "",
    effortPolicy: (route?.effort_policy as EffortPolicy) || "same",
    engine: route?.engine,
    inherit: route?.inherit ?? false,
  };
}
