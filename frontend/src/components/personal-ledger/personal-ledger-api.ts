import type { components } from "@/lib/api-types.gen";

export type LedgerState = components["schemas"]["PersonalLedgerStateResponse"];
export type LedgerEntry = components["schemas"]["PersonalLedgerEntryOut"];
export type LedgerDeadline = components["schemas"]["PersonalLedgerDeadlineOut"];
export type LedgerEntryCreate = components["schemas"]["LedgerEntryCreate"];
export type LedgerEntriesPage = components["schemas"]["PersonalLedgerEntriesResponse"];

/** 送信内容と冪等キーを作成時に固定した書き込み要求。再送は同じオブジェクトをそのまま使う。 */
export type LedgerWrite = Readonly<{ path: string; body: Readonly<Record<string, unknown>> }>;

const BASE = "/api/python-proxy/personal-ledger";

/** サーバーが応答を返した失敗（結果は確定している）。 */
export class LedgerHttpError extends Error {
  constructor(readonly status: number, message: string) {
    super(message);
    this.name = "LedgerHttpError";
  }
}

/**
 * 書き込みの結果が不明か。通信断・タイムアウト・5xx は、サーバー側で成功している可能性が
 * あるため、同じ冪等キーで再送して確かめる。4xx（408 を除く）は拒否が確定している。
 */
export function isAmbiguousWriteFailure(error: unknown): boolean {
  if (!(error instanceof LedgerHttpError)) return true;
  return error.status >= 500 || error.status === 408;
}

async function ledgerFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!res.ok) {
    const body: unknown = await res.json().catch(() => ({}));
    const detail = body && typeof body === "object" ? (body as Record<string, unknown>).detail : undefined;
    throw new LedgerHttpError(res.status, typeof detail === "string" ? detail : `API Error: ${res.status}`);
  }
  return res.json() as Promise<T>;
}

/** 操作ごとに新しい冪等キーを作る。キーは LedgerWrite の作成時に1回だけ作り、再送で変えない。 */
export function newIdempotencyKey(): string {
  return `ui-${crypto.randomUUID()}`;
}

export function addWrite(body: Omit<LedgerEntryCreate, "idempotency_key">): LedgerWrite {
  return { path: "/entries", body: { ...body, idempotency_key: newIdempotencyKey() } };
}

export function verifyWrite(entryId: string, confirmedDigest: string, supersedesEntryId?: string): LedgerWrite {
  return {
    path: `/entries/${entryId}/verify`,
    body: {
      confirmed_digest: confirmedDigest,
      idempotency_key: newIdempotencyKey(),
      ...(supersedesEntryId ? { supersedes_entry_id: supersedesEntryId } : {}),
    },
  };
}

export function completeWrite(entryId: string, completedOn: string, supersedesEntryId?: string): LedgerWrite {
  return {
    path: `/entries/${entryId}/complete`,
    body: {
      completed_on: completedOn,
      idempotency_key: newIdempotencyKey(),
      ...(supersedesEntryId ? { supersedes_entry_id: supersedesEntryId } : {}),
    },
  };
}

export const ledgerApi = {
  state: () => ledgerFetch<LedgerState>("/state"),
  entries: (beforeSeq?: number | null) =>
    ledgerFetch<LedgerEntriesPage>(`/entries?limit=50${beforeSeq ? `&before_seq=${beforeSeq}` : ""}`),
  send: (write: LedgerWrite) => ledgerFetch<unknown>(write.path, { method: "POST", body: JSON.stringify(write.body) }),
};
