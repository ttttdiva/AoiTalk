import type { components } from "./api-types.gen";
import { employeeRequest } from "./agent-employees-api";
import type { PlatformAccount } from "./media-operations-setup-api";

type S = components["schemas"];
export type BridgeConnectionPreviewRequest = S["MediaBridgeConnectionPreviewRequest"];
export type BridgeConnectionPreview = S["MediaBridgeConnectionPreviewResponse"];
export type BridgeConnectionSave = S["MediaBridgeConnectionSaveResponse"];
const base = "/api/operations/media/platform-accounts";
const hash = (value: unknown): value is string => typeof value === "string" && /^[a-f0-9]{64}$/u.test(value);
const canonicalRef = (platform: string, value: string) => platform === "x" ? value.toLowerCase() : value;
const labelValid = (value: unknown): value is string => typeof value === "string" && value.trim().length >= 1 && value.trim().length <= 80;
const uuidValid = (value: unknown): value is string => typeof value === "string" && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/u.test(value) && value !== "00000000-0000-0000-0000-000000000000";
const contextValid = (value: unknown): value is { context_id: string; context_epoch: string } => {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const keys = Object.keys(value);
  const context = value as Record<string, unknown>;
  return keys.length === 2 && keys.every(key => key === "context_id" || key === "context_epoch") && uuidValid(context.context_id) && uuidValid(context.context_epoch);
};

export function bridgeConnectionLabelsValid(platform: string, browserLabel: unknown, googleLabel: unknown): boolean {
  return labelValid(browserLabel) && (platform === "x" ? googleLabel === null || googleLabel === undefined : platform === "youtube" && labelValid(googleLabel));
}

export function bridgeConnectionRefValid(platform: string, value: string): boolean {
  return platform === "x" ? /^@[A-Za-z0-9_]{1,15}$/u.test(value)
    : platform === "youtube" && /^UC[A-Za-z0-9_-]{22}$/u.test(value);
}

export function bridgeConnectionPreviewMatches(value: BridgeConnectionPreview, account: PlatformAccount, input: BridgeConnectionPreviewRequest): boolean {
  return Boolean(value && value.mode === "bridge_manual" && value.official_api_verified === false && value.publication_confirmed === false
    && bridgeConnectionLabelsValid(account.platform, input.browser_context_label, input.google_account_label)
    && value.browser_context_label === input.browser_context_label.trim()
    && value.google_account_label === (typeof input.google_account_label === "string" ? input.google_account_label.trim() : null)
    && contextValid(value.browser_context)
    && account.owner_user_id === input.owner_user_id && account.project_id === input.project_id && account.persona_id === input.persona_id
    && account.current_revision.id === input.expected_account_revision_id && account.current_revision.content_hash === input.expected_account_revision_hash
    && value.owner_user_id === input.owner_user_id && value.project_id === input.project_id && value.persona_id === input.persona_id
    && value.platform_account_id === account.id && value.platform_account_revision_id === input.expected_account_revision_id
    && value.platform_account_revision_hash === input.expected_account_revision_hash && hash(value.platform_account_revision_hash)
    && value.platform === account.platform && value.account_ref === canonicalRef(account.platform, input.account_ref) && bridgeConnectionRefValid(value.platform, value.account_ref)
    && value.device_id === input.device_id && value.tab_id === input.tab_id
    && typeof value.persona_display_name === "string" && typeof value.account_display_name === "string"
    && typeof value.preview_token === "string" && value.preview_token.length > 0
    && Number.isFinite(Date.parse(value.checked_at)) && Number.isFinite(Date.parse(value.expires_at))
    && Date.parse(value.expires_at) > Date.parse(value.checked_at) && Date.parse(value.expires_at) > Date.now());
}

export function bridgeConnectionSaveMatches(value: BridgeConnectionSave, account: PlatformAccount, preview: BridgeConnectionPreview): boolean {
  const saved = value?.account;
  const receipt = value?.binding_receipt;
  return Boolean(value && value.mode === "bridge_manual" && value.official_api_verified === false && value.publication_confirmed === false
    && value.readback_binding_matches === true && receipt
    && bridgeConnectionLabelsValid(preview.platform, preview.browser_context_label, preview.google_account_label)
    && receipt.browser_context_label === preview.browser_context_label && receipt.google_account_label === preview.google_account_label
    && contextValid(preview.browser_context) && contextValid(receipt.browser_context)
    && receipt.browser_context.context_id === preview.browser_context.context_id && receipt.browser_context.context_epoch === preview.browser_context.context_epoch
    && receipt.owner_user_id === preview.owner_user_id && receipt.project_id === preview.project_id && receipt.persona_id === preview.persona_id
    && receipt.platform_account_id === account.id && receipt.platform === preview.platform && receipt.account_ref === preview.account_ref
    && receipt.device_id === preview.device_id && receipt.tab_id === preview.tab_id
    && Number.isSafeInteger(receipt.connection_version) && receipt.connection_version > 0 && Number.isFinite(Date.parse(receipt.saved_at))
    && saved && saved.id === account.id && saved.owner_user_id === preview.owner_user_id && saved.project_id === preview.project_id
    && saved.persona_id === preview.persona_id && saved.platform === preview.platform && saved.account_ref === preview.account_ref
    && saved.status === "active" && typeof saved.connection_id === "string" && saved.connection_id.length > 0
    && saved.current_revision && saved.current_revision.platform_account_id === account.id
    && saved.current_revision.owner_user_id === preview.owner_user_id && saved.current_revision.project_id === preview.project_id
    && saved.current_revision.id !== preview.platform_account_revision_id && typeof saved.current_revision.id === "string" && saved.current_revision.id.length > 0
    && saved.current_revision.id === receipt.binding_revision_id && saved.current_revision.content_hash === receipt.account_revision_hash
    && saved.connection_id === receipt.connection_id && hash(saved.current_revision.content_hash));
}

export const bridgeConnectionApi = {
  async preview(account: PlatformAccount, input: BridgeConnectionPreviewRequest): Promise<BridgeConnectionPreview> {
    if (!bridgeConnectionRefValid(account.platform, input.account_ref) || !input.device_id || input.device_id.length > 120
      || !Number.isSafeInteger(input.tab_id) || input.tab_id < 1 || !input.persona_id || !input.owner_user_id
      || account.owner_user_id !== input.owner_user_id || account.project_id !== input.project_id || account.persona_id !== input.persona_id
      || account.current_revision.id !== input.expected_account_revision_id || account.current_revision.content_hash !== input.expected_account_revision_hash
      || !hash(input.expected_account_revision_hash) || !bridgeConnectionLabelsValid(account.platform, input.browser_context_label, input.google_account_label)) throw new Error("本人の人格・既存枠・接続先・PC・タブとprofileの識別ラベルを明示選択してください。");
    const value = await employeeRequest<BridgeConnectionPreview>(`${base}/${encodeURIComponent(account.id)}/bridge-connection/preview`, "POST", { ...input, account_ref: canonicalRef(account.platform, input.account_ref), browser_context_label: input.browser_context_label.trim(), google_account_label: typeof input.google_account_label === "string" ? input.google_account_label.trim() : null });
    if (!bridgeConnectionPreviewMatches(value, account, input)) throw new Error("接続previewの人格・既存枠・接続先・PC・タブ・版を照合できません。入力を確認して再照合してください。");
    return value;
  },
  async save(account: PlatformAccount, preview: BridgeConnectionPreview, idempotencyKey: string): Promise<BridgeConnectionSave> {
    const input: BridgeConnectionPreviewRequest = { owner_user_id: preview.owner_user_id, project_id: preview.project_id, persona_id: preview.persona_id,
      expected_account_revision_id: preview.platform_account_revision_id, expected_account_revision_hash: preview.platform_account_revision_hash,
      account_ref: preview.account_ref, device_id: preview.device_id, tab_id: preview.tab_id, browser_context_label: preview.browser_context_label, google_account_label: preview.google_account_label };
    if (!bridgeConnectionPreviewMatches(preview, account, input) || !idempotencyKey) throw new Error("接続previewが失効または変更されています。現在の接続先を確認してください。");
    const body: S["MediaBridgeConnectionSaveRequest"] = { preview_token: preview.preview_token, confirmed: true, idempotency_key: idempotencyKey };
    const value = await employeeRequest<BridgeConnectionSave>(`${base}/${encodeURIComponent(account.id)}/bridge-connection`, "POST", body);
    if (!bridgeConnectionSaveMatches(value, account, preview)) throw new Error("接続の保存結果を照合できません。workspaceを更新して現在の接続先を確認してください。");
    return value;
  },
};
