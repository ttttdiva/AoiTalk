import type { components } from "./api-types.gen";
import { employeeRequest } from "./agent-employees-api";
import type { ContentVariantRevision } from "./media-operations-content-api";
import type { PlatformAccount } from "./media-operations-setup-api";
import { publicationValue } from "./media-persona-publication-api";
import type { PersonaWorkspaceScope } from "./persona-workspace-model";

type S = components["schemas"];
const base = "/api/operations/media";
export const productApi = {
  prepare: (body: S["MediaActionCreateRequest"]) => employeeRequest<S["ActionResponse"]>(`${base}/product-actions`, "POST", body),
  listActions: async (projectId: string, platform: string) => {
    const value = await employeeRequest<S["ActionListItemResponse"][]>(`${base}/actions?${new URLSearchParams({ project_id: projectId, platform, action_type: "media.release_product", limit: "100" })}`);
    if (!Array.isArray(value) || value.some(row => !row || typeof row !== "object" || typeof row.id !== "string")) throw new Error("商品Actionの保存記録を照合できません。更新して再確認してください。");
    return value;
  },
  revenue: async (projectId: string, offset = 0) => {
    const value = await employeeRequest<unknown[]>(`${base}/revenue-events?${new URLSearchParams({ project_id: projectId, limit: "100", offset: String(offset) })}`);
    if (!Array.isArray(value)) throw new Error("売上記録を照合できません。更新して再確認してください。");
    return value;
  },
};
export function productActionMatches(action: S["ActionResponse"], scope: PersonaWorkspaceScope, revision: ContentVariantRevision, account: PlatformAccount): boolean {
  const normalized = (payload: unknown) => publicationValue(payload && typeof payload === "object" ? { thumbnail_assets: [], ...payload } : payload);
  return action.owner_user_id === scope.ownerUserId && action.project_id === scope.projectId && action.action_type === "media.release_product" && action.execution_mode === "manual" && action.platform === revision.platform &&
    typeof action.id === "string" && Boolean(action.id) && typeof action.payload_hash === "string" && /^[a-f0-9]{64}$/u.test(action.payload_hash) && Number(action.action_version) >= 1 && Number.isInteger(action.action_version) && Number.isInteger(action.version) && Number(action.version) >= 1 &&
    action.content_item_id === revision.content_item_id && action.content_variant_id === revision.content_variant_id && action.content_variant_revision_id === revision.id &&
    action.persona_revision_id === revision.persona_revision_id && action.platform_account_id === account.id && action.platform_account_revision_id === revision.platform_account_revision_id &&
    action.connection_id === account.connection_id && action.payload?.content_variant_revision_hash === revision.content_hash &&
    action.payload?.content_item_id === revision.content_item_id && action.payload?.content_item_hash === revision.content_item_hash &&
    action.payload?.persona_revision_id === revision.persona_revision_id && action.payload?.persona_revision_hash === revision.persona_revision_hash &&
    action.payload?.platform_account_id === account.id && action.payload?.platform_account_revision_id === revision.platform_account_revision_id && action.payload?.platform_account_revision_hash === revision.platform_account_revision_hash &&
    normalized(action.payload?.payload) === normalized(revision.payload);
}
/** A display projection from the existing generic revenue response, not a new ledger. */
export function productRevenueRows(values: unknown[], scope: PersonaWorkspaceScope, revision: ContentVariantRevision, account: PlatformAccount) {
  return values.filter((v): v is Record<string, unknown> => Boolean(v && typeof v === "object" && !Array.isArray(v)))
    .filter(v => v.owner_user_id === scope.ownerUserId && v.project_id === scope.projectId && v.platform === revision.platform && [account.id, account.account_ref].includes(String(v.platform_account_ref)) &&
      (v.persona_ref == null || v.persona_ref === scope.personaId) &&
      [revision.content_item_id, revision.content_variant_id].includes(String(v.content_ref)) && typeof v.id === "string");
}
