import type { components } from "./api-types.gen";
import { employeeRequest, EmployeeApiError } from "./agent-employees-api";
import type { PlatformContentPayload, ContentVariantRevision } from "./media-operations-content-api";
import type { PlatformAccount } from "./media-operations-setup-api";
import { accountInScope, type PersonaWorkspaceScope } from "./persona-workspace-model";

type S = components["schemas"];
export type PublicationAction = S["ActionResponse"];
export type PublicationPackage = S["MediaActionPackagePreview"];
export type ProviderReadiness = {
  platform_account_id: string; platform: string; account_status: string;
  account_revision_id: string; credential_revision: number; credential_status: string;
  verified: boolean; write_capable: boolean; read_only: boolean;
  operations: Array<{ id: string; owner_user_id: string; project_id: string; platform_account_id: string;
    account_revision_id: string; operation: string; status: string; account_eligibility: string }>;
};
const base = "/api/operations/media";
const id = encodeURIComponent;
async function request<T>(path: string, method = "GET", body?: unknown): Promise<T> {
  try { return await employeeRequest<T>(path, method, body); }
  catch (error) { if (error instanceof EmployeeApiError) throw error; throw new Error("公開経路の応答を取得できません。保存状態を照合してください。"); }
}
function readinessProjection(value: ProviderReadiness): ProviderReadiness {
  if (!value || typeof value.platform_account_id !== "string" || typeof value.account_revision_id !== "string" || typeof value.platform !== "string" || typeof value.account_status !== "string" || typeof value.credential_status !== "string" || typeof value.verified !== "boolean" || !Array.isArray(value.operations) || value.operations.some(row => !row || typeof row.operation !== "string" || typeof row.status !== "string")) throw new Error("接続先の準備状態を照合できません。接続設定を再取得してください。");
  return value;
}
export const personaPublicationApi = {
  readiness: (accountId: string) => request<ProviderReadiness>(`${base}/platform-accounts/${id(accountId)}/provider-readiness`).then(readinessProjection),
  refreshReadiness: (accountId: string) => request<ProviderReadiness>(`${base}/platform-accounts/${id(accountId)}/provider-readiness/refresh`, "POST", {}).then(readinessProjection),
  oauth: async (accountId: string) => { const value = await request<S["MediaOAuthMetadataResponse"]>(`${base}/platform-accounts/${id(accountId)}/oauth`); if (!value || !Array.isArray(value.blockers) || !Array.isArray(value.required_scopes) || !Array.isArray(value.external_checks)) throw new Error("本人接続の設定を照合できません。設定を再取得してください。"); return value; },
  startOAuth: (accountId: string, body: S["MediaOAuthStartRequest"]) => request<S["MediaOAuthStartResponse"]>(`${base}/platform-accounts/${id(accountId)}/oauth/start`, "POST", body),
  list: async (projectId: string, platform: string) => { const value = await request<S["ActionListItemResponse"][]>(`${base}/actions?${new URLSearchParams({ project_id: projectId, platform, action_type: "media.publish_content", limit: "100" })}`); if (!Array.isArray(value)) throw new Error("保存済み公開記録を照合できません。再取得してください。"); return value; },
  get: (actionId: string) => request<PublicationAction>(`${base}/actions/${id(actionId)}`),
  create: (body: S["MediaActionCreateRequest"]) => request<PublicationAction>(`${base}/actions`, "POST", body),
  package: (action: PublicationAction) => request<PublicationPackage>(`/api/operations/actions/${id(action.id)}/package?${new URLSearchParams({ expected_action_version: String(action.action_version), expected_payload_hash: String(action.payload_hash) })}`),
  approve: (actionId: string, body: S["ActionDecisionRequest"]) => request<PublicationAction>(`${base}/actions/${id(actionId)}/approve`, "POST", body),
  attempt: (actionId: string, body: S["AttemptCreateRequest"]) => request<S["AttemptResponse"]>(`${base}/actions/${id(actionId)}/attempts`, "POST", body),
  recover: (actionId: string, body: S["ProviderRecoveryRequest"]) => request<PublicationAction>(`${base}/actions/${id(actionId)}/provider-recovery`, "POST", body),
};

/** Canonical comparison ignores object key order, never array order or values. */
export function publicationValue(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(publicationValue).join(",")}]`;
  if (value && typeof value === "object") return `{${Object.entries(value).filter(([, v]) => v !== undefined).sort(([a], [b]) => a.localeCompare(b)).map(([k, v]) => `${JSON.stringify(k)}:${publicationValue(v)}`).join(",")}}`;
  return JSON.stringify(value) ?? "undefined";
}
/** Matches backend canonical_publication_payload's no-value HTTP DTO defaults.
 * Immutable hashes and every nonempty value/reference remain unchanged. */
export function publicationPayloadValue(value: unknown): string {
  if (!value || typeof value !== "object" || Array.isArray(value)) return publicationValue(value);
  const data = { ...value } as Record<string, unknown>;
  const kind = data.type;
  const nullable = kind === "x_post" ? ["alt_text", "reply_to", "scheduled_at"] : kind === "x_thread" ? ["reply_to", "scheduled_at"]
    : kind === "youtube_video" || kind === "youtube_short" ? ["scheduled_at", "audience", "disclosure"] : [];
  const lists = kind === "x_post" ? ["media", "links", "hashtags"] : kind === "youtube_video" || kind === "youtube_short" ? ["thumbnail", "captions"] : [];
  nullable.forEach(key => { if (data[key] === null) delete data[key]; });
  lists.forEach(key => { if (data[key] === undefined) data[key] = []; });
  if (kind === "x_post" && data.sensitive_content === undefined) data.sensitive_content = false;
  if (kind === "x_thread" && Array.isArray(data.posts)) data.posts = data.posts.map(post => {
    if (!post || typeof post !== "object" || Array.isArray(post)) return post;
    const saved = { ...post } as Record<string, unknown>;
    if (saved.alt_text === null) delete saved.alt_text;
    if (saved.media === undefined) saved.media = [];
    return saved;
  });
  return publicationValue(data);
}
export function publicationActionMatches(action: PublicationAction, scope: PersonaWorkspaceScope, revision: ContentVariantRevision, account: PlatformAccount, executionMode: "provider" | "manual" = "provider"): boolean {
  const source = action.payload;
  const hashes = [...new Set(revision.generation_output_refs.map(ref => ref.sha256).filter((hash): hash is string => typeof hash === "string"))];
  return action.owner_user_id === scope.ownerUserId && action.project_id === scope.projectId &&
    action.platform === revision.platform && action.action_type === "media.publish_content" && action.execution_mode === executionMode &&
    action.content_item_id === revision.content_item_id && action.content_variant_id === revision.content_variant_id &&
    action.content_variant_revision_id === revision.id && action.persona_revision_id === revision.persona_revision_id &&
    action.platform_account_id === account.id && action.platform_account_revision_id === revision.platform_account_revision_id &&
    action.connection_id === account.connection_id && typeof action.execution_key === "string" && Boolean(action.execution_key) &&
    Number.isInteger(action.version) && Number(action.version) >= 1 && Number.isInteger(action.action_version) && Number(action.action_version) >= 1 && typeof action.payload_hash === "string" && /^[a-f0-9]{64}$/.test(action.payload_hash) &&
    source?.content_variant_revision_id === revision.id && source?.content_variant_revision_hash === revision.content_hash &&
    publicationValue(action.artifact_hashes ?? []) === publicationValue(hashes) &&
    source?.content_item_id === revision.content_item_id && source?.content_item_hash === revision.content_item_hash &&
    source?.persona_revision_id === revision.persona_revision_id && source?.persona_revision_hash === revision.persona_revision_hash &&
    source?.platform_account_id === account.id && source?.platform_account_revision_id === revision.platform_account_revision_id &&
    source?.platform_account_revision_hash === revision.platform_account_revision_hash &&
    publicationPayloadValue(source?.payload) === publicationPayloadValue(revision.payload);
}
/** A receipt is historical evidence for one approved revision and its exact attempt. */
export function publicationReceiptMatches(action: PublicationAction): boolean {
  const receipt = action.receipt;
  return Boolean(receipt && typeof receipt.id === "string" && receipt.id.trim() &&
    receipt.action_id === action.id && receipt.action_version === action.action_version &&
    typeof receipt.attempt_id === "string" && action.attempts?.some(attempt =>
      attempt.id === receipt.attempt_id && attempt.action_id === action.id &&
      attempt.action_version === action.action_version && attempt.execution_key === action.execution_key));
}

/** Display all operations for this exact account; a foreign capability never grants readiness. */
export function providerOperationStates(readiness: ProviderReadiness, scope: PersonaWorkspaceScope, account: PlatformAccount): Array<{ operation: string; label: string; state: string }> {
  const bound = accountInScope(account, scope) && readiness.platform_account_id === account.id &&
    readiness.platform === account.platform && readiness.account_revision_id === account.current_revision.id;
  return [["text", "本文"], ["image", "画像添付"], ["video", "動画"], ["schedule", "予約"]].map(([operation, label]) => {
    const rows = bound ? readiness.operations.filter(row => row.operation === operation && row.owner_user_id === scope.ownerUserId &&
      row.project_id === scope.projectId && row.platform_account_id === account.id && row.account_revision_id === account.current_revision.id) : [];
    const row = rows.length === 1 ? rows[0] : null;
    const unsupported = account.platform === "x" && operation === "schedule" || account.platform === "youtube" && operation === "text";
    const state = unsupported || row?.status === "unsupported" ? "未対応" : !bound ? "接続先不一致"
      : account.status !== "active" || readiness.account_status !== "active" ? "接続先停止中"
      : !readiness.verified || readiness.credential_status !== "verified" ? "本人接続が必要"
      : row?.status === "automatable" && row.account_eligibility === "eligible" ? "利用可能"
      : row?.status === "manual" ? "本人の手動操作が必要" : row?.status === "unavailable" ? "利用不可" : "権限・媒体条件が未確認";
    return { operation, label: account.platform === "youtube" && operation === "image" ? "動画のthumbnail" : label, state };
  });
}
export function publicationPackageMatches(preview: PublicationPackage, action: PublicationAction, revision: ContentVariantRevision): boolean {
  const binding = preview.manifest.action;
  return binding.id === action.id && binding.action_version === action.action_version && binding.payload_hash === action.payload_hash &&
    preview.manifest.source.content_variant_revision_id === revision.id &&
    preview.manifest.source.content_variant_revision_hash === revision.content_hash &&
    publicationPayloadValue(preview.manifest.payload) === publicationPayloadValue(revision.payload);
}
/** Same classification as backend media_publication_contract.publication_operation. */
export function publicationOperation(payload: PlatformContentPayload): "text" | "image" | "video" | "schedule" | "unsupported" {
  if ("scheduled_at" in payload && payload.scheduled_at) return "schedule";
  if (payload.type === "x_post") return payload.media?.length ? "image" : "text";
  if (payload.type === "x_thread") return payload.posts.some(post => post.media?.length) ? "image" : "text";
  if (["youtube_video", "youtube_short", "instagram_reel", "tiktok_video"].includes(payload.type)) return "video";
  if (["pixiv_work", "instagram_carousel"].includes(payload.type)) return "image";
  if (payload.type === "patreon_post") return "text";
  return "unsupported";
}
export function providerBlocker(readiness: ProviderReadiness, scope: PersonaWorkspaceScope, revision: ContentVariantRevision, account: PlatformAccount): string {
  if (!accountInScope(account, scope) || readiness.platform_account_id !== account.id || readiness.platform !== revision.platform || readiness.account_revision_id !== revision.platform_account_revision_id) return "接続先の人格・Project・保存版が一致しません。接続設定と内容の版を確認してください。";
  if (account.status !== "active" || readiness.account_status !== "active") return "この接続先は停止中です。人格に対応するアカウントを設定して有効化してください。";
  const payload = revision.payload;
  if (revision.platform === "x" && payload.type !== "x_post" && payload.type !== "x_thread" || revision.platform === "youtube" && payload.type !== "youtube_video" && payload.type !== "youtube_short" || !["x", "youtube"].includes(revision.platform)) return "この媒体・制作形式は現在の公開経路に対応していません。";
  if ((payload.type === "x_post" || payload.type === "x_thread") && payload.scheduled_at) return "X APIの予約投稿は未対応です。この保存版は公開できません。";
  if (payload.type === "x_post" && (payload.sensitive_content || payload.alt_text) || payload.type === "x_thread" && payload.posts.some(post => post.alt_text)) return "このX公開経路はセンシティブ指定・alt textに未対応です。QA・媒体別内容で対応する保存版を用意してください。";
  if ((payload.type === "youtube_video" || payload.type === "youtube_short") && payload.captions?.length) return "この公開経路はYouTubeの字幕uploadに未対応です。この保存版は公開できません。";
  if (!readiness.verified || readiness.credential_status !== "verified") return "この人格の接続先は本人確認済みの接続がありません。下の接続設定を確認してください。";
  const operation = publicationOperation(payload);
  const label = operation === "video" ? "動画upload" : operation === "schedule" ? "予約公開" : operation === "image" ? "添付素材の投稿" : "投稿";
  const row = readiness.operations.find(r => r.operation === operation && r.owner_user_id === scope.ownerUserId && r.project_id === scope.projectId && r.platform_account_id === account.id && r.account_revision_id === revision.platform_account_revision_id);
  if (row?.status === "unsupported") return `この接続先の${label}は現在の公開経路に対応していません。`;
  if (!row || row.status !== "automatable" || row.account_eligibility !== "eligible") return `この接続先の${label}権限・媒体条件が未確認です。本人接続後に準備状態を更新してください。`;
  return "";
}
