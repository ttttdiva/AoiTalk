import type { PersonaWorkspaceScope } from "./persona-workspace-model";

/** Local guard contains references only. The server remains the publication authority. */
export type PublicationSubmissionMarker = {
  idempotencyKey: string;
  revisionId: string;
  contentHash: string;
  accountId: string;
  publicationId: string | null;
  state: "submitting" | "unknown" | "recorded";
  phase?: "intent" | "approve" | "execute";
};

export function submissionStorageKey(scope: PersonaWorkspaceScope, accountId: string, contentHash: string): string {
  return `aoitalk-publication-v1:${encodeURIComponent(scope.ownerUserId)}:${encodeURIComponent(scope.projectId)}:${encodeURIComponent(scope.personaId)}:${encodeURIComponent(accountId)}:${encodeURIComponent(contentHash)}`;
}

export function readSubmissionMarker(key: string): PublicationSubmissionMarker | null {
  const raw = localStorage.getItem(key);
  if (!raw) return null;
  const value: unknown = JSON.parse(raw);
  if (!value || typeof value !== "object") throw new Error("公開記録の照合が必要です。");
  const marker = value as Partial<PublicationSubmissionMarker>;
  if (typeof marker.idempotencyKey !== "string" || typeof marker.revisionId !== "string" || typeof marker.contentHash !== "string" ||
    typeof marker.accountId !== "string" || !(marker.publicationId === null || typeof marker.publicationId === "string") ||
    !["submitting", "unknown", "recorded"].includes(String(marker.state)) ||
    (marker.phase !== undefined && !["intent", "approve", "execute"].includes(marker.phase))) throw new Error("公開記録の照合が必要です。");
  return marker as PublicationSubmissionMarker;
}

export function writeSubmissionMarker(key: string, marker: PublicationSubmissionMarker): void {
  localStorage.setItem(key, JSON.stringify(marker));
  // Fail closed if the browser cannot preserve the guard across a reload.
  if (localStorage.getItem(key) !== JSON.stringify(marker)) throw new Error("公開操作の記録を保存できません。ブラウザの保存設定を確認してください。");
}

/** Serialize publication clicks between tabs when supported by the browser. */
export async function withPublicationLock<T>(key: string, action: () => Promise<T>): Promise<T> {
  if (navigator.locks) return navigator.locks.request(key, action);
  throw new Error("複数タブの重複公開を防ぐ機能を利用できません。対応したブラウザで公開してください。");
}
