"use client";

import {
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import type { DialogRoot } from "@base-ui/react/dialog";
import { AlertTriangle, CheckCircle2, CircleHelp, Loader2, X } from "lucide-react";
import {
  Sheet,
  SheetContent,
} from "@/components/ui/sheet";
import {
  readCachedSnapshot,
  writeCachedSnapshot,
} from "@/lib/persistent-cache";
import { useCurrentUserId } from "@/components/providers/swr-global-provider";
import { apiFetch } from "./docs-utils";
import {
  type DocsClipIngestSubmission,
  type DocsClipIngestResult,
} from "./docs-clip-ingest-dialog";
import { DocsClipIngestDetailDialog } from "./docs-clip-ingest-detail-dialog";

export type DocsClipIngestJob = {
  id: string;
  source: string;
  /** POST/Retry の結果を一覧で照合するための相関キー。 */
  idempotency_key?: string;
  /** 明示指定したDocs node。旧履歴には存在しないためnull扱い。 */
  targetNodeId?: string | null;
  /** サーバー上の短命staging IDだけを保持する。File/Blobは決して入れない。 */
  upload_ids?: string[];
  skip_image_recognition?: boolean;
  /** 今回の取り込みで外部追加調査を許可したか。旧履歴ではtrue扱い。 */
  enable_external_research?: boolean;
  /** サーバーが返した安全なLLMエラーコード。 */
  code?: string;
  /** サーバーが返した再試行可否。旧履歴では未定義のまま従来挙動を維持する。 */
  retryable?: boolean;
  /** サーバーが返した相関追跡ID。 */
  trace_id?: string;
  /** サーバーが永続化したreceiptのID。旧履歴には存在しない。 */
  receipt_id?: string;
  /** server は durable API の状態、legacy は旧IDB履歴の状態。 */
  origin?: "server" | "legacy";
  createdAt: string;
  status: "queued" | "running" | "success" | "failure" | "interrupted";
  result: DocsClipIngestResult | null;
  error: string;
};

const STATUS_LABELS: Record<DocsClipIngestJob["status"], string> = {
  queued: "待機中",
  running: "取り込み中",
  success: "完了",
  failure: "失敗",
  interrupted: "結果不明",
};

export const DOCS_CLIP_INGEST_HISTORY_LIMIT = 50;
export const DOCS_CLIP_INGEST_HISTORY_KEY = "docs/clip-ingest/history";
export const DOCS_CLIP_INGEST_POLL_INTERVAL_MS = 1_000;
export const DOCS_CLIP_INGEST_RECONCILIATION_INTERVAL_MS = 1_000;
export const DOCS_CLIP_INGEST_RECONCILIATION_MAX_ATTEMPTS = 10;
export const DOCS_CLIP_INGEST_JOBS_PATH = "/api/docs/ingest/jobs";
export const DOCS_CLIP_INGEST_CONNECTION_ERROR =
  "接続できません。状態を再取得します。";
const DOCS_CLIP_INGEST_HTTP_ERROR_PREFIX = "取り込み要求が拒否されました";
const DOCS_CLIP_INGEST_HTTP_ERROR_MAX_LENGTH = 500;
const INTERRUPTED_ERROR =
  "ページの再読み込みなどにより、前回の取り込み結果を確認できませんでした。Docsで結果を確認してから再試行してください。";

let idempotencyFallbackCounter = 0;

function createIdempotencyKey(): string {
  try {
    const randomUUID = globalThis.crypto?.randomUUID;
    if (typeof randomUUID === "function") {
      const value = randomUUID.call(globalThis.crypto);
      if (typeof value === "string" && value.trim()) return value;
    }
  } catch {
    // Fall through to a local key when the browser crypto implementation is unavailable.
  }
  idempotencyFallbackCounter += 1;
  return `docs-clip-ingest-${Date.now().toString(36)}-${idempotencyFallbackCounter.toString(36)}-${Math.random().toString(36).slice(2)}`;
}

function isJobStatus(value: unknown): value is DocsClipIngestJob["status"] {
  return value === "queued"
    || value === "running"
    || value === "success"
    || value === "failure"
    || value === "interrupted";
}

function stringArray(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string")
    : [];
}

function normalizeUploadIds(value: unknown): string[] {
  return Array.from(new Set(stringArray(value).map((item) => item.trim()).filter(Boolean)));
}

function normalizeTargetNodeId(value: unknown): string | null {
  return typeof value === "string" && value.trim().length > 0 ? value.trim() : null;
}

function normalizeErrorMetadataString(value: unknown): string | undefined {
  return typeof value === "string" && value.trim().length > 0 ? value.trim() : undefined;
}

function objectRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

/** apiFetch attaches a numeric status to confirmed HTTP failures. */
function isTransportFailure(value: unknown): boolean {
  return typeof objectRecord(value)?.status !== "number";
}

/**
 * Keep confirmed HTTP failures separate from transport failures.  apiFetch
 * already reduces the response body to a safe scalar message; cap it again
 * before rendering so an upstream validation response cannot flood the panel.
 */
function formatHttpError(value: unknown): string {
  const record = objectRecord(value);
  const status = typeof record?.status === "number" ? record.status : null;
  const rawMessage = value instanceof Error ? value.message : "サーバーが取り込み要求を拒否しました。";
  const message = rawMessage.trim().slice(0, DOCS_CLIP_INGEST_HTTP_ERROR_MAX_LENGTH);
  if (status === null) return message || DOCS_CLIP_INGEST_HTTP_ERROR_PREFIX;
  return `${DOCS_CLIP_INGEST_HTTP_ERROR_PREFIX}（HTTP ${status}）${message ? `: ${message}` : "。"}`;
}

function normalizeReceiptId(value: unknown): string | undefined {
  return typeof value === "string" && value.trim().length > 0 ? value.trim() : undefined;
}

function normalizeIdempotencyKey(value: unknown): string | undefined {
  return typeof value === "string" && value.trim().length > 0 ? value.trim() : undefined;
}

function responseReceiptId(value: unknown): string | undefined {
  if (!value || typeof value !== "object" || Array.isArray(value)) return undefined;
  const record = value as Record<string, unknown>;
  const result = record.result && typeof record.result === "object" && !Array.isArray(record.result)
    ? record.result as Record<string, unknown>
    : null;
  return normalizeReceiptId(
    record.receipt_id
      ?? record.receiptId
      ?? result?.receipt_id
      ?? result?.receiptId
      ?? (record.receipt && typeof record.receipt === "object" && !Array.isArray(record.receipt)
        ? (record.receipt as Record<string, unknown>).id
        : undefined),
  );
}

function normalizeRetryable(value: unknown): boolean | undefined {
  return typeof value === "boolean" ? value : undefined;
}

function normalizeResult(value: unknown): DocsClipIngestResult | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const record = value as Record<string, unknown>;
  if (
    typeof record.target_id !== "string"
    || typeof record.target_label !== "string"
    || !["create", "append", "duplicate_skip"].includes(String(record.action))
    || typeof record.open_node_id !== "string"
    || typeof record.open_node_title !== "string"
  ) return null;
  const failedUrls = Array.isArray(record.failed_urls)
    ? record.failed_urls.flatMap((item): Array<{ url?: string; error?: string; acquisition_status?: string }> => {
        if (!item || typeof item !== "object" || Array.isArray(item)) return [];
        const failed = item as Record<string, unknown>;
        return [{
          ...(typeof failed.url === "string" ? { url: failed.url } : {}),
          ...(typeof failed.error === "string" ? { error: failed.error } : {}),
          ...(typeof failed.acquisition_status === "string"
            ? { acquisition_status: failed.acquisition_status }
            : {}),
        }];
      })
    : [];
  return {
    target_id: record.target_id,
    target_label: record.target_label,
    action: record.action as DocsClipIngestResult["action"],
    changed_node_id: typeof record.changed_node_id === "string"
      ? record.changed_node_id
      : null,
    changed_node_title: typeof record.changed_node_title === "string"
      ? record.changed_node_title
      : null,
    open_node_id: record.open_node_id,
    open_node_title: record.open_node_title,
    direct_urls: stringArray(record.direct_urls),
    supplemental_urls: stringArray(record.supplemental_urls),
    failed_urls: failedUrls,
    used_urls: stringArray(record.used_urls),
    unconfirmed: stringArray(record.unconfirmed),
  };
}

type ServerJobStatus = "queued" | "running" | "success" | "failure";

function normalizeServerStatus(value: unknown): ServerJobStatus | null {
  if (value === "queued" || value === "pending") return "queued";
  if (value === "running" || value === "processing" || value === "in_progress") {
    return "running";
  }
  if (
    value === "success"
    || value === "succeeded"
    || value === "complete"
    || value === "completed"
  ) return "success";
  if (value === "failure" || value === "failed" || value === "error") return "failure";
  return null;
}

function serverJobRecord(value: unknown): Record<string, unknown> | null {
  const record = objectRecord(value);
  if (!record) return null;
  return objectRecord(record.job) ?? record;
}

function normalizeServerCreatedAt(record: Record<string, unknown>): string {
  const value = record.created_at ?? record.createdAt ?? record.created ?? record.updated_at;
  return typeof value === "string" && !Number.isNaN(Date.parse(value))
    ? value
    : new Date().toISOString();
}

function normalizeServerError(record: Record<string, unknown>): string {
  const nested = objectRecord(record.error);
  const value = nested?.message ?? record.error_message ?? record.message ?? record.error;
  return typeof value === "string" ? value : "";
}

function serverRequest(
  record: Record<string, unknown>,
  fallback?: DocsClipIngestRequest,
): DocsClipIngestRequest {
  const request = objectRecord(record.request)
    ?? objectRecord(record.request_json)
    ?? objectRecord(record.request_snapshot)
    ?? {};
  const source = typeof record.source === "string"
    ? record.source
    : typeof request.source === "string"
      ? request.source
      : fallback?.source ?? "";
  const targetNodeId = normalizeTargetNodeId(
    record.target_node_id
      ?? record.targetNodeId
      ?? request.target_node_id
      ?? request.targetNodeId
      ?? fallback?.target_node_id
      ?? fallback?.targetNodeId,
  );
  const uploadIds = normalizeUploadIds(
    record.upload_ids
      ?? record.uploadIds
      ?? request.upload_ids
      ?? fallback?.upload_ids,
  );
  return {
    source,
    ...(targetNodeId ? { target_node_id: targetNodeId } : {}),
    upload_ids: uploadIds,
    skip_image_recognition: record.skip_image_recognition === true
      || request.skip_image_recognition === true
      || fallback?.skip_image_recognition === true,
    enable_external_research: record.enable_external_research !== false
      && request.enable_external_research !== false
      && fallback?.enable_external_research !== false,
  };
}

/**
 * Normalize the durable API shape. A success without a server id or a
 * result/receipt is malformed and must never be rendered as success.
 */
export function normalizeDocsClipIngestServerJob(
  value: unknown,
  fallback?: DocsClipIngestRequest,
): DocsClipIngestJob | null {
  const record = serverJobRecord(value);
  if (!record) return null;
  const id = normalizeErrorMetadataString(record.job_id ?? record.jobId ?? record.id);
  const status = normalizeServerStatus(record.status);
  if (!id || !status) return null;
  const idempotencyKey = normalizeIdempotencyKey(
    record.idempotency_key ?? record.idempotencyKey,
  );
  const request = serverRequest(record, fallback);
  const result = normalizeResult(
    record.result ?? record.result_json ?? record.result_snapshot ?? record.terminal_result,
  );
  const receiptId = responseReceiptId(record)
    ?? normalizeReceiptId(record.receipt_id ?? record.receiptId);
  if (status === "success" && !result && !receiptId) return null;
  const errorRecord = objectRecord(record.error);
  const code = normalizeErrorMetadataString(record.code ?? errorRecord?.code);
  const retryable = normalizeRetryable(record.retryable ?? errorRecord?.retryable);
  const traceId = normalizeErrorMetadataString(record.trace_id ?? errorRecord?.trace_id);
  return {
    id,
    origin: "server",
    source: request.source,
    ...(idempotencyKey ? { idempotency_key: idempotencyKey } : {}),
    targetNodeId: normalizeTargetNodeId(request.target_node_id),
    upload_ids: request.upload_ids ?? [],
    skip_image_recognition: request.skip_image_recognition === true,
    enable_external_research: request.enable_external_research !== false,
    ...(code ? { code } : {}),
    ...(retryable !== undefined ? { retryable } : {}),
    ...(traceId ? { trace_id: traceId } : {}),
    ...(receiptId ? { receipt_id: receiptId } : {}),
    createdAt: normalizeServerCreatedAt(record),
    status,
    result,
    error: status === "failure" ? normalizeServerError(record) : "",
  };
}

function normalizeDocsClipIngestJobsResponse(
  value: unknown,
): { valid: boolean; jobs: DocsClipIngestJob[] } {
  const record = objectRecord(value);
  const rawJobs = Array.isArray(value) ? value : record?.jobs ?? record?.items;
  if (!Array.isArray(rawJobs)) return { valid: false, jobs: [] };
  const jobs = rawJobs.flatMap((item) => {
    const normalized = normalizeDocsClipIngestServerJob(item);
    return normalized ? [normalized] : [];
  });
  return {
    valid: rawJobs.length === 0 || jobs.length > 0,
    jobs,
  };
}

/** 壊れたキャッシュを隔離し、古い順の最大50件へ正規化する。 */
export function normalizeDocsClipIngestHistory(value: unknown): DocsClipIngestJob[] {
  if (!Array.isArray(value)) return [];
  return value
    .flatMap((item): DocsClipIngestJob[] => {
      if (!item || typeof item !== "object" || Array.isArray(item)) return [];
      const record = item as Record<string, unknown>;
      if (
        typeof record.id !== "string"
        || typeof record.source !== "string"
        || !isJobStatus(record.status)
      ) return [];
      const uploadIds = normalizeUploadIds(record.upload_ids ?? record.uploadIds);
      const targetNodeId = normalizeTargetNodeId(record.targetNodeId ?? record.target_node_id);
      const code = normalizeErrorMetadataString(record.code);
      const retryable = normalizeRetryable(record.retryable);
      const traceId = normalizeErrorMetadataString(record.trace_id);
      const receiptId = normalizeReceiptId(record.receipt_id ?? record.receiptId);
      if (!record.source.trim() && uploadIds.length === 0) return [];
      const interrupted = record.status === "queued" || record.status === "running";
      // Before the explicit interrupted state existed, a reload converted an
      // in-flight job to failure with this sentinel message. Treat that
      // legacy shape as result-unknown rather than an HTTP/API failure.
      const resultUnknown = interrupted
        || record.status === "interrupted"
        || (record.status === "failure" && record.error === INTERRUPTED_ERROR);
      const createdAt = typeof record.createdAt === "string"
        && !Number.isNaN(Date.parse(record.createdAt))
        ? record.createdAt
        : new Date(0).toISOString();
      const result = resultUnknown ? null : normalizeResult(record.result);
      const invalidSuccess = record.status === "success" && result === null;
      const unknownResult = resultUnknown || invalidSuccess;
      return [{
        id: record.id,
        origin: "legacy",
        source: record.source,
        targetNodeId,
        upload_ids: uploadIds,
        skip_image_recognition: record.skip_image_recognition === true,
        enable_external_research: record.enable_external_research !== false,
        ...(code ? { code } : {}),
        ...(retryable !== undefined ? { retryable } : {}),
        ...(traceId ? { trace_id: traceId } : {}),
        ...(receiptId ? { receipt_id: receiptId } : {}),
        createdAt,
        status: unknownResult ? "interrupted" : record.status,
        result,
        error: unknownResult
          ? resultUnknown
            ? INTERRUPTED_ERROR
            : "保存済みの取り込み結果を読み込めませんでした。Docsで結果を確認してください。"
          : typeof record.error === "string"
            ? record.error
            : "",
      }];
    })
    .slice(-DOCS_CLIP_INGEST_HISTORY_LIMIT);
}

function historiesMatch(left: unknown, right: DocsClipIngestJob[]): boolean {
  try {
    return JSON.stringify(left) === JSON.stringify(right);
  } catch {
    return false;
  }
}

/** Server owns lifecycle; this hook only creates, rehydrates and polls durable jobs. */
export function useDocsClipIngestJobs() {
  const userId = useCurrentUserId();
  const userScope = userId === undefined ? "__unresolved__" : userId ?? "__anonymous__";
  const [jobs, setJobs] = useState<DocsClipIngestJob[]>([]);
  const [connectionError, setConnectionError] = useState(false);
  const [requestError, setRequestError] = useState<string | null>(null);
  const jobsRef = useRef<DocsClipIngestJob[]>([]);
  const mountedRef = useRef(true);
  const scopeRef = useRef<string | null>(null);
  const epochRef = useRef(1);
  const persistenceQueueRef = useRef<Promise<void>>(Promise.resolve());
  const pollInFlightRef = useRef(false);
  const reconciliationInFlightRef = useRef(false);
  const pendingRequestCountRef = useRef(0);
  /**
   * A POST can commit durably and still lose its 202 response at the network
   * boundary. Keep that exact key until the list endpoint echoes it back so
   * reconciliation never has to issue a second POST.
   */
  const unresolvedSubmissionsRef = useRef(new Map<string, DocsClipIngestRequest>());

  const rememberUnresolvedSubmission = useCallback((
    idempotencyKey: string,
    request: DocsClipIngestRequest,
  ) => {
    if (!idempotencyKey.trim()) return;
    unresolvedSubmissionsRef.current.set(idempotencyKey, request);
  }, []);

  const persistLegacy = useCallback(() => {
    persistenceQueueRef.current = persistenceQueueRef.current
      .catch(() => {})
      .then(async () => {
        if (!mountedRef.current) return;
        await writeCachedSnapshot(
          DOCS_CLIP_INGEST_HISTORY_KEY,
          serializeJobsForHistory(jobsRef.current.filter((job) => job.origin !== "server")),
        );
      });
  }, []);

  const setSnapshot = useCallback((
    update: (previous: DocsClipIngestJob[]) => DocsClipIngestJob[],
    persist = false,
  ) => {
    if (!mountedRef.current) return;
    const next = update(jobsRef.current).slice(-DOCS_CLIP_INGEST_HISTORY_LIMIT);
    jobsRef.current = next;
    setJobs(next);
    if (persist) persistLegacy();
  }, [persistLegacy]);

  const markConnectionError = useCallback((epoch: number) => {
    if (mountedRef.current && epochRef.current === epoch) {
      setRequestError(null);
      setConnectionError(true);
    }
  }, []);

  const markHttpError = useCallback((epoch: number, error: unknown) => {
    if (mountedRef.current && epochRef.current === epoch) {
      setConnectionError(false);
      setRequestError(formatHttpError(error));
    }
  }, []);

  const refreshRecentJobs = useCallback(async (epoch: number) => {
    try {
      const response = await apiFetch<unknown>(
        DOCS_CLIP_INGEST_JOBS_PATH + "?limit=" + DOCS_CLIP_INGEST_HISTORY_LIMIT,
      );
      if (!mountedRef.current || epochRef.current !== epoch) return;
      const normalized = normalizeDocsClipIngestJobsResponse(response);
      if (!normalized.valid) {
        markConnectionError(epoch);
        return;
      }
      for (const job of normalized.jobs) {
        const idempotencyKey = job.idempotency_key;
        if (idempotencyKey) unresolvedSubmissionsRef.current.delete(idempotencyKey);
      }
      const currentServer = jobsRef.current.filter((job) => job.origin === "server");
      const currentById = new Map(currentServer.map((job) => [job.id, job]));
      const mergedJobs = normalized.jobs.map((job) => {
        const current = currentById.get(job.id);
        const currentTerminal = current?.status === "success"
          || current?.status === "failure"
          || current?.status === "interrupted";
        const refreshedActive = job.status === "queued" || job.status === "running";
        return current && currentTerminal && refreshedActive ? current : job;
      });
      const byId = new Map(mergedJobs.map((job) => [job.id, job]));
      const stillLocal = currentServer.filter(
        (job) => !byId.has(job.id),
      );
      setSnapshot(
        (previous) => [
          ...previous.filter((job) => job.origin !== "server"),
          ...mergedJobs,
          ...stillLocal,
        ],
      );
      if (
        pendingRequestCountRef.current === 0
        && unresolvedSubmissionsRef.current.size === 0
      ) {
        setConnectionError(false);
      }
    } catch (error: unknown) {
      if (isTransportFailure(error)) markConnectionError(epoch);
      else markHttpError(epoch, error);
    }
  }, [markConnectionError, markHttpError, setSnapshot]);

  const pollJob = useCallback(async (job: DocsClipIngestJob, epoch: number): Promise<boolean> => {
    try {
      const response = await apiFetch<unknown>(
        DOCS_CLIP_INGEST_JOBS_PATH + "/" + encodeURIComponent(job.id),
      );
      if (!mountedRef.current || epochRef.current !== epoch) return false;
      const normalized = normalizeDocsClipIngestServerJob(response, job);
      if (!normalized) {
        markConnectionError(epoch);
        return false;
      }
      setSnapshot((previous) => previous.map((item) => {
        if (item.id !== job.id || item.origin !== "server") return item;
        if (item.status === "success" || item.status === "failure") return item;
        return normalized;
      }));
      return true;
    } catch (error: unknown) {
      if (isTransportFailure(error)) markConnectionError(epoch);
      else markHttpError(epoch, error);
      return false;
    }
  }, [markConnectionError, markHttpError, setSnapshot]);

  const pollActiveJobs = useCallback(async () => {
    if (pollInFlightRef.current || !mountedRef.current) return;
    const epoch = epochRef.current;
    const active = jobsRef.current.filter(
      (job) => job.origin === "server"
        && (job.status === "queued" || job.status === "running"),
    );
    if (active.length === 0) return;
    pollInFlightRef.current = true;
    try {
      const results = await Promise.all(active.map((job) => pollJob(job, epoch)));
      if (
        results.every(Boolean)
        && pendingRequestCountRef.current === 0
        && unresolvedSubmissionsRef.current.size === 0
        && mountedRef.current
        && epochRef.current === epoch
      ) {
        setConnectionError(false);
      }
    } finally {
      pollInFlightRef.current = false;
    }
  }, [pollJob]);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      epochRef.current += 1;
    };
  }, []);

  useEffect(() => {
    if (scopeRef.current === userScope) return;
    const initialScope = scopeRef.current === null;
    scopeRef.current = userScope;
    const epoch = initialScope
      ? epochRef.current
      : ++epochRef.current;
    pollInFlightRef.current = false;
    unresolvedSubmissionsRef.current.clear();
    setConnectionError(false);
    setRequestError(null);
    jobsRef.current = jobsRef.current.filter((job) => job.origin !== "server");
    setJobs(jobsRef.current);
    let cancelled = false;
    void readCachedSnapshot<unknown>(DOCS_CLIP_INGEST_HISTORY_KEY)
      .catch(() => undefined)
      .then((value) => {
        if (cancelled || !mountedRef.current || epochRef.current !== epoch) return;
        const restored = normalizeDocsClipIngestHistory(value);
        const currentServer = jobsRef.current.filter((job) => job.origin === "server");
        jobsRef.current = [...restored, ...currentServer].slice(-DOCS_CLIP_INGEST_HISTORY_LIMIT);
        setJobs(jobsRef.current);
        if (Array.isArray(value) && !historiesMatch(value, serializeJobsForHistory(restored))) {
          persistLegacy();
        }
        void refreshRecentJobs(epoch);
      });
    return () => {
      cancelled = true;
    };
  }, [persistLegacy, refreshRecentJobs, userScope]);

  useEffect(() => {
    if (!jobs.some((job) => job.origin === "server"
      && (job.status === "queued" || job.status === "running"))) return;
    const timer = window.setInterval(() => {
      void pollActiveJobs();
    }, DOCS_CLIP_INGEST_POLL_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [jobs, pollActiveJobs]);

  useEffect(() => {
    if (!connectionError) return;
    const epoch = epochRef.current;
    let attempts = 0;
    let stopped = false;

    const reconcile = () => {
      if (
        stopped
        || reconciliationInFlightRef.current
        || !mountedRef.current
        || epochRef.current !== epoch
      ) return;
      if (attempts >= DOCS_CLIP_INGEST_RECONCILIATION_MAX_ATTEMPTS) {
        stopped = true;
        window.clearInterval(timer);
        return;
      }
      attempts += 1;
      reconciliationInFlightRef.current = true;
      void refreshRecentJobs(epoch).finally(() => {
        reconciliationInFlightRef.current = false;
      });
    };

    const timer = window.setInterval(reconcile, DOCS_CLIP_INGEST_RECONCILIATION_INTERVAL_MS);
    return () => {
      stopped = true;
      window.clearInterval(timer);
    };
  }, [connectionError, refreshRecentJobs]);

  const enqueue = useCallback((input: DocsClipIngestRequest | string) => {
    const request = normalizeIngestRequest(input);
    if (!request.source.trim() && !(request.upload_ids?.length)) return;
    const epoch = epochRef.current;
    pendingRequestCountRef.current += 1;
    const idempotencyKey = createIdempotencyKey();
    void apiFetch<unknown>(DOCS_CLIP_INGEST_JOBS_PATH, {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
      body: requestBody(request),
    })
      .then((response) => {
        if (!mountedRef.current || epochRef.current !== epoch) return;
        const job = normalizeDocsClipIngestServerJob(response, request);
        if (!job) {
          rememberUnresolvedSubmission(idempotencyKey, request);
          markConnectionError(epoch);
          return;
        }
        unresolvedSubmissionsRef.current.delete(idempotencyKey);
        setRequestError(null);
        setSnapshot((previous) => [
          ...previous.filter((item) => item.id !== job.id),
          job,
        ]);
        if (
          pendingRequestCountRef.current <= 1
          && unresolvedSubmissionsRef.current.size === 0
        ) {
          setConnectionError(false);
        }
      })
      .catch((error: unknown) => {
        const transportFailure = isTransportFailure(error);
        const alreadyReconciled = jobsRef.current.some(
          (job) => job.idempotency_key === idempotencyKey,
        );
        if (transportFailure) {
          if (
            mountedRef.current
            && epochRef.current === epoch
            && !alreadyReconciled
          ) {
            rememberUnresolvedSubmission(idempotencyKey, request);
          }
          if (alreadyReconciled) return;
        }
        if (transportFailure) markConnectionError(epoch);
        else markHttpError(epoch, error);
      })
      .finally(() => {
        pendingRequestCountRef.current = Math.max(0, pendingRequestCountRef.current - 1);
      });
  }, [markConnectionError, markHttpError, rememberUnresolvedSubmission, setSnapshot]);

  const dismiss = useCallback((id: string) => {
    setSnapshot((previous) => previous.filter((job) => job.id !== id), true);
  }, [setSnapshot]);

  const retry = useCallback((id: string) => {
    const job = jobsRef.current.find((item) => item.id === id);
    if (
      !job
      || (job.status !== "failure" && job.status !== "interrupted")
      || job.retryable === false
    ) return;
    const request: DocsClipIngestRequest = {
      source: job.source,
      ...(job.targetNodeId ? { target_node_id: job.targetNodeId } : {}),
      upload_ids: job.upload_ids ?? [],
      skip_image_recognition: job.skip_image_recognition === true,
      enable_external_research: job.enable_external_research !== false,
    };
    const epoch = epochRef.current;
    pendingRequestCountRef.current += 1;
    const path = job.origin === "server"
      ? DOCS_CLIP_INGEST_JOBS_PATH + "/" + encodeURIComponent(job.id) + "/retry"
      : DOCS_CLIP_INGEST_JOBS_PATH;
    const idempotencyKey = createIdempotencyKey();
    void apiFetch<unknown>(path, {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
      ...(job.origin === "server" ? {} : { body: requestBody(request) }),
    })
      .then((response) => {
        if (!mountedRef.current || epochRef.current !== epoch) return;
        const retried = normalizeDocsClipIngestServerJob(response, request);
        if (!retried || (job.origin === "server" && retried.id === job.id)) {
          rememberUnresolvedSubmission(idempotencyKey, request);
          markConnectionError(epoch);
          return;
        }
        unresolvedSubmissionsRef.current.delete(idempotencyKey);
        setRequestError(null);
        setSnapshot((previous) => [
          ...previous.filter((item) => item.id !== retried.id),
          retried,
        ]);
        if (
          pendingRequestCountRef.current <= 1
          && unresolvedSubmissionsRef.current.size === 0
        ) {
          setConnectionError(false);
        }
      })
      .catch((error: unknown) => {
        const transportFailure = isTransportFailure(error);
        const alreadyReconciled = jobsRef.current.some(
          (item) => item.idempotency_key === idempotencyKey,
        );
        if (transportFailure) {
          if (
            mountedRef.current
            && epochRef.current === epoch
            && !alreadyReconciled
          ) {
            rememberUnresolvedSubmission(idempotencyKey, request);
          }
          if (alreadyReconciled) return;
        }
        if (transportFailure) markConnectionError(epoch);
        else markHttpError(epoch, error);
      })
      .finally(() => {
        pendingRequestCountRef.current = Math.max(0, pendingRequestCountRef.current - 1);
      });
  }, [markConnectionError, markHttpError, rememberUnresolvedSubmission, setSnapshot]);

  return { jobs, enqueue, dismiss, retry, connectionError, requestError };
}

type DocsClipIngestPanelProps = {
  jobs: DocsClipIngestJob[];
  connectionError?: boolean;
  requestError?: string | null;
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
  onDismiss: (id: string) => void;
  onOpenNode: (nodeId: string) => void | Promise<void>;
  onRetry?: (id: string) => void;
  /** Provider-owned shared receipt detail Dialog callback. */
  onOpenDetail?: (job: DocsClipIngestJob) => void;
  /** Provider-owned detail Dialog is open while a history item is selected. */
  detailOpen?: boolean;
  onCloseDetail?: () => void;
};

/**
 * 履歴が閉じている間は詳細Dialogのstateを持つcomponent自体をunmountする。
 * Providerはlayout直下で常駐するため、単に `return null` するだけでは
 * detailJobIdが残り、次回open時に直前の結果が復活してしまう。
 */
export function DocsClipIngestPanel(props: DocsClipIngestPanelProps) {
  if (props.open === false) return null;
  return <DocsClipIngestPanelContent {...props} open />;
}

export type DocsClipIngestRequest = DocsClipIngestSubmission;

export function normalizeIngestRequest(
  input: DocsClipIngestRequest | string,
): DocsClipIngestRequest {
  if (typeof input === "string") {
    return { source: input, enable_external_research: true };
  }
  const targetNodeId = normalizeTargetNodeId(input.target_node_id ?? input.targetNodeId);
  return {
    source: typeof input.source === "string" ? input.source : "",
    ...(targetNodeId ? { target_node_id: targetNodeId } : {}),
    upload_ids: normalizeUploadIds(input.upload_ids),
    skip_image_recognition: input.skip_image_recognition === true,
    enable_external_research: input.enable_external_research !== false,
  };
}

export function requestBody(request: DocsClipIngestRequest): string {
  // Keep the text-only wire shape backwards compatible. An explicitly chosen
  // target has a deliberately strict text contract: do not add empty upload
  // arrays or a false image-recognition flag to that request.
  const targetNodeId = normalizeTargetNodeId(request.target_node_id ?? request.targetNodeId);
  const hasUploads = Boolean(request.upload_ids?.length);
  const skipImageRecognition = request.skip_image_recognition === true;
  const enableExternalResearch = request.enable_external_research !== false;
  if (targetNodeId && !hasUploads && !skipImageRecognition) {
    return JSON.stringify({
      source: request.source,
      target_node_id: targetNodeId,
      ...(enableExternalResearch ? {} : { enable_external_research: false }),
    });
  }
  if (hasUploads || skipImageRecognition) {
    return JSON.stringify({
      source: request.source,
      ...(targetNodeId ? { target_node_id: targetNodeId } : {}),
      upload_ids: request.upload_ids ?? [],
      skip_image_recognition: skipImageRecognition,
      ...(enableExternalResearch ? {} : { enable_external_research: false }),
    });
  }
  return JSON.stringify({
    source: request.source,
    ...(enableExternalResearch ? {} : { enable_external_research: false }),
  });
}

export function serializeJobsForHistory(jobs: DocsClipIngestJob[]): DocsClipIngestJob[] {
  // Keep persistence intentionally scalar. In particular, do not spread an
  // object supplied by an upload picker into the cache: only staging IDs may
  // survive a reload and be used for retry.
  return jobs.map((job) => {
    const code = normalizeErrorMetadataString(job.code);
    const retryable = normalizeRetryable(job.retryable);
    const traceId = normalizeErrorMetadataString(job.trace_id);
    return {
      id: job.id,
      source: job.source,
      targetNodeId: normalizeTargetNodeId(job.targetNodeId),
      upload_ids: normalizeUploadIds(job.upload_ids),
      skip_image_recognition: job.skip_image_recognition === true,
      enable_external_research: job.enable_external_research !== false,
      ...(code ? { code } : {}),
      ...(retryable !== undefined ? { retryable } : {}),
      ...(traceId ? { trace_id: traceId } : {}),
      ...(normalizeReceiptId(job.receipt_id) ? { receipt_id: normalizeReceiptId(job.receipt_id) } : {}),
      createdAt: job.createdAt,
      status: job.status,
      result: job.result,
      error: job.error,
    };
  });
}

function DocsClipIngestPanelContent({
  jobs,
  connectionError = false,
  requestError = null,
  open = true,
  onOpenChange,
  onDismiss,
  onOpenNode,
  onRetry,
  onOpenDetail,
  detailOpen = false,
  onCloseDetail,
}: DocsClipIngestPanelProps) {
  const [detailJobId, setDetailJobId] = useState<string | null>(null);
  const detailJob = jobs.find((job) => job.id === detailJobId) ?? null;
  const detailProcessing = detailJob?.status === "queued" || detailJob?.status === "running";
  const retryingJobIdsRef = useRef(new Set<string>());
  const jobsRef = useRef(jobs);
  const detailCycleGenerationRef = useRef(0);
  const mountedRef = useRef(true);

  useEffect(() => {
    jobsRef.current = jobs;
  }, [jobs]);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  useEffect(() => {
    if (!detailJob) {
      retryingJobIdsRef.current.clear();
      if (detailJobId !== null) {
        const staleDetailJobId = detailJobId;
        const staleDetailCycleGeneration = detailCycleGenerationRef.current;
        // 履歴は親から非同期に渡されるため、現在のrenderを壊さずに
        // microtaskで詳細IDを解除する。別ジョブを開く競合はfunctional
        // updaterで保護し、実行時にも最新jobsとcycle世代を再確認して
        // 同じIDのrehydrateを古いcallbackが消さないようにする。
        queueMicrotask(() => {
          if (!mountedRef.current) return;
          if (
            detailCycleGenerationRef.current !== staleDetailCycleGeneration
            || jobsRef.current.some((job) => job.id === staleDetailJobId)
          ) return;
          detailCycleGenerationRef.current += 1;
          setDetailJobId((current) => current === staleDetailJobId ? null : current);
        });
      }
    } else if (!detailProcessing) {
      retryingJobIdsRef.current.delete(detailJob.id);
    }
  }, [detailJob, detailJobId, detailProcessing]);

  const clearDetail = useCallback(() => {
    detailCycleGenerationRef.current += 1;
    setDetailJobId(null);
  }, []);

  const handleOpenChange = useCallback((
    nextOpen: boolean,
    eventDetails?: DialogRoot.ChangeEventDetails,
  ) => {
    // 結果Dialogを開くとフォーカスがSheet外のPortalへ移るため、modal={false}
    // のSheetがfocusOutやoutside-pressとして閉じようとする。詳細表示中は
    // その自動dismissをキャンセルし、明示的なheader closeはclearDetail先行で通す。
    if (
      !nextOpen
      && (detailJobId !== null || detailOpen)
      && (
        eventDetails?.reason === "focus-out"
        || eventDetails?.reason === "outside-press"
      )
    ) {
      eventDetails.cancel?.();
      return;
    }
    if (!nextOpen) clearDetail();
    onOpenChange?.(nextOpen);
  }, [clearDetail, detailJobId, detailOpen, onOpenChange]);

  if (!open) return null;

  const handleExplicitHistoryClose = () => {
    clearDetail();
    onCloseDetail?.();
    onOpenChange?.(false);
  };

  const panelContent = (
    <>
      <div className="flex items-center justify-between border-b px-3 py-2 text-xs font-medium text-muted-foreground">
          <span>クリップ取り込み</span>
          <div className="flex items-center gap-2">
            <span>{jobs.length}件</span>
            {onOpenChange ? (
              <button
                type="button"
                aria-label="取り込み履歴を閉じる"
                onClick={handleExplicitHistoryClose}
                className="grid size-5 place-items-center rounded hover:bg-accent hover:text-foreground"
              >
                <X className="size-3.5" />
              </button>
            ) : null}
          </div>
      </div>
      <div className="min-h-0 flex-1 space-y-1 overflow-auto p-2">
        {requestError ? (
          <div role="alert" className="rounded-lg border border-red-300 bg-red-50 p-2 text-xs text-red-900 dark:border-red-700 dark:bg-red-950/30 dark:text-red-100">
            {requestError}
          </div>
        ) : null}
        {connectionError ? (
          <div role="status" className="rounded-lg border border-amber-300 bg-amber-50 p-2 text-xs text-amber-900 dark:border-amber-700 dark:bg-amber-950/30 dark:text-amber-100">
            {DOCS_CLIP_INGEST_CONNECTION_ERROR}
          </div>
        ) : null}
        {jobs.length === 0 ? (
          <div className="rounded-lg border border-dashed p-3 text-center text-xs text-muted-foreground">
            取り込み履歴はまだありません
          </div>
        ) : jobs.slice().reverse().map((job) => {
          const finished = job.status === "success"
            || job.status === "failure"
            || job.status === "interrupted";
          return (
            <div
              key={job.id}
              className="flex items-start gap-2 rounded-lg border bg-background p-2 text-xs"
            >
              <button
                type="button"
                disabled={!finished}
                onClick={() => {
                  if (onOpenDetail) {
                    onOpenDetail(job);
                    return;
                  }
                  detailCycleGenerationRef.current += 1;
                  setDetailJobId(job.id);
                }}
                className="min-w-0 flex-1 text-left disabled:cursor-default"
              >
                <div className="flex items-center gap-1.5 font-medium">
                  {job.status === "success" ? (
                    <CheckCircle2 className="size-3.5 shrink-0 text-emerald-600 dark:text-emerald-400" />
                  ) : job.status === "failure" ? (
                    <AlertTriangle className="size-3.5 shrink-0 text-destructive" />
                  ) : job.status === "interrupted" ? (
                    <CircleHelp className="size-3.5 shrink-0 text-amber-600 dark:text-amber-400" />
                  ) : (
                    <Loader2 className="size-3.5 shrink-0 animate-spin" />
                  )}
                  <span>{STATUS_LABELS[job.status]}</span>
                </div>
                <div className="mt-1 line-clamp-2 break-all text-muted-foreground">
                  {job.result?.open_node_title
                    || job.source
                    || ((job.upload_ids?.length ?? 0) > 0
                      ? `${job.upload_ids?.length}件のファイル`
                      : "")}
                </div>
                {job.skip_image_recognition ? (
                  <div className="mt-1 text-[10px] text-muted-foreground/70">画像認識を実施しない</div>
                ) : null}
                <div className="mt-1 text-[10px] text-muted-foreground/70">
                  {new Date(job.createdAt).toLocaleString("ja-JP")}
                </div>
                {finished ? (
                  <div className="mt-1 text-[11px] text-muted-foreground/80">
                    クリックで結果を表示
                  </div>
                ) : null}
              </button>
              <button
                type="button"
                aria-label="この取り込みを一覧から消す"
                disabled={!finished}
                onClick={() => onDismiss(job.id)}
                className="grid size-5 shrink-0 place-items-center rounded text-muted-foreground hover:bg-accent hover:text-foreground disabled:opacity-30"
              >
                <X className="size-3.5" />
              </button>
            </div>
          );
        })}
      </div>
    </>
  );

  return (
    <>
      <Sheet modal={false} open={open} onOpenChange={handleOpenChange}>
          <SheetContent
            side="right"
            showCloseButton={false}
            aria-label="クリップ取り込みの状況"
            className="w-[min(90vw,20rem)] gap-0 p-0"
          >
            {panelContent}
          </SheetContent>
      </Sheet>

      {!onOpenDetail ? (
        <DocsClipIngestDetailDialog
          open={detailJob !== null}
          legacyJob={detailJob}
          onOpenChange={(nextOpen) => {
            if (!nextOpen) clearDetail();
          }}
          onRetry={onRetry ? (jobId) => {
            if (retryingJobIdsRef.current.has(jobId)) return;
            retryingJobIdsRef.current.add(jobId);
            detailCycleGenerationRef.current += 1;
            onRetry(jobId);
          } : undefined}
          onOpenNode={onOpenNode}
        />
      ) : null}
    </>
  );
}
