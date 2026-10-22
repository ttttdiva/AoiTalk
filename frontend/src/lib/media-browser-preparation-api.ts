import type { components } from "./api-types.gen";
import { employeeRequest } from "./agent-employees-api";
import { accountInScope, type PersonaWorkspaceScope } from "./persona-workspace-model";
import type { ContentVariantRevision } from "./media-operations-content-api";
import type { PlatformAccount } from "./media-operations-setup-api";
import { publicationValue, type PublicationAction } from "./media-persona-publication-api";

type S = components["schemas"];
export type BrowserPreparation = S["MediaBrowserPreparationResponse"];
export type BridgeDevice = { id: string; name: string; online: boolean; edge_connected: boolean; browser_backend: string | null; capabilities: string[]; media_preparation_backend?: string | null; media_preparation_connected?: boolean };
export const browserPreparationDeviceReady = (device: BridgeDevice | undefined): boolean => Boolean(device?.online &&
  (device.media_preparation_backend === "managed_profile"
    ? device.media_preparation_connected === true && device.capabilities.includes("media_prepare")
    : device.media_preparation_backend == null && device.browser_backend === "extension" && device.edge_connected && device.capabilities.includes("browser")));
export type BrowserPreparationTarget = { owner_user_id: string; project_id: string | null; persona_id: string; platform_account_id: string; platform_account_revision_id: string; platform: string; device_id: string; tab_id: number };
export type BrowserPreparationAvailability = { available: boolean; source_available: boolean; publication_confirmation_allowed: false; execution_mode: "manual"; blockers: string[]; readiness_target: (BrowserPreparationTarget & { checked_at: string }) | null };
const base = "/api/operations/media";
const id = encodeURIComponent;
const object = (value: unknown): value is Record<string, unknown> => Boolean(value && typeof value === "object" && !Array.isArray(value));

export const browserPreparationApi = {
  async availability(platform: string, target?: BrowserPreparationTarget): Promise<BrowserPreparationAvailability> {
    if (target && (target.platform !== platform || !target.owner_user_id || !target.persona_id || !target.platform_account_id || !target.platform_account_revision_id || !target.device_id || target.device_id.length > 120 || !Number.isSafeInteger(target.tab_id) || target.tab_id < 1)) throw new Error("本人のaccount・PC・既存タブを明示選択してください。");
    const query = target ? `?${new URLSearchParams({ platform_account_id: target.platform_account_id, device_id: target.device_id, tab_id: String(target.tab_id) })}` : "";
    const result = await employeeRequest<S["MediaAdapterStatusResponse"]>(`${base}/adapter-status${query}`);
    const browser = result?.platforms?.[platform]?.browser_publication;
    const value = object(browser) ? browser.preparation : null;
    if (!object(value) || typeof value.available !== "boolean" || typeof value.source_available !== "boolean" || value.execution_mode !== "manual" || value.publication_confirmation_allowed !== false || value.required_terminal_stage !== "awaiting_final_confirmation" || value.prepare_operation_id !== "media_operations_prepare_browser_action" || value.get_operation_id !== "media_operations_get_browser_preparation" || value.inspect_operation_id !== "media_operations_inspect_browser_preparation" || !Array.isArray(value.blockers) || value.blockers.some(b => typeof b !== "string")) throw new Error("Bridge準備経路の契約と利用可否を照合できません。");
    const checked = value.readiness_target;
    if ((value.available && (!target || !object(checked) || value.blockers.length || !value.source_available)) || (target && checked != null && (!object(checked) || Object.entries(target).some(([key, expected]) => checked[key] !== expected) || typeof checked.checked_at !== "string" || !Number.isFinite(Date.parse(checked.checked_at))))) throw new Error("Bridge準備の照会結果と本人の人格・account・PC・タブが一致しません。対象を選び直して利用状態を更新してください。");
    if (!target && checked != null) throw new Error("対象未選択のBridge準備状態を利用可能として扱えません。");
    return value as BrowserPreparationAvailability;
  },
  async devices(): Promise<BridgeDevice[]> {
    const value = await employeeRequest<S["BridgeReply"]>("/api/pc-bridge/devices");
    const devices = value?.result?.devices;
    if (!Array.isArray(devices) || devices.some(d => !object(d) || typeof d.id !== "string" || typeof d.name !== "string" || typeof d.online !== "boolean" || typeof d.edge_connected !== "boolean" || !Array.isArray(d.capabilities))) throw new Error("本人の接続済みPC一覧を照合できません。");
    return devices as BridgeDevice[];
  },
  get: (actionId: string) => employeeRequest<S["MediaBrowserPreparationEnvelope"]>(`${base}/actions/${id(actionId)}/browser-preparation`),
  prepare: (actionId: string, body: S["MediaBrowserPreparationRequest"]) => employeeRequest<BrowserPreparation>(`${base}/actions/${id(actionId)}/browser-preparation`, "POST", body),
  inspect: (actionId: string, body: S["MediaBrowserPreparationInspectRequest"]) => employeeRequest<BrowserPreparation>(`${base}/actions/${id(actionId)}/browser-preparation/inspect`, "POST", body),
};

export function browserPreparationReadinessMessage(blockers: readonly string[]): string {
  if (blockers.includes("browser_preparation_legacy_revision_unverified")) return "このタブの旧拡張機能による準備証跡は安全性を確認できません。拡張機能を更新し、本人が新しい制作タブを手動で開いてaccountを確認してください。新しいActionの作成前にそのタブを選択してください。保存済み操作を自動で再送しません。";
  if (blockers.includes("browser_preparation_new_document_required")) return "このタブには以前の準備操作が残っています。本人が新しい制作タブを手動で開いてaccountを確認し、新しいActionの作成前にそのタブを選択してください。保存済み記録は読み取りで照合でき、入力を自動で再送しません。";
  if (blockers.includes("browser_explicit_owner_device_tab_required")) return "本人の接続済みPCと、本人が開いた既存タブIDをAction作成前に明示選択してください。";
  if (blockers.includes("browser_preparation_checkpoint_key_unavailable")) return "準備証跡の安全な保存設定を確認できません。管理者にBridgeの暗号化設定を確認してください。";
  if (blockers.some(code => code.includes("update_required"))) return "対象PCのBridgeと拡張機能を更新してから、本人が開いた制作タブの利用状態を更新してください。";
  if (blockers.includes("browser_prepare_device_busy")) return "対象PCで別の操作が進行中です。完了後に利用状態を更新してください。";
  return "選択したaccount・PC・タブで安全に準備できることを確認できません。本人の制作画面とBridge接続を確認して利用状態を更新してください。";
}

export function browserPreparationBlocker(scope: PersonaWorkspaceScope, revision: ContentVariantRevision, account: PlatformAccount): string {
  if (!accountInScope(account, scope) || account.current_revision.id !== revision.platform_account_revision_id || account.current_revision.content_hash !== revision.platform_account_revision_hash || account.platform !== revision.platform) return "接続先の人格・Project・保存版が一致しません。現在の接続先を選び直してください。";
  if (account.status !== "active" || !account.connection_id) return "この人格に対応する接続先を設定して有効化してください。";
  const payload = revision.payload;
  if (revision.platform === "x") {
    if (!/^@[A-Za-z0-9_]{1,15}$/.test(account.account_ref)) return "Xの接続先には本人の明示的な@handleが必要です。数値IDからアカウントを推測できません。";
    if (payload.type !== "x_post" || (payload.media?.length ?? 0) > 1 || payload.reply_to || payload.scheduled_at || payload.sensitive_content || payload.alt_text) return "Bridge準備はX本文と単一画像に対応します。thread・返信・予約・センシティブ指定・alt textは未対応です。";
  } else if (revision.platform === "youtube") {
    if (!/^UC[A-Za-z0-9_-]{22}$/.test(account.account_ref)) return "YouTubeの接続先には本人の明示的なUC channel IDが必要です。個人アカウントを人格へ自動割当できません。";
    if (payload.type !== "youtube_video" || payload.visibility !== "private" || payload.audience !== "not_made_for_kids" || payload.tags?.length || payload.media_asset.length !== 1 || payload.thumbnail?.length || payload.captions?.length || payload.disclosure || payload.scheduled_at) return "Bridge準備はprivate動画・子ども向けではない設定・空のtagsに対応します。Shorts・追加画像・字幕・開示・予約・他の公開範囲は未対応です。";
  } else return "この媒体のBridge準備経路は未対応です。";
  return "";
}

/** backendと同じX本文・YouTube canonical metadataのUTF-8 hashを照合する。 */
export async function browserPreparationContentHash(revision: ContentVariantRevision): Promise<string> {
  const payload = revision.payload;
  let value: string;
  if (payload.type === "x_post") {
    value = [payload.text.trim(), ...(payload.hashtags ?? []).filter(t => t.trim()).map(t => `#${t.trim().replace(/^#+/, "")}`), ...(payload.links ?? []).filter(t => t.trim()).map(t => t.trim())].join(" ").trim();
  } else if (payload.type === "youtube_video") value = publicationValue({ title: payload.title, description: payload.description, tags: [], visibility: "private", audience: "not_made_for_kids" });
  else throw new Error("Bridgeの本文・metadataを照合できません。");
  const bytes = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
  return Array.from(new Uint8Array(bytes), b => b.toString(16).padStart(2, "0")).join("");
}

export function browserPreparationMatches(value: BrowserPreparation, action: PublicationAction, scope: PersonaWorkspaceScope, revision: ContentVariantRevision, account: PlatformAccount, expectedDigest: string): boolean {
  return Boolean(value && value.owner_user_id === scope.ownerUserId && value.project_id === scope.projectId && value.action_id === action.id && value.action_version === action.action_version && value.payload_hash === action.payload_hash && value.platform === revision.platform && value.platform_account_id === account.id && value.platform_account_revision_id === revision.platform_account_revision_id && value.publication_confirmed === false && value.automatic_retry_allowed === false && typeof value.current_action_matches === "boolean" && typeof value.current_approval_valid === "boolean" && Number.isInteger(value.action_state_version) && value.action_state_version > 0 && (!value.current_action_matches || value.action_state_version === action.version) && (!value.current_approval_valid || action.status === "approved") && value.content_sha256 === expectedDigest && (revision.platform === "x" ? value.text_sha256 === expectedDigest : value.metadata_sha256 === expectedDigest) && typeof value.id === "string" && typeof value.device_id === "string" && Number.isInteger(value.tab_id) && value.tab_id > 0 && Number.isInteger(value.version) && value.version > 0 && ["preparing", "awaiting_final_confirmation", "uncertain", "blocked"].includes(value.status));
}
