import type { components } from "@/lib/api-types.gen";

export type LedgerState = components["schemas"]["PersonalLedgerStateResponse"];
export type LedgerEntry = components["schemas"]["PersonalLedgerEntryOut"];
export type LedgerDeadline = components["schemas"]["PersonalLedgerDeadlineOut"];
export type LedgerEntryCreate = components["schemas"]["LedgerEntryCreate"];
export type LedgerEntriesPage = components["schemas"]["PersonalLedgerEntriesResponse"];
export type ReminderActionResponse = components["schemas"]["ReminderActionResponse"];
export type ReminderItem = components["schemas"]["ReminderItem"];
export type ReminderListResponse = components["schemas"]["ReminderListResponse"];
export type ReminderSelectionResponse = components["schemas"]["ReminderSelectionResponse"];

/** 送信内容と冪等キーを作成時に固定した書き込み要求。再送は同じオブジェクトをそのまま使う。 */
export type LedgerWrite = Readonly<{ path: string; body: Readonly<Record<string, unknown>> }>;

const BASE = "/api/python-proxy/personal-ledger";
const REMINDER_BASE = "/api/python-proxy/personal-reminders";

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

async function ownerFetch<T>(base: string, path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${base}${path}`, {
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

async function ledgerFetch<T>(path: string, init?: RequestInit): Promise<T> {
  return ownerFetch<T>(BASE, path, init);
}

async function reminderFetch<T>(path: string, init?: RequestInit): Promise<T> {
  return ownerFetch<T>(REMINDER_BASE, path, init);
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

export const reminderApi = {
  list: () => reminderFetch<ReminderListResponse>("?limit=100"),
  selectTime: (body: {
    candidate_entry_id: string;
    confirmed_digest: string;
    remind_at: string;
    time_zone: string;
    idempotency_key: string;
  }) => reminderFetch<ReminderSelectionResponse>("/selections", {
    method: "POST", body: JSON.stringify(body),
  }),
  propose: (body: {
    candidate_entry_id: string;
    confirmed_digest: string;
    remind_at: string;
    time_zone: string;
    idempotency_key: string;
  }) => reminderFetch<ReminderActionResponse>("/propose", {
    method: "POST", body: JSON.stringify(body),
  }),
  get: (draftRef: string) => reminderFetch<ReminderItem>(`/${encodeURIComponent(draftRef)}`),
  schedule: (draftRef: string, body: { confirmed_digest: string; idempotency_key: string }) => reminderFetch<ReminderActionResponse>(
    `/${encodeURIComponent(draftRef)}/schedule`, { method: "POST", body: JSON.stringify(body) },
  ),
  cancel: (draftRef: string, body: { idempotency_key: string }) => reminderFetch<ReminderActionResponse>(
    `/${encodeURIComponent(draftRef)}/cancel`, { method: "POST", body: JSON.stringify(body) },
  ),
  acknowledge: (draftRef: string, body: { idempotency_key: string }) => reminderFetch<ReminderActionResponse>(
    `/${encodeURIComponent(draftRef)}/ack`, { method: "POST", body: JSON.stringify(body) },
  ),
};
