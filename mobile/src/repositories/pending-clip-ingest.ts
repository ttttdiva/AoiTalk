/**
 * クリップ取り込みの保留キュー。
 *
 * サーバー未到達かつモバイルLLMでもローカル完結できなかった入力を SQLite に
 * 保持し、次にサーバーへ到達できたときへ `POST /api/docs/ingest` で再送する。
 *
 * 保留行には enqueue 時点の認証スコープ（`auth:<user_id>` / 'anonymous'）を持たせる。
 * 再送対象は常に現在のスコープと完全一致する行だけに限る。旧バージョンで
 * `auth_scope` が NULL のまま保存された行は、どのJWTにも帰属させずDBに保持する
 * （自動 adopt / send / delete はしない）。未ログインの行も明示的な
 * `anonymous` スコープとしてのみ扱い、ログイン後のユーザーへ引き継がない。
 */

import { and, asc, eq } from "drizzle-orm";
import { getDb, schema } from "../db/client";
import { docsApi } from "../lib/docs-api";
import { isApiConnectionError, isApiHttpError } from "../lib/api-client";
import { getToken, getTokenAuthScope } from "../lib/auth";
import { randomId } from "./outbox";

/** 恒久エラー扱いにするまでの再送回数。 */
export const PENDING_CLIP_INGEST_MAX_RETRY = 5;

/** 未ログインで積まれた保留行の明示スコープ。ログイン後へ引き継がない。 */
export const ANONYMOUS_AUTH_SCOPE = "anonymous";

export interface PendingClipIngestRow {
  id: string;
  source: string;
  status: string;
  authScope: string | null;
  retryCount: number;
  lastError: string | null;
  createdAt: string | null;
}

/** 現在の認証スコープ。sync engine 等と同じ `getTokenAuthScope` の流儀に合わせる。 */
export async function getCurrentClipIngestAuthScope(): Promise<string> {
  return getTokenAuthScope(await getToken());
}

/** 取り出し対象のスコープ条件。NULL/別ユーザーを決して含めない。 */
function authScopeFilter(currentScope: string) {
  return eq(schema.pendingClipIngests.authScope, currentScope);
}

function errorText(error: unknown): string {
  const raw = error instanceof Error ? error.message : String(error || "");
  const jsonStart = raw.indexOf("{");
  if (jsonStart >= 0) {
    try {
      const parsed = JSON.parse(raw.slice(jsonStart)) as { detail?: unknown };
      if (typeof parsed.detail === "string") return parsed.detail;
    } catch {
      // JSONでなければ元のメッセージを使う。
    }
  }
  return raw.slice(0, 500) || "クリップ取り込みの再送に失敗しました";
}

/** 再送しても直らない恒久エラー（入力・設定起因）かを判定する。 */
function isPermanentIngestError(error: unknown): boolean {
  if (!isApiHttpError(error)) return false;
  const status = (error as { status: number }).status;
  // 401（未ログイン・期限切れ）と 408/409/429 は状況が変われば通るので保留のまま。
  // 409 は同時取り込みの競合など一過性の衝突を表すため、恒久失敗にしない。
  if (status === 401 || status === 408 || status === 409 || status === 429) {
    return false;
  }
  return status >= 400 && status < 500;
}

/** 認証更新やサーバー復旧、待機で解消するHTTPエラー。再送上限を消費しない。 */
function isDeferredIngestError(error: unknown): boolean {
  if (!isApiHttpError(error)) return false;
  const status = (error as { status: number }).status;
  return [401, 408, 409, 429].includes(status) || status >= 500;
}

export async function enqueuePendingClipIngest(source: string): Promise<string> {
  const db = getDb();
  const id = randomId();
  const now = new Date().toISOString();
  await db.insert(schema.pendingClipIngests).values({
    id,
    source,
    status: "queued",
    authScope: await getCurrentClipIngestAuthScope(),
    retryCount: 0,
    lastError: null,
    createdAt: now,
    updatedAt: now,
  });
  return id;
}

type PendingClipIngestDbRow = {
  id: string;
  source: string;
  status: string;
  authScope: string | null;
  retryCount: number;
  lastError: string | null;
  createdAt: string | null;
};

function toRow(row: PendingClipIngestDbRow): PendingClipIngestRow {
  return {
    id: row.id,
    source: row.source,
    status: row.status,
    authScope: row.authScope ?? null,
    retryCount: row.retryCount,
    lastError: row.lastError,
    createdAt: row.createdAt,
  };
}

async function listClipIngestsByStatus(
  status: string,
): Promise<PendingClipIngestRow[]> {
  const db = getDb();
  const currentScope = await getCurrentClipIngestAuthScope();
  const rows = (await db
    .select()
    .from(schema.pendingClipIngests)
    .where(
      and(
        eq(schema.pendingClipIngests.status, status),
        authScopeFilter(currentScope),
      ),
    )
    .orderBy(
      asc(schema.pendingClipIngests.createdAt),
    )) as PendingClipIngestDbRow[];
  return rows.map(toRow);
}

export async function listPendingClipIngests(): Promise<PendingClipIngestRow[]> {
  return listClipIngestsByStatus("queued");
}

/** ユーザーへ見せる失敗済みの保留（恒久エラー・再送打ち切り）。 */
export async function listFailedClipIngests(): Promise<PendingClipIngestRow[]> {
  return listClipIngestsByStatus("failed");
}

export async function countPendingClipIngests(): Promise<number> {
  return (await listPendingClipIngests()).length;
}

export async function removePendingClipIngest(id: string): Promise<void> {
  const db = getDb();
  const authScope = await getCurrentClipIngestAuthScope();
  await db
    .delete(schema.pendingClipIngests)
    .where(
      and(
        eq(schema.pendingClipIngests.id, id),
        eq(schema.pendingClipIngests.authScope, authScope),
      ),
    );
}

export async function markPendingClipIngestRetry(
  id: string,
  retryCount: number,
  error: string,
): Promise<void> {
  const db = getDb();
  const authScope = await getCurrentClipIngestAuthScope();
  const next = retryCount + 1;
  await db
    .update(schema.pendingClipIngests)
    .set({
      retryCount: next,
      lastError: error,
      // 再送を繰り返しても通らないものは失敗として残し、無限再送を避ける。
      status: next >= PENDING_CLIP_INGEST_MAX_RETRY ? "failed" : "queued",
      updatedAt: new Date().toISOString(),
    })
    .where(
      and(
        eq(schema.pendingClipIngests.id, id),
        eq(schema.pendingClipIngests.status, "queued"),
        eq(schema.pendingClipIngests.authScope, authScope),
      ),
    );
}

export async function markPendingClipIngestDeferred(
  id: string,
  error: string,
): Promise<void> {
  const db = getDb();
  const authScope = await getCurrentClipIngestAuthScope();
  await db
    .update(schema.pendingClipIngests)
    .set({
      lastError: error,
      updatedAt: new Date().toISOString(),
    })
    .where(
      and(
        eq(schema.pendingClipIngests.id, id),
        eq(schema.pendingClipIngests.status, "queued"),
        eq(schema.pendingClipIngests.authScope, authScope),
      ),
    );
}

export async function markPendingClipIngestFailed(
  id: string,
  error: string,
): Promise<void> {
  const db = getDb();
  const authScope = await getCurrentClipIngestAuthScope();
  await db
    .update(schema.pendingClipIngests)
    .set({
      status: "failed",
      lastError: error,
      updatedAt: new Date().toISOString(),
    })
    .where(
      and(
        eq(schema.pendingClipIngests.id, id),
        eq(schema.pendingClipIngests.authScope, authScope),
      ),
    );
}

/** flush の副作用。テストから差し替えられるよう外出しする。 */
export interface PendingClipIngestFlushDeps {
  list: () => Promise<PendingClipIngestRow[]>;
  hasAuth: () => Promise<boolean>;
  ingest: (source: string) => Promise<unknown>;
  remove: (id: string) => Promise<void>;
  markFailed: (id: string, error: string) => Promise<void>;
  markDeferred: (id: string, error: string) => Promise<void>;
  markRetry: (
    id: string,
    retryCount: number,
    error: string,
  ) => Promise<void>;
}

const defaultFlushDeps: PendingClipIngestFlushDeps = {
  list: listPendingClipIngests,
  hasAuth: async () => Boolean(await getToken()),
  ingest: (source) => docsApi.ingest(source),
  remove: removePendingClipIngest,
  markFailed: markPendingClipIngestFailed,
  markDeferred: markPendingClipIngestDeferred,
  markRetry: markPendingClipIngestRetry,
};

/**
 * 保留中のクリップ取り込みをサーバーへ再送する。
 * 対象は `listPendingClipIngests`（現在のスコープ + anonymous）に限られるため、
 * 別ユーザーのスコープで積まれた入力を現ユーザーとして送ることはない。
 * サーバー未到達を検知したらその場で打ち切り、次の同期へ回す。
 */
export async function flushPendingClipIngests(
  overrides: Partial<PendingClipIngestFlushDeps> = {},
): Promise<void> {
  const deps = { ...defaultFlushDeps, ...overrides };
  // Defense in depth: the SQL list is scope-filtered, but re-check rows here
  // so an injected/legacy reader can never cause a NULL or another user's row
  // to be sent with the current JWT.  NULL remains in SQLite for recovery.
  const currentScope = await getCurrentClipIngestAuthScope();
  const rows = (await deps.list()).filter(
    (row) => row.authScope === currentScope,
  );
  if (!rows.length) return;
  if (!(await deps.hasAuth())) return;

  for (const row of rows) {
    try {
      await deps.ingest(row.source);
      await deps.remove(row.id);
    } catch (error) {
      if (isApiConnectionError(error)) {
        // まだサーバーへ届かない。回数を消費せず次回の同期で再試行する。
        throw error;
      }
      const message = errorText(error);
      if (isDeferredIngestError(error)) {
        // 再ログイン、競合解消、rate limit解除後に通るため、失敗上限を消費しない。
        await deps.markDeferred(row.id, message);
        // 同じ認証・サーバー状態のまま後続行へ連続送信しない。
        return;
      }
      if (isPermanentIngestError(error)) {
        await deps.markFailed(row.id, message);
        continue;
      }
      await deps.markRetry(row.id, row.retryCount, message);
    }
  }
}
