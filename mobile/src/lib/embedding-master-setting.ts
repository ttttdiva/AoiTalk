/** PATCH /api/settings の key。値は boolean のみ（文字列の真偽値は送らない）。 */
export const EMBEDDING_ENABLED_SETTING_KEY = "search.embedding_enabled";

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

export type EmbeddingEnabledRead = { ok: true; enabled: boolean } | { ok: false };

/**
 * GET /api/settings の本文から settings.search.embedding_enabled を読む。
 * キー（または search セクション）が無い場合だけ既定の true とみなす。
 * boolean 以外（null・文字列など）は推測せず不正として扱い、呼び出し側は未確定表示にする。
 */
export function readEmbeddingEnabled(body: unknown): EmbeddingEnabledRead {
  const settings = isRecord(body) ? body.settings : undefined;
  if (!isRecord(settings)) return { ok: false };
  const search = settings.search;
  if (search === undefined) return { ok: true, enabled: true };
  if (!isRecord(search)) return { ok: false };
  const value = search.embedding_enabled;
  if (value === undefined) return { ok: true, enabled: true };
  return typeof value === "boolean" ? { ok: true, enabled: value } : { ok: false };
}

/** PATCH /api/settings が要求値を永続化したことを示す応答か。 */
export function isSavedEmbeddingEnabled(body: unknown, requested: boolean): boolean {
  return isRecord(body)
    && body.success === true
    && body.key === EMBEDDING_ENABLED_SETTING_KEY
    && body.persisted === true
    && body.value === requested;
}

/** FastAPI の detail（文字列・{message}・検証エラー配列）から表示文を取り出す。 */
export function apiErrorDetail(body: unknown): string | null {
  if (!isRecord(body)) return null;
  const detail = body.detail ?? body.message;
  if (typeof detail === "string" && detail.trim()) return detail;
  if (isRecord(detail) && typeof detail.message === "string" && detail.message.trim()) return detail.message;
  if (Array.isArray(detail)) {
    const messages = detail
      .map((item) => (isRecord(item) && typeof item.msg === "string" ? item.msg : ""))
      .filter(Boolean);
    if (messages.length) return messages.join(" / ");
  }
  return null;
}

export type EmbeddingMasterPhase =
  | "unknown"
  | "on"
  | "off_checking"
  | "off_draining"
  | "off_unloading"
  | "off_released";

/** embeddingMasterPhase が参照するランタイム状態の最小形（EmbeddingRuntimeStatus の部分集合）。 */
export interface EmbeddingPhaseStatus {
  state: string;
  pending_requests: number;
  active_requests: number;
  queued_requests: number;
  process_running: boolean;
  loaded: boolean;
}

/**
 * 保存済みの設定値とランタイム状態から、OFF の進捗を導く。
 * 「解放済み」はワーカープロセスが無く未処理要求も無いと観測できた場合に限り、
 * 設定を保存した直後に解放済みとは表示しない。
 */
export function embeddingMasterPhase(
  enabled: boolean | null,
  status: EmbeddingPhaseStatus | null,
  statusLoaded: boolean,
): EmbeddingMasterPhase {
  if (enabled === null) return "unknown";
  if (enabled) return "on";
  if (!statusLoaded) return "off_checking";
  if (!status) return "off_released";
  const outstanding = status.pending_requests > 0 || status.active_requests > 0
    || status.queued_requests > 0 || status.state === "busy" || status.state === "loading";
  if (outstanding) return "off_draining";
  const resident = status.process_running || status.loaded
    || status.state === "loaded" || status.state === "releasing";
  return resident ? "off_unloading" : "off_released";
}

/**
 * ApiHttpError（status と responseBody 文字列を持つ）から FastAPI の detail を取り出す。
 * JSON でない本文や detail の無い本文は null を返し、呼び出し側が元のエラー文を使う。
 */
export function apiHttpErrorDetail(error: unknown): string | null {
  if (!(error instanceof Error)) return null;
  const responseBody = (error as Error & { responseBody?: unknown }).responseBody;
  if (typeof responseBody !== "string") return null;
  try {
    return apiErrorDetail(JSON.parse(responseBody));
  } catch {
    return null;
  }
}
