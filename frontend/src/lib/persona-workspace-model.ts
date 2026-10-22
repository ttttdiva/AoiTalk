import type { MediaPersona } from "./media-operations-api";
import type { ContentVariant } from "./media-operations-content-api";
import type { PlatformAccount } from "./media-operations-setup-api";

/** Every production surface is pinned to an authenticated, server-owned persona. */
export type PersonaWorkspaceScope = {
  personaId: string;
  personaRevisionId: string;
  ownerUserId: string;
  projectId: string;
  displayName: string;
};

export function personaScope(persona: MediaPersona): PersonaWorkspaceScope | null {
  const revision = persona.current_revision;
  if (!persona.project_id || !persona.owner_user_id || !revision?.id ||
    revision.persona_id !== persona.id || revision.project_id !== persona.project_id ||
    revision.owner_user_id !== persona.owner_user_id) return null;
  return { personaId: persona.id, personaRevisionId: revision.id,
    ownerUserId: persona.owner_user_id, projectId: persona.project_id,
    displayName: revision.display_name };
}

export function accountInScope(account: PlatformAccount, scope: PersonaWorkspaceScope): boolean {
  return account.persona_id === scope.personaId && account.project_id === scope.projectId &&
    account.owner_user_id === scope.ownerUserId &&
    account.current_revision?.platform_account_id === account.id &&
    account.current_revision.owner_user_id === scope.ownerUserId &&
    account.current_revision.project_id === scope.projectId;
}

export function variantInScope(variant: ContentVariant, scope: PersonaWorkspaceScope, accounts: PlatformAccount[]): boolean {
  const revision = variant.current_revision;
  if (!revision || variant.owner_user_id !== scope.ownerUserId || variant.project_id !== scope.projectId ||
    revision.persona_revision_id !== scope.personaRevisionId || revision.owner_user_id !== scope.ownerUserId ||
    revision.project_id !== scope.projectId || revision.content_variant_id !== variant.id ||
    revision.content_item_id !== variant.content_item_id || revision.platform !== variant.platform) return false;
  return accounts.some(account => account.id === revision.platform_account_id &&
    account.platform === revision.platform && accountInScope(account, scope));
}

export function workspaceHref(personaId: string, stage?: string): string {
  const query = new URLSearchParams({ tab: "studio", persona: personaId });
  if (stage) query.set("stage", stage);
  return `/operations?${query}`;
}

export function safeReceiptUrl(value: string | null | undefined, platform: string): string | null {
  if (!value) return null;
  try {
    const url = new URL(value);
    const hosts = platform === "x" ? ["x.com", "www.x.com", "twitter.com", "www.twitter.com"]
      : platform === "youtube" ? ["www.youtube.com", "youtube.com", "youtu.be"] : [];
    return url.protocol === "https:" && !url.username && !url.password && hosts.includes(url.hostname) ? url.href : null;
  } catch { return null; }
}

export function publicationBlockerMessage(code: string): string {
  const messages: Record<string, string> = {
    revision_missing: "媒体別内容の保存済み版がありません。QA・媒体別内容で保存してください。",
    account_binding_missing: "公開する接続先アカウントをQA・媒体別内容で選択してください。",
    qa_missing: "この版のQAを記録してください。",
    qa_failed: "この版のQAで指摘された項目を修正してください。",
    qa_review_required: "この版のQAを確認し、判定を記録してください。",
    rights_missing: "この版の素材・権利確認を記録してください。",
    rights_blocked: "この版の素材・権利確認で公開が止まっています。権利を確認してください。",
    rights_review_required: "この版の素材・権利確認に人の確認が必要です。",
    rights_unknown: "この版の素材・権利を確認してください。",
    generation_output_missing: "保存した制作素材が見つかりません。素材の保存状態を確認してください。",
    generation_output_scope_mismatch: "制作素材の所属がこの人格のProjectと一致しません。",
    generation_output_hash_mismatch: "制作素材の内容hashが保存した版と一致しません。",
  };
  if (messages[code]) return messages[code];
  if (/^(?:qa|rights|revision)_(?:variant_mismatch|revision_mismatch|hash_invalid)$/u.test(code)) return "保存内容とQA・権利記録の版またはhashが一致しません。QA・媒体別内容で確認してください。";
  return `公開前に確認が必要な項目: ${code}`;
}

export const PERSONA_WORKSPACE_STAGES = [
  { id: "brief", label: "制作brief", detail: "企画と目的" },
  { id: "generation", label: "計画・生成", detail: "生成／既存output" },
  { id: "qa", label: "QA・媒体別内容", detail: "内容と権利を確認" },
  { id: "publish", label: "preview・公開", detail: "本人操作と記録" },
] as const;
export type PersonaWorkspaceStage = typeof PERSONA_WORKSPACE_STAGES[number]["id"];
