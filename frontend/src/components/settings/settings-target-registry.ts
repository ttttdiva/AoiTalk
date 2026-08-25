import type { ComponentType } from "react";
import {
  AudioLines,
  Bell,
  BookOpen,
  Bug,
  CalendarDays,
  CircleHelp,
  Database,
  FileKey2,
  KeyRound,
  Keyboard,
  MessageSquareText,
  Plug,
  Search,
  ShieldCheck,
  Smartphone,
  UserCog,
  Workflow,
} from "lucide-react";

/**
 * Stable anchors used by the settings workspace.  Keep these ids independent
 * from component labels so links can be shared by the sidebar, overview and
 * quick-settings pins without coupling navigation to implementation details.
 */
export type SettingsCategoryId =
  | "overview"
  | "conversation"
  | "tts-yomi"
  | "input"
  | "notifications"
  | "knowledge"
  | "integrations"
  | "tool-permissions"
  | "account"
  | "admin"
  | "support";

export type SettingsCategory = {
  id: SettingsCategoryId;
  label: string;
  description: string;
  icon: ComponentType<{ className?: string }>;
  adminOnly?: boolean;
};

export const SETTINGS_CATEGORIES: readonly SettingsCategory[] = [
  {
    id: "overview",
    label: "概要",
    description: "設定の入口とよく使う項目",
    icon: BookOpen,
  },
  {
    id: "conversation",
    label: "会話・AI応答",
    description: "カスタム指示、メモリ、モデル配置",
    icon: MessageSquareText,
  },
  {
    id: "tts-yomi",
    label: "音声・読み",
    description: "読み上げと読み辞書",
    icon: AudioLines,
  },
  {
    id: "input",
    label: "入力・操作",
    description: "タブ、エディタ、端末コマンド",
    icon: Keyboard,
  },
  {
    id: "notifications",
    label: "通知・予定",
    description: "タスク通知、予定、リモート接続",
    icon: Bell,
  },
  {
    id: "knowledge",
    label: "ナレッジ・検索",
    description: "検索、取り込み、クローラー",
    icon: Search,
  },
  {
    id: "integrations",
    label: "外部連携",
    description: "接続サービスとMCP",
    icon: Plug,
  },
  {
    id: "tool-permissions",
    label: "権限・実行環境",
    description: "ツール権限と実行境界",
    icon: ShieldCheck,
  },
  {
    id: "account",
    label: "アカウント",
    description: "パスワード、履歴、利用状況",
    icon: KeyRound,
  },
  {
    id: "admin",
    label: "管理・運用",
    description: "ユーザー、エクスポート、Skills",
    icon: UserCog,
    adminOnly: true,
  },
  {
    id: "support",
    label: "サポート",
    description: "フィードバック",
    icon: CircleHelp,
    adminOnly: true,
  },
] as const;

export type SettingsTargetId =
  | "task-notifications"
  | "restart-shortcut"
  | "web-search"
  | "user-memory"
  | "google-calendar"
  | "webex"
  | "snippets"
  | "integrations"
  | "custom-instructions"
  | "characters"
  | "llm-model"
  | "autonomous-task-execution"
  | "embed-card"
  | "mobile-commands"
  | "knowledge-sources"
  | "crawler-status"
  | "tool-permissions"
  | "account-security"
  | "memory"
  | "yomi-linter"
  | "yomi-dictionary"
  | "yomi-candidates"
  | "navigation-tabs"
  | "editor-settings"
  | "audio-player"
  | "search-provider"
  | "clip-ingest"
  | "mcp"
  | "remote-server"
  | "spotify"
  | "hydrus"
  | "comfyui"
  | "cookie-management"
  | "password"
  | "login-history"
  | "cost-dashboard"
  | "user-management"
  | "user-export"
  | "skills"
  | "heartbeats"
  | "feedback";

export type SettingsTargetDefinition = {
  id: SettingsTargetId;
  /** Category anchor to select when this target is opened. */
  category: SettingsCategoryId;
  /** DOM/hash anchor. Explicit; aliases may intentionally share one physical target. */
  targetId: string;
  label: string;
  icon: ComponentType<{ className?: string }>;
  quick?: boolean;
  adminOnly?: boolean;
  /** Category-like targets should scroll without opening an arbitrary child. */
  openDisclosure?: boolean;
};

export const SETTINGS_TARGET_REGISTRY: readonly SettingsTargetDefinition[] = [
  { id: "task-notifications", targetId: "task-notifications", category: "notifications", label: "タスク通知", icon: Bell, quick: true },
  { id: "restart-shortcut", targetId: "restart-shortcut", category: "input", label: "ショートカット", icon: Keyboard },
  { id: "web-search", targetId: "search-provider", category: "knowledge", label: "検索プロバイダ", icon: Search, quick: true },
  { id: "user-memory", targetId: "memory", category: "conversation", label: "メモリ", icon: Database, quick: true },
  { id: "google-calendar", targetId: "google-calendar", category: "notifications", label: "Google Calendar", icon: CalendarDays, quick: true },
  { id: "webex", targetId: "webex", category: "integrations", label: "Webex", icon: MessageSquareText, quick: true },
  { id: "snippets", targetId: "snippets", category: "input", label: "スニペット", icon: Keyboard, quick: true },
  { id: "integrations", targetId: "integrations", category: "integrations", label: "外部連携", icon: Plug, quick: true, openDisclosure: false },
  { id: "custom-instructions", targetId: "custom-instructions", category: "conversation", label: "会話カスタム指示", icon: MessageSquareText, quick: true },
  { id: "characters", targetId: "characters", category: "conversation", label: "キャラクター", icon: MessageSquareText, quick: true },
  { id: "llm-model", targetId: "llm-model", category: "conversation", label: "言語モデル", icon: MessageSquareText, quick: true },
  { id: "autonomous-task-execution", targetId: "autonomous-task-execution", category: "tool-permissions", label: "自律タスク実行", icon: Workflow, quick: true },
  // The persisted quick id is kept for compatibility, but the physical
  // target is the editor-settings panel (there is no standalone card).
  { id: "embed-card", targetId: "editor-settings", category: "input", label: "埋め込みカード", icon: Keyboard, quick: true },
  { id: "mobile-commands", targetId: "mobile-commands", category: "input", label: "モバイルコマンド", icon: Smartphone, quick: true, adminOnly: true },
  { id: "knowledge-sources", targetId: "knowledge-sources", category: "knowledge", label: "ナレッジソース", icon: Database, quick: true },
  { id: "crawler-status", targetId: "crawler-status", category: "knowledge", label: "クローラーステータス", icon: Bug, quick: true },
  { id: "tool-permissions", targetId: "tool-permissions", category: "tool-permissions", label: "ツール権限", icon: ShieldCheck, quick: true },
  { id: "account-security", targetId: "password", category: "account", label: "アカウント", icon: KeyRound, quick: true },
  { id: "memory", targetId: "memory", category: "conversation", label: "メモリ", icon: Database },
  { id: "yomi-linter", targetId: "yomi-linter", category: "tts-yomi", label: "読み検出", icon: AudioLines },
  { id: "yomi-dictionary", targetId: "yomi-dictionary", category: "tts-yomi", label: "読み辞書", icon: AudioLines },
  { id: "yomi-candidates", targetId: "yomi-candidates", category: "tts-yomi", label: "読み候補", icon: AudioLines },
  { id: "navigation-tabs", targetId: "navigation-tabs", category: "input", label: "ナビゲーションタブ", icon: Keyboard },
  { id: "editor-settings", targetId: "editor-settings", category: "input", label: "エディタ", icon: Keyboard },
  { id: "audio-player", targetId: "audio-player", category: "input", label: "音楽プレイヤー", icon: Keyboard },
  { id: "search-provider", targetId: "search-provider", category: "knowledge", label: "検索プロバイダ", icon: Search },
  { id: "clip-ingest", targetId: "clip-ingest", category: "knowledge", label: "クリップ取り込み", icon: Database },
  { id: "mcp", targetId: "mcp", category: "integrations", label: "MCP", icon: Plug },
  { id: "remote-server", targetId: "remote-server", category: "notifications", label: "外部AoiTalkサーバー", icon: Plug },
  { id: "spotify", targetId: "spotify", category: "integrations", label: "Spotify", icon: Plug },
  { id: "hydrus", targetId: "hydrus", category: "integrations", label: "Hydrus", icon: Plug },
  { id: "comfyui", targetId: "comfyui", category: "integrations", label: "ComfyUI", icon: Plug },
  { id: "cookie-management", targetId: "cookie-management", category: "integrations", label: "Cookie管理", icon: FileKey2 },
  { id: "password", targetId: "password", category: "account", label: "パスワード変更", icon: KeyRound },
  { id: "login-history", targetId: "login-history", category: "account", label: "ログイン履歴", icon: KeyRound, adminOnly: true },
  { id: "cost-dashboard", targetId: "cost-dashboard", category: "account", label: "利用状況", icon: KeyRound },
  { id: "user-management", targetId: "user-management", category: "admin", label: "ユーザー管理", icon: UserCog, adminOnly: true },
  { id: "user-export", targetId: "user-export", category: "admin", label: "ユーザーデータ", icon: UserCog, adminOnly: true },
  { id: "skills", targetId: "skills", category: "admin", label: "Skills", icon: UserCog, adminOnly: true },
  { id: "heartbeats", targetId: "heartbeats", category: "admin", label: "Heartbeats", icon: UserCog, adminOnly: true },
  { id: "feedback", targetId: "feedback", category: "support", label: "フィードバック", icon: CircleHelp, adminOnly: true },
] as const;

export const QUICK_SETTING_REGISTRY = SETTINGS_TARGET_REGISTRY.filter(
  (item) => item.quick,
);

const CATEGORY_IDS = new Set<string>(SETTINGS_CATEGORIES.map((item) => item.id));
const TARGET_BY_ID = new Map<string, SettingsTargetDefinition>(
  SETTINGS_TARGET_REGISTRY.map((item) => [item.targetId, item]),
);

export function getSettingsTarget(targetId: string) {
  return TARGET_BY_ID.get(targetId);
}

export function isSettingsCategoryId(value: string): value is SettingsCategoryId {
  return CATEGORY_IDS.has(value);
}

export function getVisibleSettingsTargets(isAdmin: boolean) {
  return SETTINGS_TARGET_REGISTRY.filter((item) => !item.adminOnly || isAdmin);
}
