import { Platform } from "react-native";

/**
 * Mobile app configuration.
 */

const LOCALHOST_API_URL = "http://127.0.0.1:3000";
const ANDROID_EMULATOR_API_URL = "http://10.0.2.2:3000";

export const DEFAULT_API_URL =
  Platform.OS === "android" ? ANDROID_EMULATOR_API_URL : LOCALHOST_API_URL;

export const EXTERNAL_API_URL = "https://nk-h01.f5.si:6002";

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
} as const;

export const API_TIMEOUT = 10000;
export const CHAT_TIMEOUT = 30000;
// プロバイダーAPIからのモデル一覧取得タイムアウト。
export const MODEL_LIST_TIMEOUT = 8000;
