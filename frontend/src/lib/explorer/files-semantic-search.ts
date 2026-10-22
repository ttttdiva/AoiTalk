import type { components } from "@/lib/api-types.gen";
import {
  mediaSearchApi,
  type MediaSearchRequest,
  type MediaUnifiedHit,
  type MediaUnifiedSearchResponse,
} from "@/lib/media-search-api";

/**
 * Files 画面の「内容（意味）検索」と索引状況。
 *
 * - 検索は統合検索 API（targets=["files"]）を使い、表示中のタブ（User Files /
 *   Project Files）とパス配下へ結果を絞り込む。
 * - 索引状況・設定・再スキャンは /api/search/files/* を使う。
 */

export type FilesIndexStatus = components["schemas"]["FilesIndexStatusResponse"];
export type FilesIndexCounts = components["schemas"]["FilesIndexCounts"];
export type FilesIndexEntry = components["schemas"]["FilesIndexEntry"];
export type FilesIndexEntriesResponse = components["schemas"]["FilesIndexEntriesResponse"];
export type FilesIndexSettings = components["schemas"]["FilesIndexSettingsResponse"];
export type FilesIndexGlobalSettings = components["schemas"]["FilesIndexGlobalSettings"];
export type FilesIndexOwnerRules = components["schemas"]["FilesIndexOwnerRules"];
export type FilesIndexRescanResponse = components["schemas"]["FilesIndexRescanResponse"];
export type FilesLocation = components["schemas"]["FilesLocation"];
type IndexState = FilesIndexEntry["index_state"];

// ─── スコープ ───

export interface FilesSemanticScope {
  scope: "user" | "project";
  /** Project Files の場合のプロジェクト ID。 */
  projectId: string | null;
  /** User Files / Project Files ルートの explorer パス。 */
  rootPath: string;
  /** ルートから見た現在フォルダ（空文字はルート）。 */
  relativeFolder: string;
}

function normalizePath(path: string): string {
  return path.replace(/\\/g, "/").replace(/^\/+|\/+$/g, "");
}

/**
 * explorer パス（`_users/user_<id>/...` / `_projects/project_<id>/...`）から
 * 内容検索のスコープを求める。絶対パス・リモート・ルート外は null。
 */
export function resolveFilesSemanticScope(path: string | null | undefined): FilesSemanticScope | null {
  const normalized = normalizePath(path ?? "");
  const match = normalized.match(/^(_users\/user_|_projects\/project_)([^/]+)(?:\/(.*))?$/);
  if (!match) return null;
  const [, prefix, id, rest] = match;
  const isProject = prefix === "_projects/project_";
  return {
    scope: isProject ? "project" : "user",
    projectId: isProject ? id : null,
    rootPath: `${prefix}${id}`,
    relativeFolder: normalizePath(rest ?? ""),
  };
}

// ─── API ───

const FILES_INDEX_BASE = "/api/python-proxy/search/files";

async function filesIndexRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${FILES_INDEX_BASE}${path}`, {
    credentials: "include",
    cache: "no-store",
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  if (!response.ok) {
    let detail = "";
    try {
      const body = (await response.json()) as { detail?: unknown };
      if (typeof body?.detail === "string") detail = body.detail;
    } catch {
      /* ignore */
    }
    throw new Error(
      response.status === 403
        ? "この操作の権限がありません"
        : response.status === 404
          ? "索引の対象範囲を確認できません"
          : response.status === 422
            ? detail || "入力内容を確認してください"
            : "ファイル索引を利用できません。しばらくしてから再試行してください。",
    );
  }
  return response.json() as Promise<T>;
}

function scopeParams(scope: FilesSemanticScope): URLSearchParams {
  const params = new URLSearchParams({ scope: scope.scope });
  if (scope.projectId) params.set("project_id", scope.projectId);
  return params;
}

export const filesIndexApi = {
  status: (scope: FilesSemanticScope, signal?: AbortSignal) =>
    filesIndexRequest<FilesIndexStatus>(`/status?${scopeParams(scope)}`, { signal }),
  entries: (
    scope: FilesSemanticScope,
    options: { states: IndexState[]; offset?: number; limit?: number },
    signal?: AbortSignal,
  ) => {
    const params = scopeParams(scope);
    for (const state of options.states) params.append("state", state);
    params.set("offset", String(options.offset ?? 0));
    params.set("limit", String(options.limit ?? 100));
    return filesIndexRequest<FilesIndexEntriesResponse>(`/entries?${params}`, { signal });
  },
  settings: (projectId: string | null, signal?: AbortSignal) =>
    filesIndexRequest<FilesIndexSettings>(
      `/settings${projectId ? `?${new URLSearchParams({ project_id: projectId })}` : ""}`,
      { signal },
    ),
  updateSettings: (
    target: "global" | "user" | "project",
    settings: Record<string, unknown>,
    projectId?: string | null,
  ) =>
    filesIndexRequest<FilesIndexSettings>("/settings", {
      method: "PUT",
      body: JSON.stringify({
        target,
        settings,
        ...(target === "project" || (target === "global" && projectId)
          ? { project_id: projectId ?? null }
          : {}),
      }),
    }),
  rescan: (scope: FilesSemanticScope, retryFailed: boolean) =>
    filesIndexRequest<FilesIndexRescanResponse>("/rescan", {
      method: "POST",
      body: JSON.stringify({
        scope: scope.scope,
        project_id: scope.projectId,
        retry_failed: retryFailed,
      }),
    }),
};

// ─── 検索 ───

export const FILES_SEMANTIC_SEARCH_LIMIT = 50;

export function buildFilesSemanticRequest(query: string, scope: FilesSemanticScope): MediaSearchRequest {
  return {
    plan: {
      version: 1,
      instruction: query,
      sources: [],
      mode: "combined",
      // User Files は個人パーティション（プロジェクトなしの現在コンテキスト）、
      // Project Files はそのプロジェクトだけを検索する。
      scope: scope.scope === "project" ? "project" : "current_context",
      targets: ["files"],
      limit: FILES_SEMANTIC_SEARCH_LIMIT,
    },
    project_id: scope.projectId,
    space_id: null,
    query: "",
    image: null,
    limit: FILES_SEMANTIC_SEARCH_LIMIT,
    use_current_attachments: false,
    attachment_indices: null,
    session_id: null,
    message_id: null,
  };
}

export type FilesFileKind = "pdf" | "audio" | "video" | "image" | "office" | "text" | "other";

const AUDIO_EXTS = new Set(["mp3", "wav", "m4a", "aac", "flac", "ogg", "oga", "opus", "wma"]);
const VIDEO_EXTS = new Set(["mp4", "mov", "m4v", "webm", "mkv", "avi", "wmv"]);
const IMAGE_EXTS = new Set(["png", "jpg", "jpeg", "gif", "webp", "bmp", "tif", "tiff", "heic", "svg"]);
const OFFICE_EXTS = new Set(["doc", "docx", "xls", "xlsx", "xlsm", "ppt", "pptx", "odt", "ods", "odp", "rtf"]);

export function filesFileKind(name: string): FilesFileKind {
  const ext = name.includes(".") ? name.split(".").pop()!.toLowerCase() : "";
  if (ext === "pdf") return "pdf";
  if (AUDIO_EXTS.has(ext)) return "audio";
  if (VIDEO_EXTS.has(ext)) return "video";
  if (IMAGE_EXTS.has(ext)) return "image";
  if (OFFICE_EXTS.has(ext)) return "office";
  if (ext) return "text";
  return "other";
}

export function formatMediaSeconds(value: number): string {
  const total = Math.max(0, Math.floor(value));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  const mm = hours > 0 ? String(minutes).padStart(2, "0") : String(minutes);
  return `${hours > 0 ? `${hours}:` : ""}${mm}:${String(seconds).padStart(2, "0")}`;
}

export interface FilesSemanticPosition {
  key: string;
  label: string | null;
  snippet: string;
  score: number;
  pageNumber: number | null;
  startSeconds: number | null;
  endSeconds: number | null;
  originalUrl: string | null;
}

export interface FilesSemanticResult {
  key: string;
  name: string;
  kind: FilesFileKind;
  /** ファイルの explorer パス。 */
  filePath: string;
  /** 親フォルダの explorer パス（Filesで場所を開く移動先）。 */
  folderPath: string;
  /** ルートからの親フォルダ（表示用、空文字はルート）。 */
  folderLabel: string;
  location: FilesLocation;
  best: FilesSemanticPosition;
  /** 同じファイルの他の該当箇所（スコア順）。 */
  morePositions: FilesSemanticPosition[];
  /** 同じ原本を持つ別の場所（チャット添付など）。 */
  otherLocations: string[];
}

function positionLabel(hit: MediaUnifiedHit): string | null {
  if (hit.page_number != null && hit.page_number > 0) return `${hit.page_number}ページ`;
  if (hit.start_seconds != null && Number.isFinite(hit.start_seconds)) {
    const end = hit.end_seconds;
    return end != null && Number.isFinite(end) && end > hit.start_seconds
      ? `${formatMediaSeconds(hit.start_seconds)}–${formatMediaSeconds(end)}`
      : formatMediaSeconds(hit.start_seconds);
  }
  return null;
}

/** 原本のバイト列（ACL・内容ハッシュ確認付き）を開く URL。PDF は #page、音声/動画は #t。 */
export function filesOriginalUrl(
  hit: Pick<MediaUnifiedHit, "unit_id" | "page_number" | "start_seconds" | "end_seconds">,
  projectId: string | null,
): string | null {
  if (!hit.unit_id) return null;
  const params = projectId ? `?${new URLSearchParams({ project_id: projectId })}` : "";
  let fragment = "";
  if (hit.page_number != null && hit.page_number > 0) {
    fragment = `#page=${hit.page_number}`;
  } else if (hit.start_seconds != null && Number.isFinite(hit.start_seconds) && hit.start_seconds >= 0) {
    const end = hit.end_seconds;
    fragment = `#t=${hit.start_seconds}${end != null && Number.isFinite(end) && end > hit.start_seconds ? `,${end}` : ""}`;
  }
  return `/api/python-proxy/search/media/${encodeURIComponent(hit.unit_id)}/content${params}${fragment}`;
}

const OTHER_LOCATION_LABELS: Record<string, string> = {
  chat_attachment: "チャット添付",
  task_attachment: "タスク添付",
  docs_attachment: "Docs添付",
  knowledge_document: "Knowledge",
  meeting_audio: "会議音声",
  generated_media: "生成物",
  generation_output: "生成物",
  conversation_message: "会話",
  workspace_file: "ファイル",
};

function otherLocationLabel(location: NonNullable<MediaUnifiedHit["other_locations"]>[number]): string {
  const kind = (location.source_kind && OTHER_LOCATION_LABELS[location.source_kind]) || location.target_kind;
  return location.title ? `${kind}: ${location.title}` : kind;
}

function hitMatchesScope(location: FilesLocation, scope: FilesSemanticScope, folder: string): boolean {
  if (location.scope !== scope.scope) return false;
  if (!normalizePath(location.path).startsWith(`${normalizePath(scope.rootPath)}/`)) return false;
  if (scope.scope === "project" && location.project_id !== scope.projectId) return false;
  if (!folder) return true;
  const relative = normalizePath(location.relative_path);
  return relative.startsWith(`${folder}/`);
}

/**
 * 統合検索の結果を Files の表示用に変換する。
 * 表示中のタブ（scope / project）とフォルダ配下へ絞り込み、同一ファイルの
 * 複数ヒットは最上位を代表にして他の該当箇所へまとめる。
 */
export function toFilesSemanticResults(
  response: Pick<MediaUnifiedSearchResponse, "results">,
  scope: FilesSemanticScope,
  options: { underFolder: boolean },
): FilesSemanticResult[] {
  const folder = options.underFolder ? scope.relativeFolder : "";
  const grouped = new Map<string, FilesSemanticResult>();
  for (const hit of response.results) {
    const location = hit.files_location;
    if (hit.target_kind !== "files" || !location) continue;
    if (!hitMatchesScope(location, scope, folder)) continue;
    const filePath = normalizePath(location.path);
    const relative = normalizePath(location.relative_path);
    const slash = relative.lastIndexOf("/");
    const name = relative.slice(slash + 1) || hit.title;
    const folderLabel = slash >= 0 ? relative.slice(0, slash) : "";
    const pathSlash = filePath.lastIndexOf("/");
    const position: FilesSemanticPosition = {
      key: JSON.stringify([hit.unit_id, hit.page_number, hit.start_seconds, hit.text_start]),
      label: positionLabel(hit),
      snippet: (hit.text ?? "").replace(/\s+/g, " ").trim(),
      score: hit.score ?? 0,
      pageNumber: hit.page_number ?? null,
      startSeconds: hit.start_seconds ?? null,
      endSeconds: hit.end_seconds ?? null,
      originalUrl: filesOriginalUrl(hit, scope.projectId),
    };
    const existing = grouped.get(filePath);
    if (existing) {
      if (position.score > existing.best.score) {
        existing.morePositions.push(existing.best);
        existing.best = position;
      } else {
        existing.morePositions.push(position);
      }
      for (const other of hit.other_locations ?? []) {
        const label = otherLocationLabel(other);
        if (!existing.otherLocations.includes(label)) existing.otherLocations.push(label);
      }
      continue;
    }
    grouped.set(filePath, {
      key: filePath,
      name,
      kind: filesFileKind(name),
      filePath,
      folderPath: pathSlash >= 0 ? filePath.slice(0, pathSlash) : scope.rootPath,
      folderLabel,
      location,
      best: position,
      morePositions: [],
      otherLocations: Array.from(new Set((hit.other_locations ?? []).map(otherLocationLabel))),
    });
  }
  const results = Array.from(grouped.values());
  for (const result of results) result.morePositions.sort((a, b) => b.score - a.score);
  return results.sort((a, b) => b.best.score - a.best.score);
}

export async function searchFilesSemantic(
  query: string,
  scope: FilesSemanticScope,
  options: { underFolder: boolean },
  signal?: AbortSignal,
): Promise<{ response: MediaUnifiedSearchResponse; results: FilesSemanticResult[] }> {
  const response = await mediaSearchApi.unified(buildFilesSemanticRequest(query, scope), signal);
  return { response, results: toFilesSemanticResults(response, scope, options) };
}

// ─── 索引状況と0件判定 ───

export interface FilesIndexCoverage {
  total: number;
  indexable: number;
  ready: number;
  /** 未処理（待機・処理中・ハッシュ待ち）の件数。 */
  waiting: number;
  failed: number;
  excluded: number;
  /** ready / indexable（0〜100、indexable=0 のときは null）。 */
  percent: number | null;
  /** 待機・処理中・スキャン中のいずれかがある。 */
  inProgress: boolean;
}

export function filesIndexCoverage(status: Pick<FilesIndexStatus, "counts" | "queue_waiting" | "scanner_running">): FilesIndexCoverage {
  const counts = status.counts;
  const total = counts.total ?? 0;
  const indexable = counts.indexable ?? 0;
  const ready = counts.ready ?? 0;
  const waiting = (counts.pending ?? 0) + (counts.running ?? 0) + (counts.pending_hash ?? 0);
  const failed = counts.failed ?? 0;
  const excluded = (counts.excluded ?? 0) + (counts.unsupported ?? 0);
  return {
    total,
    indexable,
    ready,
    waiting,
    failed,
    excluded,
    percent: indexable > 0 ? Math.floor((ready / indexable) * 100) : null,
    inProgress: waiting > 0 || Boolean(status.scanner_running),
  };
}

export type FilesSemanticOutcomeKind =
  | "results"
  | "no_match"
  | "no_match_partial"
  | "limited_results"
  | "indexing"
  | "not_indexed"
  | "no_files"
  | "all_excluded"
  | "index_disabled"
  | "semantic_unavailable"
  | "search_failed"
  | "status_unknown";

export interface FilesSemanticOutcome {
  kind: FilesSemanticOutcomeKind;
  title: string;
  detail: string | null;
  /** 文字列検索への切り替えを案内する。 */
  suggestStringSearch: boolean;
}

function filesLaneFailed(response: Pick<MediaUnifiedSearchResponse, "lane_status" | "complete"> | null): boolean {
  if (!response) return false;
  const lane = response.lane_status.find((status) => status.target_kind === "files");
  return !response.complete || (lane != null && lane.status !== "complete");
}

/**
 * 内容検索の結果を判定する。0件のとき「該当資料なし」と
 * 「未索引・索引中・embedding未準備（文字列検索へ縮退）」を区別する。
 */
export function judgeFilesSemanticOutcome(input: {
  status: FilesIndexStatus | null;
  response: Pick<MediaUnifiedSearchResponse, "lane_status" | "complete"> | null;
  resultCount: number;
  /** A folder filter was applied after the server-wide top-k limit. */
  limitedToTopResults?: boolean;
}): FilesSemanticOutcome {
  const { status, response, resultCount, limitedToTopResults = false } = input;
  const coverage = status ? filesIndexCoverage(status) : null;
  const percentText = coverage?.percent != null ? `索引完了 ${coverage.percent}%（${coverage.ready}/${coverage.indexable}件）` : null;
  const laneFailed = filesLaneFailed(response);
  const limitedDetail = limitedToTopResults
    ? `検索全体の上位${FILES_SEMANTIC_SEARCH_LIMIT}件から現在のフォルダ内に絞り込んでいます。ほかの該当ファイルが含まれない場合があります。フォルダの絞り込みを解除するか、文字列検索も試してください。`
    : null;

  if (status && !status.semantic_available) {
    return {
      kind: "semantic_unavailable",
      title: "意味検索の準備ができていません",
      detail: "埋め込みモデル（EmbeddingGemma 2）が未準備のため、内容による検索を利用できません。名前・本文の文字列検索で探してください。",
      suggestStringSearch: true,
    };
  }
  if (laneFailed) {
    return {
      kind: "search_failed",
      title: "内容検索を完了できませんでした",
      detail: "ファイル索引の検索に失敗しました。時間をおいて再試行するか、文字列検索で探してください。",
      suggestStringSearch: true,
    };
  }
  if (resultCount > 0) {
    const partial = coverage && (coverage.inProgress || coverage.failed > 0);
    return {
      kind: "results",
      title: `${resultCount}件`,
      detail: [
        partial
          ? `${percentText ?? "索引中"}${coverage.failed > 0 ? `・失敗 ${coverage.failed}件` : ""}。未索引のファイルは結果に含まれません。`
          : null,
        limitedDetail,
      ].filter(Boolean).join(" ") || null,
      suggestStringSearch: false,
    };
  }
  if (limitedToTopResults) {
    return {
      kind: "limited_results",
      title: "検索候補の範囲では見つかりませんでした",
      detail: limitedDetail,
      suggestStringSearch: true,
    };
  }
  if (!status || !coverage) {
    return {
      kind: "status_unknown",
      title: "該当する資料は見つかりませんでした",
      detail: "索引状況を確認できないため、未索引のファイルがある可能性があります。文字列検索も試してください。",
      suggestStringSearch: true,
    };
  }
  if (!status.enabled) {
    return {
      kind: "index_disabled",
      title: "この範囲の内容索引は無効です",
      detail: "索引設定で有効にすると内容で検索できます。いまは名前・本文の文字列検索で探してください。",
      suggestStringSearch: true,
    };
  }
  if (coverage.total === 0) {
    return {
      kind: "no_files",
      title: coverage.inProgress ? "ファイルを確認しています" : "索引対象のファイルがありません",
      detail: coverage.inProgress
        ? "スキャン中です。完了後にもう一度検索してください。"
        : "この範囲にはまだ索引されたファイルがありません。",
      suggestStringSearch: true,
    };
  }
  if (coverage.indexable === 0) {
    return {
      kind: "all_excluded",
      title: "索引できるファイルがありません",
      detail: `除外・非対応 ${coverage.excluded}件。除外理由は索引状況の詳細で確認できます。文字列検索も利用できます。`,
      suggestStringSearch: true,
    };
  }
  if (coverage.ready === 0) {
    return {
      kind: "not_indexed",
      title: coverage.inProgress ? "まだ索引を作成中です" : "まだ索引されていません",
      detail: `${percentText ? `${percentText}。` : ""}索引が完了するまでは文字列検索で探してください。`,
      suggestStringSearch: true,
    };
  }
  if (coverage.inProgress) {
    return {
      kind: "indexing",
      title: "索引済みの範囲では見つかりませんでした",
      detail: `${percentText}・待機 ${coverage.waiting}件。未索引のファイルに該当がある可能性があります。文字列検索も試してください。`,
      suggestStringSearch: true,
    };
  }
  if (coverage.failed > 0) {
    return {
      kind: "no_match_partial",
      title: "該当する資料は見つかりませんでした",
      detail: `ただし ${coverage.failed}件のファイルは索引に失敗しているため検索対象外です。詳細から再試行できます。`,
      suggestStringSearch: true,
    };
  }
  return {
    kind: "no_match",
    title: "該当する資料は見つかりませんでした",
    detail: `${percentText}。言い換えると見つかる場合があります。`,
    suggestStringSearch: false,
  };
}

// ─── 表示ラベル ───

const EXCLUDED_REASON_LABELS: Record<string, string> = {
  scope_disabled: "索引が無効",
  hidden: "隠しファイル・フォルダ",
  outside_include_roots: "対象フォルダ外",
  unsupported_format: "非対応の形式",
  empty_file: "空のファイル",
  size_limit: "サイズ上限超過",
  text_size_limit: "テキストのサイズ上限超過",
  media_size_limit: "音声・動画のサイズ上限超過",
  duration_limit: "音声・動画の長さ上限超過",
  unsafe_path_name: "扱えないファイル名",
  project_deleted: "プロジェクト削除済み",
  link: "リンク",
};

export function filesExcludedReasonLabel(reason: string | null | undefined): string {
  if (!reason) return "理由不明";
  if (reason.startsWith("pattern:")) return `除外パターン ${reason.slice("pattern:".length)}`;
  return EXCLUDED_REASON_LABELS[reason] ?? reason;
}

export function formatIndexTimestamp(value: string | null | undefined): string {
  if (!value) return "未実行";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("ja-JP", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

/** 改行区切りのテキストエリア値をリストへ変換する（空行は除く）。 */
export function parseLines(value: string): string[] {
  return value
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);
}
