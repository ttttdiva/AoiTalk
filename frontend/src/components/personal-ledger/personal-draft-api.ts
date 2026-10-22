import type { components, paths } from "@/lib/api-types.gen";

export type PersonalDraft = components["schemas"]["PersonalDraftView"];
export type PersonalDraftPage = components["schemas"]["PersonalDraftPage"];
type Decision = components["schemas"]["PersonalDraftDecision"];
type Accepted = components["schemas"]["PersonalDraftAcceptanceResponse"];
type Discarded = paths["/api/personal-drafts/{draft_id}/discard"]["post"]["responses"][200]["content"]["application/json"] & {
  draft_id: string; status: "discarded"; target_id: null; target_kind: null; replayed: boolean;
};
export type DraftDecisionResult = Accepted | Discarded;
export type DraftOperation = "accept" | "discard";
/** Created once per explicit action; every retry uses the original immutable body. */
export type DraftWrite = Readonly<{
  draftRef: string;
  expectedKind: PersonalDraft["proposal"]["kind"];
  operation: DraftOperation;
  body: Readonly<Decision>;
}>;

const BASE = "/api/python-proxy/personal-drafts";
export const DRAFT_LIST_LIMIT = 50;

export class DraftHttpError extends Error {
  constructor(readonly status: number, message: string) {
    super(message);
    this.name = "DraftHttpError";
  }
}

export function ambiguousDraftFailure(error: unknown): boolean {
  return !(error instanceof DraftHttpError) || error.status >= 500 || error.status === 408;
}

async function draftFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE}${path}`, {
    credentials: "include", cache: "no-store", ...init,
    headers: { "Content-Type": "application/json" },
  });
  if (!response.ok) {
    throw new DraftHttpError(response.status, response.status === 401
      ? "ログイン状態を確認してください"
      : response.status === 429 ? "しばらく待ってから再試行してください"
        : response.status === 409 ? "候補の状態が変わっています。再取得して確認してください"
          : `候補の操作に失敗しました（${response.status}）`);
  }
  return response.json() as Promise<T>;
}

export function draftWrite(draft: PersonalDraft, operation: DraftOperation): DraftWrite {
  return Object.freeze({
    draftRef: draft.draft_ref, expectedKind: draft.proposal.kind, operation,
    body: Object.freeze({ confirmed_digest: draft.confirmed_digest, idempotency_key: crypto.randomUUID() }),
  });
}

export const personalDraftApi = {
  list: async (cursor?: string): Promise<PersonalDraftPage> => {
    const query = new URLSearchParams({ limit: String(DRAFT_LIST_LIMIT) });
    if (cursor) query.set("cursor", cursor);
    const page = await draftFetch<PersonalDraftPage>(`?${query}`);
    if (!page || !Array.isArray(page.items) || page.items.length > DRAFT_LIST_LIMIT ||
      !(page.next_cursor === null || typeof page.next_cursor === "string" &&
        /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(page.next_cursor)) ||
      page.next_cursor === cursor || page.next_cursor !== null && !page.items.length) {
      throw new Error("候補一覧を取得できませんでした");
    }
    return page;
  },
  get: (id: string) => draftFetch<PersonalDraft>(`/${encodeURIComponent(id)}`),
  send: async (write: DraftWrite): Promise<DraftDecisionResult> => {
    const result = await draftFetch<DraftDecisionResult>(`/${encodeURIComponent(write.draftRef)}/${write.operation}`, {
      method: "POST", body: JSON.stringify(write.body),
    });
    // Invalid/lost success responses remain unknown outcomes, never a new action.
    if (result.draft_id !== write.draftRef || typeof result.replayed !== "boolean" ||
      (write.operation === "discard" ? result.status !== "discarded" || result.target_id !== null || result.target_kind !== null :
        typeof result.target_id !== "string" || !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(result.target_id) ||
        result.target_kind !== write.expectedKind ||
        !(result.status === "accepted" && result.target_kind === "inventory" ||
          result.status === "materialized_requires_verification" && result.target_kind === "deadline"))) {
      throw new Error("操作結果を照合できませんでした");
    }
    return result;
  },
};
