/**
 * Mobile app configuration.
 */

export const PUBLIC_API_URL =
  process.env.EXPO_PUBLIC_AOITALK_API_URL?.trim() || "";

export const DEFAULT_API_URL: string = __DEV__
  ? process.env.EXPO_PUBLIC_AOITALK_DEV_API_URL?.trim() || PUBLIC_API_URL
  : PUBLIC_API_URL;

/**
 * Public AoiTalk Gateway contract.  Keep the portal origin and service path
 * separate: the browser authorization page is rooted at the portal while
 * AoiTalk API traffic is served below `/at`.
 */
export const DEFAULT_PORTAL_URL = "https://az-vnetspace.com/";
export const DEFAULT_GATEWAY_SERVICE_PATH = "/at";
export const GATEWAY_CLIENT_ID = "aoitalk-android";
export const GATEWAY_SERVICE_ID = "at";
export const GATEWAY_CALLBACK_PATH = "/.gateway/mobile/callback/aoitalk";
export const GATEWAY_REDIRECT_URI = `${DEFAULT_PORTAL_URL.replace(/\/+$/, "")}${GATEWAY_CALLBACK_PATH}`;

/**
 * Accept only the verified HTTPS App Link callback carrying an OAuth
 * response in its fragment. Query parameters are rejected so code/state/iss
 * cannot be moved onto a URL that a proxy or server log would expose.
 */
export function isGatewayCallbackUrl(value: string): boolean {
  try {
    const actual = new URL(value);
    const expected = new URL(GATEWAY_REDIRECT_URI);
    return actual.protocol === "https:"
      && actual.origin === expected.origin
      && actual.pathname === GATEWAY_CALLBACK_PATH
      && !actual.username
      && !actual.password
      && !actual.search
      && actual.hash.length > 1;
  } catch {
    return false;
  }
}

// Public GitHub release metadata used by the in-app updater.
export const UPDATE_CHECK_URL =
  "https://raw.githubusercontent.com/ttttdiva/AoiTalk/main/latest.json";

export const STORAGE_KEYS = {
  ACCESS_TOKEN: "aoitalk_access_token",
  AUTH_MODE: "aoitalk_auth_mode",
  API_URL: "aoitalk_api_url",
  SELECTED_PROJECT_ID: "aoitalk_selected_project_id",
  SELECTED_SPACE_ID: "aoitalk_selected_space_id",
  CURRENT_CHARACTER_SLUG: "aoitalk_default_character_name",
  // チャット背景アバターは明示的なONだけを復元する（未設定はOFF）。
  CHAT_AVATAR_ENABLED: "aoitalk_chat_avatar_enabled",
  // チャット背景アニメーションは端末単位で保存し、未設定時はON。
  CHAT_WAVE_BACKGROUND_ENABLED: "aoitalk_chat_wave_background_enabled",
  // 送信前の意図確認は明示的なONだけを復元する（未設定はOFF）。
  CHAT_INTENT_CONFIRMATION_ENABLED: "aoitalk_chat_intent_confirmation_enabled",
  // メインスロットのプロバイダー選択（"server" | direct各種）。
  CHAT_LLM_PROVIDER: "aoitalk_chat_llm_provider",
  // メインスロットのモデルID（スロット単位）。
  CHAT_LLM_MAIN_MODEL: "aoitalk_chat_llm_main_model",
  // メインの Direct スロットで使う reasoning effort。
  CHAT_LLM_MAIN_EFFORT: "aoitalk_chat_llm_main_effort",
  // フォールバック設定（独立オブジェクト）。
  CHAT_LLM_FALLBACK_ENABLED: "aoitalk_chat_llm_fallback_enabled",
  CHAT_LLM_FALLBACK_PROVIDER: "aoitalk_chat_llm_fallback_provider",
  CHAT_LLM_FALLBACK_MODEL: "aoitalk_chat_llm_fallback_model",
  CHAT_LLM_FALLBACK_EFFORT: "aoitalk_chat_llm_fallback_effort",
  // クリップ取り込み専用スロット（個別指定が無効ならメインを使う）。
  CHAT_LLM_CLIP_INGEST_ENABLED: "aoitalk_chat_llm_clip_ingest_enabled",
  CHAT_LLM_CLIP_INGEST_PROVIDER: "aoitalk_chat_llm_clip_ingest_provider",
  CHAT_LLM_CLIP_INGEST_MODEL: "aoitalk_chat_llm_clip_ingest_model",
  CHAT_LLM_CLIP_INGEST_EFFORT: "aoitalk_chat_llm_clip_ingest_effort",
  // 新設定へ移行済みかどうかのフラグ。
  CHAT_LLM_SLOT_MIGRATED: "aoitalk_chat_llm_slot_migrated",
  // プロバイダー別のモデル一覧キャッシュ（AsyncStorage・モデルIDのみ。APIキーは保持しない）。
  CHAT_LLM_MODEL_CATALOG_CACHE: "aoitalk_chat_llm_model_catalog_cache",
  // chat composerの候補・選択・未同期mode。server URL + account scope別のsuffixを付ける。
  CHAT_LLM_UI_PREFERENCES_PREFIX: "aoitalk_chat_llm_ui_preferences_v1",
  // 未送信ChatComposer本文。server/user/conversation scopeをkeyへ含める。
  CHAT_COMPOSER_DRAFT_PREFIX: "aoitalk_chat_composer_draft_v1",
  // プロバイダー単位の共有プロファイル（APIキー / Base URL）。
  // モデルはスロット単位へ移行したため、ここではモデルを保持しない。
  CHAT_LLM_OPENAI_API_KEY: "aoitalk_chat_llm_openai_api_key",
  CHAT_LLM_OPENAI_BASE_URL: "aoitalk_chat_llm_openai_base_url",
  CHAT_LLM_GEMINI_API_KEY: "aoitalk_chat_llm_gemini_api_key",
  CHAT_LLM_GEMINI_BASE_URL: "aoitalk_chat_llm_gemini_base_url",
  CHAT_LLM_KIMI_API_KEY: "aoitalk_chat_llm_kimi_api_key",
  CHAT_LLM_KIMI_BASE_URL: "aoitalk_chat_llm_kimi_base_url",
  CHAT_LLM_DEEPSEEK_API_KEY: "aoitalk_chat_llm_deepseek_api_key",
  CHAT_LLM_DEEPSEEK_BASE_URL: "aoitalk_chat_llm_deepseek_base_url",
  CHAT_LLM_DEEPINFRA_API_KEY: "aoitalk_chat_llm_deepinfra_api_key",
  CHAT_LLM_DEEPINFRA_BASE_URL: "aoitalk_chat_llm_deepinfra_base_url",
  CHAT_LLM_OPENROUTER_API_KEY: "aoitalk_chat_llm_openrouter_api_key",
  CHAT_LLM_OPENROUTER_BASE_URL: "aoitalk_chat_llm_openrouter_base_url",
  CHAT_LLM_ANTHROPIC_API_KEY: "aoitalk_chat_llm_anthropic_api_key",
  CHAT_LLM_ANTHROPIC_BASE_URL: "aoitalk_chat_llm_anthropic_base_url",
  CHAT_LLM_CUSTOM_API_KEY: "aoitalk_chat_llm_custom_api_key",
  CHAT_LLM_CUSTOM_BASE_URL: "aoitalk_chat_llm_custom_base_url",
  // --- 以下は移行元として読むだけのレガシーキー（新規書き込みしない） ---
  CHAT_LLM_API_KEY: "aoitalk_chat_llm_api_key",
  CHAT_LLM_MODEL: "aoitalk_chat_llm_model",
  CHAT_LLM_BASE_URL: "aoitalk_chat_llm_base_url",
  CHAT_LLM_OPENAI_MODEL: "aoitalk_chat_llm_openai_model",
  CHAT_LLM_GEMINI_MODEL: "aoitalk_chat_llm_gemini_model",
  CHAT_LLM_OPENAI_COMPATIBLE_API_KEY:
    "aoitalk_chat_llm_openai_compatible_api_key",
  CHAT_LLM_OPENAI_COMPATIBLE_MODEL:
    "aoitalk_chat_llm_openai_compatible_model",
  CHAT_LLM_OPENAI_COMPATIBLE_BASE_URL:
    "aoitalk_chat_llm_openai_compatible_base_url",
  NETWORK_ENDPOINT_ROUTING: "aoitalk_network_endpoint_routing",
  GATEWAY_AUTH: "aoitalk_gateway_auth_v1",
} as const;

export const API_TIMEOUT = 10000;
// Deletion includes route selection and signed Gateway identity checks.
// Keep reads/offline detection fast and bound this explicit mutation once.
export const FILES_DELETE_TIMEOUT_MS = 30_000;
export const CHAT_TIMEOUT = 30000;
// プロバイダーAPIからのモデル一覧取得タイムアウト。
export const MODEL_LIST_TIMEOUT = 8000;
