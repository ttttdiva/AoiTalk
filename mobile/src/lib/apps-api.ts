/**
 * Apps のモバイル API クライアント。
 *
 * モバイルは App の概要・関連・実行状況を扱う観察/操作 surface に限定する。
 * source import、ファイル書き込み、README/manifest 編集、Git、Release 作成、
 * Grant 管理、embedded runtime はここへ追加しない。
 */

import { fetchApi, fetchApiBytes } from "./api-client";
import type { components } from "../types/api-types.gen";

export type AppPermission = "viewer" | "runner" | "developer" | "maintainer" | "admin";
export type AppBindingMode = "development" | "installed";
export type AppJobType = "build" | "test" | "run" | "package";

export interface AppTarget {
  id: string;
  app_id: string;
  target_key: string;
  display_name: string;
  surface: string;
  runtime: string;
  execution_host: string;
  entrypoint: string;
  manifest_snapshot?: Record<string, unknown>;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface AppArtifact {
  id: string;
  release_id: string;
  target_id: string;
  artifact_type: string;
  filename: string;
  sha256: string;
  size_bytes: number;
  created_at?: string | null;
}

export interface AppRelease {
  id: string;
  app_id: string;
  version: string;
  git_revision: string;
  manifest_hash: string;
  readme_hash: string;
  changelog?: string | null;
  status: "published" | "deprecated" | string;
  created_at?: string | null;
  artifacts?: AppArtifact[];
}

export interface AppSummary {
  id: string;
  owner_user_id?: string | null;
  origin_project_id?: string | null;
  name: string;
  slug: string;
  description?: string | null;
  visibility: string;
  default_target_key?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  archived_at?: string | null;
  permission?: AppPermission | string;
  targets?: AppTarget[];
  releases?: AppRelease[];
  related_project_ids?: string[];
}

export interface AppFile {
  path: string;
  filename?: string;
  name?: string;
  size_bytes?: number;
  size?: number;
  is_dir?: boolean;
  modified_at?: string | null;
  extension?: string;
  [key: string]: unknown;
}

export interface AppFileContent {
  path: string;
  content: string;
  sha256?: string;
}

export interface AppJob {
  id: string;
  app_id: string;
  target_id?: string | null;
  project_id?: string | null;
  release_id?: string | null;
  job_type: AppJobType | string;
  status: string;
  result_json?: Record<string, unknown>;
  exit_code?: number | null;
  started_at?: string | null;
  ended_at?: string | null;
}

export interface TaskAppLink {
  id: string;
  task_id: string;
  app_id: string;
  target_id?: string | null;
  relation_type: string;
  app?: AppSummary;
  target?: AppTarget | null;
}

export interface ProjectAppBinding {
  project_id: string;
  app_id: string;
  binding_mode: AppBindingMode;
  installed_release_id?: string | null;
  enabled: boolean;
  pinned: boolean;
  display_alias?: string | null;
  config_json?: Record<string, unknown>;
  capability_grants_json?: Record<string, unknown>;
  created_at?: string | null;
  updated_at?: string | null;
  permission?: AppPermission | string;
  app: AppSummary;
  targets?: AppTarget[];
  latest_release?: AppRelease | null;
  latest_job?: AppJob | null;
  latest_agent_run?: {
    id: string;
    status?: string;
    created_at?: string | null;
  } | null;
  incomplete_task_count?: number;
}

export interface AppContext {
  app: AppSummary;
  permission: AppPermission | string;
  target_key?: string | null;
  targets?: AppTarget[];
  releases?: AppRelease[];
  manifest?: Record<string, unknown>;
  manifest_hash?: string | null;
  readme: string;
  binding_mode?: AppBindingMode;
  selected_release?: AppRelease | null;
}

/** Release-bound declarative App runtime wire types. */
export type AppRuntimeNodeKind = components["schemas"]["AppUiNode"]["kind"];
export type AppRuntimeValuePath = string;
export type AppRuntimeOption = components["schemas"]["AppUiOption"];
export type AppRuntimeFormField = components["schemas"]["AppUiField"];
export type AppRuntimeNode = components["schemas"]["AppUiNode"];
export type AppRuntimeUi = components["schemas"]["AppUiDocument"];
export type AppRuntimeAction = components["schemas"]["AppActionDescriptor"];
export type AppRuntimeFileModel = {
  id: string;
  file_id?: string;
  artifact_id?: string | null;
  filename: string;
  size_bytes: number;
  content_type: string;
  direction: "input" | "output";
  sha256: string;
  run_id?: string;
  download_url?: string | null;
};
export type AppRuntimeFileRef = {
  id: string;
  file_id?: string;
  artifact_id?: string | null;
  filename?: string;
  direction?: "input" | "output";
  run_id?: string;
};
export type AppRuntimeRun = components["schemas"]["AppRunView"] & { files?: AppRuntimeFileModel[] };
export type AppRuntimeAvailability = components["schemas"]["AppRuntimeAvailability"];
export type AppRuntimeActivation = components["schemas"]["AppRuntimeActivation"];
export type AppRuntimeDeclaration = components["schemas"]["AppRuntimeResponse"];

// Keep each transport response an exact generated component alias.  Domain
// helpers normalize these immutable projections for the renderer/repository.
export type AppRuntimeWireResponse = components["schemas"]["AppRuntimeResponse"];
export type AppRuntimeWireActionRunResponse = components["schemas"]["AppActionRunResponse"];
export type AppRuntimeWireRun = components["schemas"]["AppRunView"];
export type AppRuntimeWireRunList = components["schemas"]["AppRunListResponse"];
export type AppRuntimeWireFile = components["schemas"]["AppRunFileResponse"];
export type AppRuntimeWireLogs = components["schemas"]["AppRunLogsResponse"];
export type AppRuntimeInputFile = components["schemas"]["AppRunFileUploadResponse"];

export type AppRuntimeFileAsset = {
  uri: string;
  name: string;
  mimeType?: string | null;
  size?: number | null;
};

export class AppRuntimeContractError extends Error {
  constructor(message: string) {
    super(`App Runtime契約が不正です: ${message}`);
    this.name = "AppRuntimeContractError";
  }
}

type RuntimeRecord = Record<string, unknown>;

function runtimeRecord(value: unknown, label: string): RuntimeRecord {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new AppRuntimeContractError(`${label}はobjectである必要があります`);
  }
  return value as RuntimeRecord;
}

function runtimeString(value: unknown, label: string): string {
  if (typeof value !== "string" || value.length === 0) {
    throw new AppRuntimeContractError(`${label}がありません`);
  }
  return value;
}

function runtimeBoolean(value: unknown, label: string): boolean {
  if (typeof value !== "boolean") throw new AppRuntimeContractError(`${label}がbooleanではありません`);
  return value;
}

function runtimeKeys(value: RuntimeRecord, allowed: readonly string[], label: string): void {
  const allowedSet = new Set(allowed);
  const extra = Object.keys(value).find((key) => !allowedSet.has(key));
  if (extra) throw new AppRuntimeContractError(`${label}.${extra}は未定義です`);
}

const RUNTIME_NODE_KINDS = new Set<components["schemas"]["AppUiNode"]["kind"]>([
  "stack", "card", "text", "button", "form", "select", "switch", "list", "table", "detail", "progress", "log", "result", "file",
]);
const RUNTIME_VALUE_PATH = /^run\.(?:result|status)(?:\.[A-Za-z][A-Za-z0-9_-]*)*$/;

function parseRuntimeOption(value: unknown, label: string): AppRuntimeOption {
  const raw = runtimeRecord(value, label);
  runtimeKeys(raw, ["label", "value"], label);
  return { label: runtimeString(raw.label, `${label}.label`), value: runtimeString(raw.value, `${label}.value`) };
}

function parseRuntimeField(value: unknown, label: string): AppRuntimeFormField {
  const raw = runtimeRecord(value, label);
  runtimeKeys(raw, ["name", "label", "kind", "required", "options"], label);
  const kind = raw.kind;
  if (kind !== "text" && kind !== "number" && kind !== "select" && kind !== "switch" && kind !== "file") {
    throw new AppRuntimeContractError(`${label}.kindが不正です`);
  }
  const options = raw.options === undefined || raw.options === null
    ? raw.options
    : Array.isArray(raw.options) ? raw.options.map((item, index) => parseRuntimeOption(item, `${label}.options[${index}]`)) : (() => { throw new AppRuntimeContractError(`${label}.optionsがarrayではありません`); })();
  return {
    name: runtimeString(raw.name, `${label}.name`),
    label: runtimeString(raw.label, `${label}.label`),
    kind,
    required: runtimeBoolean(raw.required, `${label}.required`),
    options,
  };
}

function parseRuntimeNode(value: unknown, label: string): AppRuntimeNode {
  const raw = runtimeRecord(value, label);
  runtimeKeys(raw, ["id", "kind", "children", "fields", "columns", "label", "text", "name", "options", "data", "value", "value_path", "action_id", "target_key"], label);
  const kind = raw.kind;
  if (typeof kind !== "string" || !RUNTIME_NODE_KINDS.has(kind as components["schemas"]["AppUiNode"]["kind"])) {
    throw new AppRuntimeContractError(`${label}.kindが不正です`);
  }
  const children = raw.children === undefined
    ? raw.children
    : Array.isArray(raw.children) ? raw.children.map((item, index) => parseRuntimeNode(item, `${label}.children[${index}]`)) : (() => { throw new AppRuntimeContractError(`${label}.childrenがarrayではありません`); })();
  const fields = raw.fields === undefined || raw.fields === null
    ? raw.fields
    : Array.isArray(raw.fields) ? raw.fields.map((item, index) => parseRuntimeField(item, `${label}.fields[${index}]`)) : (() => { throw new AppRuntimeContractError(`${label}.fieldsがarrayではありません`); })();
  const columns = raw.columns === undefined || raw.columns === null
    ? raw.columns
    : Array.isArray(raw.columns) ? raw.columns.map((item, index) => {
      const column = runtimeRecord(item, `${label}.columns[${index}]`);
      runtimeKeys(column, ["name", "label"], `${label}.columns[${index}]`);
      return { name: runtimeString(column.name, `${label}.columns[${index}].name`), label: runtimeString(column.label, `${label}.columns[${index}].label`) };
    }) : (() => { throw new AppRuntimeContractError(`${label}.columnsがarrayではありません`); })();
  const options = raw.options === undefined || raw.options === null
    ? raw.options
    : Array.isArray(raw.options) ? raw.options.map((item, index) => parseRuntimeOption(item, `${label}.options[${index}]`)) : (() => { throw new AppRuntimeContractError(`${label}.optionsがarrayではありません`); })();
  if (raw.value_path !== undefined && raw.value_path !== null && (typeof raw.value_path !== "string" || !RUNTIME_VALUE_PATH.test(raw.value_path))) {
    throw new AppRuntimeContractError(`${label}.value_pathが不正です`);
  }
  for (const [key, value] of [["label", raw.label], ["text", raw.text], ["name", raw.name], ["action_id", raw.action_id], ["target_key", raw.target_key]] as const) {
    if (value !== undefined && value !== null && typeof value !== "string") throw new AppRuntimeContractError(`${label}.${key}がstringではありません`);
  }
  return {
    id: runtimeString(raw.id, `${label}.id`),
    kind: kind as AppRuntimeNode["kind"],
    ...(children === undefined ? {} : { children }),
    ...(fields === undefined ? {} : { fields }),
    ...(columns === undefined ? {} : { columns }),
    ...(options === undefined ? {} : { options }),
    ...(raw.label === undefined ? {} : { label: raw.label as string | null }),
    ...(raw.text === undefined ? {} : { text: raw.text as string | null }),
    ...(raw.name === undefined ? {} : { name: raw.name as string | null }),
    ...(raw.data === undefined ? {} : { data: raw.data }),
    ...(raw.value === undefined ? {} : { value: raw.value }),
    ...(raw.value_path === undefined ? {} : { value_path: raw.value_path as string | null }),
    ...(raw.action_id === undefined ? {} : { action_id: raw.action_id as string | null }),
    ...(raw.target_key === undefined ? {} : { target_key: raw.target_key as string | null }),
  };
}

function parseRuntimeUi(value: unknown): AppRuntimeUi {
  const raw = runtimeRecord(value, "ui");
  runtimeKeys(raw, ["schema_version", "root", "target_key"], "ui");
  if (raw.schema_version !== 1) throw new AppRuntimeContractError("ui.schema_versionが未対応です");
  if (raw.target_key !== undefined && raw.target_key !== null && typeof raw.target_key !== "string") throw new AppRuntimeContractError("ui.target_keyがstringではありません");
  return { schema_version: 1, root: parseRuntimeNode(raw.root, "ui.root"), ...(raw.target_key === undefined ? {} : { target_key: raw.target_key as string | null }) };
}

function parseRuntimeFile(value: unknown): AppRuntimeFileModel {
  const raw = runtimeRecord(value, "run.file");
  const base = {
    filename: runtimeString(raw.filename, "run.file.filename"),
    content_type: runtimeString(raw.content_type, "run.file.content_type"),
    size_bytes: typeof raw.size_bytes === "number" && raw.size_bytes >= 0 ? raw.size_bytes : (() => { throw new AppRuntimeContractError("run.file.size_bytesが不正です"); })(),
    sha256: runtimeString(raw.sha256, "run.file.sha256"),
    ...(raw.download_url === undefined ? {} : { download_url: raw.download_url === null ? null : runtimeString(raw.download_url, "run.file.download_url") }),
  };
  if (typeof raw.artifact_id === "string" && typeof raw.run_id === "string") {
    runtimeKeys(raw, ["artifact_id", "content_type", "download_url", "filename", "run_id", "sha256", "size_bytes"], "run.file");
    if (typeof raw.download_url !== "string") throw new AppRuntimeContractError("run.file.download_urlがありません");
    return { ...base, id: raw.artifact_id, artifact_id: raw.artifact_id, direction: "output", run_id: raw.run_id, download_url: raw.download_url };
  }
  if (typeof raw.id === "string" && (raw.direction === "input" || raw.direction === "output")) {
    runtimeKeys(raw, ["artifact_id", "content_type", "direction", "download_url", "filename", "id", "sha256", "size_bytes"], "run.file");
    return { ...base, id: raw.id, artifact_id: raw.artifact_id === null || raw.artifact_id === undefined ? raw.artifact_id : runtimeString(raw.artifact_id, "run.file.artifact_id"), direction: raw.direction };
  }
  throw new AppRuntimeContractError("run.fileのopaque id shapeが不正です");
}

function parseRuntimeRun(value: unknown): AppRuntimeRun {
  const raw = runtimeRecord(value, "run");
  runtimeKeys(raw, ["action_id", "app_id", "created_at", "error", "exit_code", "finished_at", "id", "idempotency_key", "links", "mode", "process_status", "project_id", "readiness_status", "release_id", "result", "started_at", "status", "target_id"], "run");
  const statuses = ["unknown", "queued", "dispatching", "accepted", "starting", "running", "stopping", "succeeded", "failed", "cancelled", "blocked"] as const;
  const processes = ["unknown", "not_started", "running", "exited"] as const;
  const readiness = ["unknown", "not_required", "ready", "pending", "failed"] as const;
  if (!statuses.includes(raw.status as typeof statuses[number])) throw new AppRuntimeContractError("run.statusが不正です");
  if (!processes.includes(raw.process_status as typeof processes[number])) throw new AppRuntimeContractError("run.process_statusが不正です");
  if (!readiness.includes(raw.readiness_status as typeof readiness[number])) throw new AppRuntimeContractError("run.readiness_statusが不正です");
  if (raw.mode !== "finite" && raw.mode !== "service" && raw.mode !== "desktop") throw new AppRuntimeContractError("run.modeが不正です");
  if (typeof raw.exit_code !== "number" && raw.exit_code !== null) throw new AppRuntimeContractError("run.exit_codeが不正です");
  if (raw.error !== undefined && raw.error !== null && (typeof raw.error !== "object" || Array.isArray(raw.error) || Object.values(raw.error).some((item) => typeof item !== "string"))) throw new AppRuntimeContractError("run.errorが不正です");
  if (raw.links !== undefined && (typeof raw.links !== "object" || raw.links === null || Array.isArray(raw.links) || Object.values(raw.links).some((item) => typeof item !== "string"))) throw new AppRuntimeContractError("run.linksが不正です");
  return {
    action_id: runtimeString(raw.action_id, "run.action_id"),
    app_id: runtimeString(raw.app_id, "run.app_id"),
    ...(raw.created_at === undefined ? {} : { created_at: raw.created_at as string | null }),
    ...(raw.error === undefined ? {} : { error: raw.error as { [key: string]: string } | null }),
    exit_code: raw.exit_code as number | null,
    ...(raw.finished_at === undefined ? {} : { finished_at: raw.finished_at as string | null }),
    id: runtimeString(raw.id, "run.id"),
    idempotency_key: runtimeString(raw.idempotency_key, "run.idempotency_key"),
    ...(raw.links === undefined ? {} : { links: raw.links as { [key: string]: string } }),
    mode: raw.mode as AppRuntimeRun["mode"],
    process_status: raw.process_status as AppRuntimeRun["process_status"],
    ...(raw.project_id === undefined ? {} : { project_id: raw.project_id as string | null }),
    readiness_status: raw.readiness_status as AppRuntimeRun["readiness_status"],
    release_id: runtimeString(raw.release_id, "run.release_id"),
    ...(raw.result === undefined ? {} : { result: raw.result }),
    ...(raw.started_at === undefined ? {} : { started_at: raw.started_at as string | null }),
    status: raw.status as AppRuntimeRun["status"],
    target_id: runtimeString(raw.target_id, "run.target_id"),
  };
}

function parseRuntimeAction(value: unknown, label: string): AppRuntimeAction {
  const raw = runtimeRecord(value, label);
  runtimeKeys(raw, ["action_id", "available", "capabilities", "description", "execution", "execution_target_id", "input_schema", "label", "output_schema", "target_key", "unavailable_reason"], label);
  if (raw.capabilities !== undefined && (!Array.isArray(raw.capabilities) || raw.capabilities.some((item) => typeof item !== "string"))) throw new AppRuntimeContractError(`${label}.capabilitiesが不正です`);
  if (raw.input_schema === null || !raw.input_schema || typeof raw.input_schema !== "object" || Array.isArray(raw.input_schema)) throw new AppRuntimeContractError(`${label}.input_schemaが不正です`);
  if (raw.output_schema === null || !raw.output_schema || typeof raw.output_schema !== "object" || Array.isArray(raw.output_schema)) throw new AppRuntimeContractError(`${label}.output_schemaが不正です`);
  const execution = runtimeRecord(raw.execution, `${label}.execution`);
  runtimeKeys(execution, ["kind", "mode", "command_key", "saved_action_id", "sha256", "contract_hash"], `${label}.execution`);
  if (execution.kind !== "target_command" && execution.kind !== "pc_saved_action") throw new AppRuntimeContractError(`${label}.execution.kindが不正です`);
  if (execution.mode !== "finite" && execution.mode !== "service" && execution.mode !== "desktop") throw new AppRuntimeContractError(`${label}.execution.modeが不正です`);
  if (raw.description !== undefined && raw.description !== null && typeof raw.description !== "string") throw new AppRuntimeContractError(`${label}.descriptionが不正です`);
  if (raw.unavailable_reason !== undefined && raw.unavailable_reason !== null && typeof raw.unavailable_reason !== "string") throw new AppRuntimeContractError(`${label}.unavailable_reasonが不正です`);
  return {
    action_id: runtimeString(raw.action_id, `${label}.action_id`),
    available: runtimeBoolean(raw.available, `${label}.available`),
    ...(raw.capabilities === undefined ? {} : { capabilities: raw.capabilities as string[] }),
    ...(raw.description === undefined ? {} : { description: raw.description as string | null }),
    execution: execution as components["schemas"]["AppActionExecution"],
    execution_target_id: runtimeString(raw.execution_target_id, `${label}.execution_target_id`),
    input_schema: raw.input_schema as { [key: string]: unknown },
    label: runtimeString(raw.label, `${label}.label`),
    output_schema: raw.output_schema as { [key: string]: unknown },
    target_key: runtimeString(raw.target_key, `${label}.target_key`),
    ...(raw.unavailable_reason === undefined ? {} : { unavailable_reason: raw.unavailable_reason as string | null }),
  };
}

function parseRuntimeRelease(value: unknown): AppRuntimeWireResponse["release"] {
  const raw = runtimeRecord(value, "runtime.release");
  runtimeKeys(raw, ["id", "version", "git_revision", "manifest_hash", "readme_hash", "status", "source_bundle_sha256", "source_bundle_size_bytes", "created_at"], "runtime.release");
  if (raw.status !== "published" && raw.status !== "deprecated") throw new AppRuntimeContractError("runtime.release.statusが不正です");
  return {
    id: runtimeString(raw.id, "runtime.release.id"),
    version: runtimeString(raw.version, "runtime.release.version"),
    git_revision: runtimeString(raw.git_revision, "runtime.release.git_revision"),
    manifest_hash: runtimeString(raw.manifest_hash, "runtime.release.manifest_hash"),
    readme_hash: runtimeString(raw.readme_hash, "runtime.release.readme_hash"),
    status: raw.status,
    ...(raw.source_bundle_sha256 === undefined ? {} : { source_bundle_sha256: raw.source_bundle_sha256 as string | null }),
    ...(raw.source_bundle_size_bytes === undefined ? {} : { source_bundle_size_bytes: raw.source_bundle_size_bytes as number | null }),
    ...(raw.created_at === undefined ? {} : { created_at: raw.created_at as string | null }),
  };
}

function parseRuntimeTarget(value: unknown): AppRuntimeWireResponse["target"] {
  const raw = runtimeRecord(value, "runtime.target");
  runtimeKeys(raw, ["id", "target_key", "display_name", "surface", "runtime", "execution_host", "entrypoint", "runtime_bundle_sha256", "runtime_bundle_size_bytes"], "runtime.target");
  const surfaces = ["embedded_web", "standalone_web", "desktop_gui", "headless", "office"] as const;
  const runtimes = ["static_web", "node", "python", "powershell", "batch", "vba", "executable"] as const;
  const hosts = ["aoitalk", "server", "client", "browser", "office", "download_only"] as const;
  if (!surfaces.includes(raw.surface as typeof surfaces[number])) throw new AppRuntimeContractError("runtime.target.surfaceが不正です");
  if (!runtimes.includes(raw.runtime as typeof runtimes[number])) throw new AppRuntimeContractError("runtime.target.runtimeが不正です");
  if (!hosts.includes(raw.execution_host as typeof hosts[number])) throw new AppRuntimeContractError("runtime.target.execution_hostが不正です");
  return {
    id: runtimeString(raw.id, "runtime.target.id"),
    target_key: runtimeString(raw.target_key, "runtime.target.target_key"),
    entrypoint: runtimeString(raw.entrypoint, "runtime.target.entrypoint"),
    surface: raw.surface as AppRuntimeWireResponse["target"]["surface"],
    runtime: raw.runtime as AppRuntimeWireResponse["target"]["runtime"],
    execution_host: raw.execution_host as AppRuntimeWireResponse["target"]["execution_host"],
    ...(raw.display_name === undefined ? {} : { display_name: raw.display_name as string | null }),
    ...(raw.runtime_bundle_sha256 === undefined ? {} : { runtime_bundle_sha256: raw.runtime_bundle_sha256 as string | null }),
    ...(raw.runtime_bundle_size_bytes === undefined ? {} : { runtime_bundle_size_bytes: raw.runtime_bundle_size_bytes as number | null }),
  };
}

function parseRuntimeActivation(value: unknown): AppRuntimeActivation | null {
  if (value === null) return null;
  const raw = runtimeRecord(value, "runtime.activation");
  runtimeKeys(raw, ["app_id", "release_id", "manifest_hash", "activated_by", "activated_at"], "runtime.activation");
  return {
    app_id: runtimeString(raw.app_id, "runtime.activation.app_id"),
    release_id: runtimeString(raw.release_id, "runtime.activation.release_id"),
    manifest_hash: runtimeString(raw.manifest_hash, "runtime.activation.manifest_hash"),
    ...(raw.activated_by === undefined ? {} : { activated_by: raw.activated_by as string | null }),
    ...(raw.activated_at === undefined ? {} : { activated_at: raw.activated_at as string | null }),
  };
}

export function normalizeAppRuntimeFile(value: AppRuntimeWireFile | AppRuntimeInputFile): AppRuntimeFileModel {
  return parseRuntimeFile(value);
}

export function normalizeAppRuntimeRun(value: AppRuntimeWireRun): AppRuntimeRun {
  return parseRuntimeRun(value);
}

export function normalizeAppRuntimeDeclaration(value: AppRuntimeWireResponse): AppRuntimeDeclaration {
  const raw = runtimeRecord(value, "runtime");
  runtimeKeys(raw, ["actions", "activation", "app_id", "availability", "latest_runs", "release", "target", "ui"], "runtime");
  if (!Array.isArray(raw.actions)) throw new AppRuntimeContractError("runtime.actionsがarrayではありません");
  if (!Array.isArray(raw.latest_runs)) throw new AppRuntimeContractError("runtime.latest_runsがarrayではありません");
  const availability = runtimeRecord(raw.availability, "runtime.availability");
  runtimeKeys(availability, ["available", "reason"], "runtime.availability");
  if (availability.reason !== undefined && availability.reason !== null && typeof availability.reason !== "string") throw new AppRuntimeContractError("runtime.availability.reasonが不正です");
  const activation = parseRuntimeActivation(raw.activation);
  return {
    ...(raw as AppRuntimeWireResponse),
    app_id: runtimeString(raw.app_id, "runtime.app_id"),
    actions: raw.actions.map((item, index) => parseRuntimeAction(item, `runtime.actions[${index}]`)),
    activation,
    availability: {
      available: runtimeBoolean(availability.available, "runtime.availability.available"),
      ...(availability.reason === undefined ? {} : { reason: availability.reason as string | null }),
    },
    latest_runs: raw.latest_runs.map(parseRuntimeRun),
    release: parseRuntimeRelease(raw.release),
    target: parseRuntimeTarget(raw.target),
    ui: raw.ui === undefined || raw.ui === null ? null : parseRuntimeUi(raw.ui),
  };
}

export interface CreateAppInput {
  name: string;
  slug?: string;
  description?: string;
  origin_project_id?: string | null;
  visibility?: "private" | "shared" | "public";
}

export interface ProjectAppInput {
  app_id: string;
  binding_mode: AppBindingMode;
  installed_release_id?: string | null;
  enabled?: boolean;
  pinned?: boolean;
  display_alias?: string | null;
}

export interface TaskAppInput {
  app_id: string;
  target_id?: string | null;
  relation_type?: string;
}

export class AppsOfflineMutationError extends Error {
  constructor() {
    super("オフラインでは Apps の変更を実行できません");
    this.name = "AppsOfflineMutationError";
  }
}

export class AppsPermissionError extends Error {
  constructor(message = "この Apps 操作には runner 権限が必要です") {
    super(message);
    this.name = "AppsPermissionError";
  }
}

const PERMISSION_RANK: Record<AppPermission, number> = {
  viewer: 10,
  runner: 20,
  developer: 30,
  maintainer: 40,
  admin: 50,
};

export function permissionAtLeast(
  permission: string | null | undefined,
  required: AppPermission,
): boolean {
  return (
    PERMISSION_RANK[permission as AppPermission] ?? 0
  ) >= PERMISSION_RANK[required];
}

export function assertRunnerPermission(permission: string | null | undefined): void {
  if (!permissionAtLeast(permission, "runner")) {
    throw new AppsPermissionError();
  }
}

function query(params: Record<string, string | null | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== null && value !== undefined && value !== "") {
      search.set(key, value);
    }
  }
  const encoded = search.toString();
  return encoded ? `?${encoded}` : "";
}

function arrayPayload<T>(payload: T[] | { [key: string]: unknown }, key: string): T[] {
  if (Array.isArray(payload)) return payload;
  const value = payload[key];
  return Array.isArray(value) ? (value as T[]) : [];
}

export const appsApi = {
  async list(projectId?: string | null): Promise<AppSummary[]> {
    const response = await fetchApi<{ apps?: AppSummary[] } | AppSummary[]>(
      `/api/apps${query({ project_id: projectId })}`,
    );
    return arrayPayload(response, "apps");
  },

  async get(appId: string, projectId?: string | null): Promise<AppSummary> {
    const response = await fetchApi<{ app: AppSummary } | AppSummary>(
      `/api/apps/${encodeURIComponent(appId)}${query({ project_id: projectId })}`,
    );
    if ("app" in response && response.app) return response.app;
    return response as AppSummary;
  },

  async create(input: CreateAppInput): Promise<AppSummary> {
    const response = await fetchApi<{ app: AppSummary }>("/api/apps", {
      method: "POST",
      body: JSON.stringify(input),
    });
    return response.app;
  },

  async update(
    appId: string,
    input: Partial<CreateAppInput> & { default_target_key?: string | null },
    projectId?: string | null,
  ): Promise<AppSummary> {
    const response = await fetchApi<{ app: AppSummary }>(
      `/api/apps/${encodeURIComponent(appId)}${query({ project_id: projectId })}`,
      { method: "PATCH", body: JSON.stringify(input) },
    );
    return response.app;
  },

  async archive(appId: string, projectId?: string | null): Promise<AppSummary | null> {
    const response = await fetchApi<{ app?: AppSummary; archived?: boolean }>(
      `/api/apps/${encodeURIComponent(appId)}${query({ project_id: projectId })}`,
      { method: "DELETE" },
    );
    return response.app ?? null;
  },

  async getContext(appId: string, projectId?: string | null): Promise<AppContext> {
    return fetchApi<AppContext>(
      `/api/apps/${encodeURIComponent(appId)}/context${query({ project_id: projectId })}`,
    );
  },

  /** Read the immutable Release-bound declarative runtime for normal use. */
  async getRuntime(
    appId: string,
    options: { projectId?: string | null } = {},
  ): Promise<AppRuntimeWireResponse> {
    return fetchApi<AppRuntimeWireResponse>(
      `/api/apps/${encodeURIComponent(appId)}/runtime${query({ project_id: options.projectId })}`,
    );
  },

  async activateRelease(
    appId: string,
    releaseId: string,
  ): Promise<AppRuntimeWireResponse> {
    return fetchApi<AppRuntimeWireResponse>(
      `/api/apps/${encodeURIComponent(appId)}/releases/${encodeURIComponent(releaseId)}/activate`,
      { method: "POST" },
    );
  },

  /**
   * Start one Release-bound Action. Only the opaque Action ID and user input
   * cross this boundary; target, command and Release are server-owned.
   */
  async runRuntimeAction(
    appId: string,
    actionId: string,
    input: Record<string, unknown>,
    options: { idempotencyKey: string; projectId?: string | null },
  ): Promise<AppRuntimeWireActionRunResponse> {
    return fetchApi<AppRuntimeWireActionRunResponse>(
      `/api/apps/${encodeURIComponent(appId)}/actions/${encodeURIComponent(actionId)}/runs${query({ project_id: options.projectId })}`,
      {
        method: "POST",
        headers: { "Idempotency-Key": options.idempotencyKey },
        body: JSON.stringify({ input }),
      },
    );
  },

  async listRuntimeRuns(
    appId: string,
    options: { projectId?: string | null; limit?: number; idempotencyKey?: string | null } = {},
  ): Promise<AppRuntimeWireRunList> {
    return fetchApi<AppRuntimeWireRunList>(
      `/api/apps/${encodeURIComponent(appId)}/runs${query({
        project_id: options.projectId,
        limit: options.limit ? String(options.limit) : undefined,
        idempotency_key: options.idempotencyKey,
      })}`,
    );
  },

  async getRuntimeRun(
    appId: string,
    runId: string,
    options: { projectId?: string | null } = {},
  ): Promise<AppRuntimeWireRun> {
    const response = await fetchApi<AppRuntimeWireActionRunResponse>(
      `/api/apps/${encodeURIComponent(appId)}/runs/${encodeURIComponent(runId)}${query({ project_id: options.projectId })}`,
    );
    // The canonical endpoint always returns { run }.  Unwrap exactly once so
    // repository normalization cannot accidentally treat the envelope as an
    // unknown run with an empty id.
    return response.run;
  },

  async stopRuntimeRun(
    appId: string,
    runId: string,
    options: { projectId?: string | null } = {},
  ): Promise<AppRuntimeWireRun> {
    const response = await fetchApi<AppRuntimeWireActionRunResponse>(
      `/api/apps/${encodeURIComponent(appId)}/runs/${encodeURIComponent(runId)}/stop${query({ project_id: options.projectId })}`,
      { method: "POST" },
    );
    return response.run;
  },

  async getRuntimeRunLogs(
    appId: string,
    runId: string,
    options: { projectId?: string | null } = {},
  ): Promise<AppRuntimeWireLogs> {
    return fetchApi<AppRuntimeWireLogs>(
      `/api/apps/${encodeURIComponent(appId)}/runs/${encodeURIComponent(runId)}/logs${query({ project_id: options.projectId })}`,
    );
  },

  async getRuntimeRunFile(
    appId: string,
    runId: string,
    fileId: string,
    options: { projectId?: string | null } = {},
  ): Promise<AppRuntimeWireFile> {
    return fetchApi<AppRuntimeWireFile>(
      `/api/apps/${encodeURIComponent(appId)}/runs/${encodeURIComponent(runId)}/files/${encodeURIComponent(fileId)}${query({ project_id: options.projectId })}`,
    );
  },

  async listRuntimeRunFiles(
    appId: string,
    runId: string,
    options: { projectId?: string | null } = {},
  ): Promise<AppRuntimeWireFile[]> {
    const response = await fetchApi<AppRuntimeWireFile[] | { files?: AppRuntimeWireFile[] }>(
      `/api/apps/${encodeURIComponent(appId)}/runs/${encodeURIComponent(runId)}/files${query({ project_id: options.projectId })}`,
    );
    return arrayPayload(response, "files");
  },

  async uploadRuntimeInputFile(
    appId: string,
    actionId: string,
    file: AppRuntimeFileAsset,
    options: { projectId?: string | null } = {},
  ): Promise<AppRuntimeInputFile> {
    const formData = new FormData();
    formData.append("file", {
      uri: file.uri,
      name: file.name,
      type: file.mimeType || "application/octet-stream",
    } as unknown as Blob);
    return fetchApi<AppRuntimeInputFile>(
      `/api/apps/${encodeURIComponent(appId)}/actions/${encodeURIComponent(actionId)}/files${query({ project_id: options.projectId })}`,
      { method: "POST", body: formData },
    );
  },

  async downloadRuntimeRunFileBytes(
    appId: string,
    runId: string,
    fileId: string,
    options: { projectId?: string | null } = {},
  ): Promise<ArrayBuffer> {
    return fetchApiBytes(
      `/api/apps/${encodeURIComponent(appId)}/runs/${encodeURIComponent(runId)}/files/${encodeURIComponent(fileId)}/download${query({ project_id: options.projectId })}`,
    );
  },

  async getTargets(appId: string, projectId?: string | null): Promise<AppTarget[]> {
    const response = await fetchApi<{ targets?: AppTarget[] } | AppTarget[]>(
      `/api/apps/${encodeURIComponent(appId)}/targets${query({ project_id: projectId })}`,
    );
    return arrayPayload(response, "targets");
  },

  async getFiles(appId: string, projectId?: string | null): Promise<AppFile[]> {
    const response = await fetchApi<{ files?: AppFile[] } | AppFile[]>(
      `/api/apps/${encodeURIComponent(appId)}/files${query({ project_id: projectId })}`,
    );
    return arrayPayload(response, "files");
  },

  async getFile(
    appId: string,
    path: string,
    projectId?: string | null,
  ): Promise<AppFileContent> {
    return fetchApi<AppFileContent>(
      `/api/apps/${encodeURIComponent(appId)}/files/content${query({
        path,
        project_id: projectId,
      })}`,
    );
  },

  async getReleases(appId: string, projectId?: string | null): Promise<AppRelease[]> {
    const response = await fetchApi<{ releases?: AppRelease[] } | AppRelease[]>(
      `/api/apps/${encodeURIComponent(appId)}/releases${query({ project_id: projectId })}`,
    );
    return arrayPayload(response, "releases");
  },

  async getJobs(appId: string, projectId?: string | null): Promise<AppJob[]> {
    const response = await fetchApi<{ jobs?: AppJob[] } | AppJob[]>(
      `/api/apps/${encodeURIComponent(appId)}/jobs${query({ project_id: projectId })}`,
    );
    return arrayPayload(response, "jobs");
  },

  async startJob(
    appId: string,
    input: {
      target_key: string;
      job_type: AppJobType;
      project_id?: string | null;
      input_json?: Record<string, unknown>;
    },
  ): Promise<AppJob> {
    const response = await fetchApi<{ job: AppJob }>(
      `/api/apps/${encodeURIComponent(appId)}/jobs`,
      { method: "POST", body: JSON.stringify(input) },
    );
    return response.job;
  },

  async stopJob(
    appId: string,
    jobId: string,
    projectId?: string | null,
  ): Promise<AppJob> {
    const response = await fetchApi<{ job: AppJob }>(
      `/api/apps/${encodeURIComponent(appId)}/jobs/${encodeURIComponent(jobId)}/stop${query({
        project_id: projectId,
      })}`,
      { method: "POST", body: JSON.stringify({}) },
    );
    return response.job;
  },

  async getJobLogs(
    appId: string,
    jobId: string,
    projectId?: string | null,
  ): Promise<{ job_id: string; logs: string }> {
    return fetchApi<{ job_id: string; logs: string }>(
      `/api/apps/${encodeURIComponent(appId)}/jobs/${encodeURIComponent(jobId)}/logs${query({
        project_id: projectId,
      })}`,
    );
  },

  async getProjectApps(projectId: string): Promise<ProjectAppBinding[]> {
    const response = await fetchApi<
      { project_id?: string; apps?: ProjectAppBinding[] } | ProjectAppBinding[]
    >(`/api/projects/${encodeURIComponent(projectId)}/apps`);
    return arrayPayload(response, "apps");
  },

  async linkProjectApp(
    projectId: string,
    input: ProjectAppInput,
  ): Promise<ProjectAppBinding> {
    const response = await fetchApi<{ binding: ProjectAppBinding }>(
      `/api/projects/${encodeURIComponent(projectId)}/apps`,
      { method: "POST", body: JSON.stringify(input) },
    );
    return response.binding;
  },

  async updateProjectApp(
    projectId: string,
    appId: string,
    input: Partial<ProjectAppInput>,
  ): Promise<ProjectAppBinding> {
    const response = await fetchApi<{ binding: ProjectAppBinding }>(
      `/api/projects/${encodeURIComponent(projectId)}/apps/${encodeURIComponent(appId)}`,
      { method: "PATCH", body: JSON.stringify(input) },
    );
    return response.binding;
  },

  async unlinkProjectApp(projectId: string, appId: string): Promise<void> {
    await fetchApi(
      `/api/projects/${encodeURIComponent(projectId)}/apps/${encodeURIComponent(appId)}`,
      { method: "DELETE" },
    );
  },

  async listTaskApps(taskId: string): Promise<TaskAppLink[]> {
    const response = await fetchApi<{ task_id?: string; apps?: TaskAppLink[] } | TaskAppLink[]>(
      `/api/tasks/${encodeURIComponent(taskId)}/apps`,
    );
    return arrayPayload(response, "apps");
  },

  async listAppTasks(
    appId: string,
    projectId: string,
    relationType?: string,
  ): Promise<Array<TaskAppLink & { task?: Record<string, unknown> }>> {
    const response = await fetchApi<{
      app_id?: string;
      project_id?: string;
      tasks?: Array<TaskAppLink & { task?: Record<string, unknown> }>;
    }>(
      `/api/apps/${encodeURIComponent(appId)}/tasks${query({
        project_id: projectId,
        relation_type: relationType,
      })}`,
    );
    return response.tasks ?? [];
  },

  async linkTaskApp(taskId: string, input: TaskAppInput): Promise<TaskAppLink> {
    const response = await fetchApi<{ link: TaskAppLink }>(
      `/api/tasks/${encodeURIComponent(taskId)}/apps`,
      { method: "POST", body: JSON.stringify(input) },
    );
    return response.link;
  },

  async unlinkTaskApp(
    taskId: string,
    appId: string,
    options?: { targetId?: string | null; relationType?: string | null },
  ): Promise<void> {
    await fetchApi(
      `/api/tasks/${encodeURIComponent(taskId)}/apps/${encodeURIComponent(appId)}${query({
        target_id: options?.targetId,
        relation_type: options?.relationType,
      })}`,
      { method: "DELETE" },
    );
  },
};
