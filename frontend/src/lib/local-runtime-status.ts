export type LocalRuntimeStartupProgress = {
  phase?: string | null;
  elapsed_s?: number | null;
};

/**
 * Read-only projection returned by `/llm/local-runtime/status`.
 *
 * The endpoint intentionally keeps runtime-specific details behind a small
 * public shape. Keep this type permissive because older servers omit some of
 * the readiness fields and newer runtimes may add a status projection.
 */
export type LocalRuntimeStatusSnapshot = {
  task_id?: string | null;
  model?: string | null;
  runtime?: string | null;
  managed?: boolean;
  phase?: string | null;
  status?: string | null;
  done?: boolean;
  error?: string | null;
  reason?: string | null;
  runtime_installed?: boolean;
  model_installed?: boolean;
  prepared?: boolean;
  runtime_install?: {
    status?: string | null;
    installed?: boolean;
  } | null;
  model_artifact?: {
    status?: string | null;
    installed?: boolean;
  } | null;
  server?: {
    status?: string | null;
    ready?: boolean | null;
    running?: boolean;
    auto_start_on_send?: boolean;
    startup_progress?: LocalRuntimeStartupProgress | null;
  } | null;
};

export type LocalRuntimeReadiness =
  | "loading"
  | "ready"
  | "failed"
  | "prepared"
  | "waiting"
  | "unknown";

export type LocalRuntimeReadinessView = {
  state: LocalRuntimeReadiness;
  label: string;
  detail?: string;
};

const MANAGED_RUNTIMES = new Set(["llama_cpp", "freetoken", "strata"]);
const FAILURE_STATES = new Set([
  "failed",
  "runtime_missing",
  "model_missing",
  "manual_model_required",
  "installer_required",
  "unsupported",
  "cancelled",
  "cancelling",
]);
const LOADING_STATES = new Set([
  "queued",
  "running",
  "preparing",
  "loading",
  "model",
  "verify",
]);

function normalized(value: unknown): string {
  return typeof value === "string" ? value.trim().toLowerCase() : "";
}

function validStartupProgress(
  value: LocalRuntimeStartupProgress | null | undefined,
): LocalRuntimeStartupProgress | null {
  if (!value || typeof value !== "object") return null;
  const phase = normalized(value.phase);
  const elapsed = value.elapsed_s;
  if (
    !["loading", "reading_weights", "allocating_cache", "initializing"].includes(phase) ||
    typeof elapsed !== "number" ||
    !Number.isFinite(elapsed) ||
    elapsed < 0
  ) {
    return null;
  }
  return { phase, elapsed_s: elapsed };
}

function isManagedSnapshot(snapshot: LocalRuntimeStatusSnapshot): boolean {
  if (snapshot.managed === false) return false;
  const runtime = normalized(snapshot.runtime);
  return snapshot.managed === true || MANAGED_RUNTIMES.has(runtime);
}

function failureReason(snapshot: LocalRuntimeStatusSnapshot): string | null {
  const status = normalized(snapshot.status);
  const runtimeStatus = normalized(snapshot.runtime_install?.status);
  const artifactStatus = normalized(snapshot.model_artifact?.status);
  if (FAILURE_STATES.has(status) || FAILURE_STATES.has(runtimeStatus) || FAILURE_STATES.has(artifactStatus)) {
    return snapshot.reason?.trim() || snapshot.error?.trim() || "ローカルruntimeの準備に失敗しました";
  }
  if (snapshot.error?.trim()) return snapshot.error.trim();
  const serverStatus = normalized(snapshot.server?.status);
  if (["error", "failed"].includes(serverStatus)) {
    return snapshot.reason?.trim() || "ローカルモデルの状態を確認できません";
  }
  return null;
}

/**
 * Convert the server projection into user-facing readiness without treating
 * artifact preparation (`prepared`) as a loaded model. `server.ready === true`
 * is the only positive proof that the selected model can accept a turn.
 */
export function localRuntimeReadinessView(
  snapshot: LocalRuntimeStatusSnapshot | null | undefined,
  requestError?: string | null,
): LocalRuntimeReadinessView | null {
  if (requestError?.trim()) {
    return { state: "unknown", label: "状態確認に失敗", detail: requestError.trim() };
  }
  if (!snapshot || !isManagedSnapshot(snapshot)) return null;

  const failure = failureReason(snapshot);
  if (failure) return { state: "failed", label: "失敗", detail: failure };

  const server = snapshot.server;
  if (server?.ready === true) {
    return { state: "ready", label: "準備完了", detail: "選択したモデルが応答可能です" };
  }

  const progress = validStartupProgress(server?.startup_progress);
  const status = normalized(snapshot.status);
  const serverStatus = normalized(server?.status);
  if (
    progress ||
    LOADING_STATES.has(status) ||
    ["loading", "starting", "initializing"].includes(serverStatus) ||
    snapshot.done === false ||
    (snapshot.done !== true && Boolean(snapshot.task_id))
  ) {
    const detail = progress
      ? `${progress.phase} · 経過 ${progress.elapsed_s}秒`
      : undefined;
    return { state: "loading", label: "ロード中", detail };
  }

  // Prepared artifacts plus an idle auto-start runtime are safe to use, but
  // the model is not loaded yet. Keep this distinct from `ready` so the UI
  // never reports a false positive before the first turn's load completes.
  if (snapshot.prepared === true && server?.auto_start_on_send === true) {
    return { state: "waiting", label: "ロード待ち", detail: "送信時にモデルをロードします" };
  }
  if (snapshot.prepared === true) {
    return { state: "prepared", label: "準備済み", detail: "モデルのロード状態を確認中です" };
  }

  return { state: "unknown", label: "準備状態を確認中", detail: snapshot.reason?.trim() || undefined };
}

export function isManagedLocalRuntime(value: unknown): boolean {
  return MANAGED_RUNTIMES.has(normalized(value));
}

export function displayModelOptionLabel(option: {
  id: string;
  label?: string;
  installed?: boolean;
  source_label?: string;
}): string {
  const base = option.label?.trim() || option.id;
  const source = option.installed
    ? option.source_label?.trim() || "インストール済み"
    : "";
  if (!source || base.includes(source)) return base;
  return `${base} · ${source}`;
}
