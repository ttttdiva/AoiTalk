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
  DEFAULT_CHARACTER_NAME: "aoitalk_default_character_name",
  CHAT_LLM_PROVIDER: "aoitalk_chat_llm_provider",
  CHAT_LLM_FALLBACK_PROVIDER: "aoitalk_chat_llm_fallback_provider",
  CHAT_LLM_API_KEY: "aoitalk_chat_llm_api_key",
  CHAT_LLM_MODEL: "aoitalk_chat_llm_model",
  CHAT_LLM_BASE_URL: "aoitalk_chat_llm_base_url",
  CHAT_LLM_OPENAI_API_KEY: "aoitalk_chat_llm_openai_api_key",
  CHAT_LLM_OPENAI_MODEL: "aoitalk_chat_llm_openai_model",
  CHAT_LLM_OPENAI_BASE_URL: "aoitalk_chat_llm_openai_base_url",
  CHAT_LLM_GEMINI_API_KEY: "aoitalk_chat_llm_gemini_api_key",
  CHAT_LLM_GEMINI_MODEL: "aoitalk_chat_llm_gemini_model",
  CHAT_LLM_GEMINI_BASE_URL: "aoitalk_chat_llm_gemini_base_url",
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
