import type { ComponentType } from "react";
import {
  AudioLines,
  ArrowLeftRight,
  Bell,
  Bug,
  CalendarDays,
  CircleHelp,
  Database,
  Eye,
  FileKey2,
  KeyRound,
  Keyboard,
  MessageSquareText,
  MonitorCog,
  Plug,
  Search,
  ShieldCheck,
  Smartphone,
  UserCog,
  Waves,
  Workflow,
} from "lucide-react";

/**
 * Stable anchors used by the settings workspace.  Keep these ids independent
 * from component labels so links can be shared by the sidebar and search
 * without coupling navigation to implementation details.
 */
export type SettingsCategoryId =
  | "conversation"
  | "tts-yomi"
  | "appearance"
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
    id: "appearance",
    label: "表示・外観",
    description: "チャット背景、ペット、エディタ表示、プレイヤー",
    icon: MonitorCog,
  },
  {
    id: "input",
    label: "入力・操作",
    description: "ショートカット、スニペット、端末コマンド",
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
  | "pet"
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
  | "editor-settings"
  | "file-transfer-compatibility"
  | "chat-background-animation"
  | "audio-player"
  | "search-provider"
  | "browser-agent"
  | "pc-bridge"
  | "clip-ingest"
  | "mcp"
  | "remote-server"
  | "spotify"
  | "comfyui"
  | "cookie-management"
  | "password"
  | "login-history"
  | "cost-dashboard"
  | "user-management"
  | "user-export"
  | "skills"
  | "heartbeats"
  | "verification-data"
  | "command-workspace-unconfined"
  | "feedback";

export type SettingsTargetDefinition = {
  id: SettingsTargetId;
  /** Category anchor to select when this target is opened. */
  category: SettingsCategoryId;
  /** DOM/hash anchor. Explicit; aliases may intentionally share one physical target. */
  targetId: string;
  label: string;
  icon: ComponentType<{ className?: string }>;
  adminOnly?: boolean;
  /** Runtime capability required before this target can be opened. */
  capability?: "entertainment";
  /** Additional words used by the settings search without changing the UI label. */
  keywords?: readonly string[];
  /** Category-like targets should scroll without opening an arbitrary child. */
  openDisclosure?: boolean;
};

export const SETTINGS_TARGET_REGISTRY: readonly SettingsTargetDefinition[] = [
  { id: "task-notifications", targetId: "task-notifications", category: "notifications", label: "タスク通知", icon: Bell, keywords: ["通知", "予定"] },
  { id: "restart-shortcut", targetId: "restart-shortcut", category: "input", label: "ショートカット", icon: Keyboard, adminOnly: true },
  { id: "web-search", targetId: "search-provider", category: "knowledge", label: "検索プロバイダ", icon: Search, keywords: ["検索", "Web検索"] },
  { id: "user-memory", targetId: "memory", category: "conversation", label: "メモリ", icon: Database, keywords: ["記憶"] },
  { id: "google-calendar", targetId: "google-calendar", category: "notifications", label: "Google Calendar", icon: CalendarDays, keywords: ["カレンダー", "予定"] },
  { id: "webex", targetId: "webex", category: "integrations", label: "Webex", icon: MessageSquareText },
  { id: "snippets", targetId: "snippets", category: "input", label: "スニペット", icon: Keyboard, keywords: ["定型文", "ショートカット"] },
  { id: "integrations", targetId: "integrations", category: "integrations", label: "外部連携", icon: Plug, openDisclosure: false },
  { id: "custom-instructions", targetId: "custom-instructions", category: "conversation", label: "会話カスタム指示", icon: MessageSquareText, keywords: ["指示", "プロンプト"] },
  { id: "characters", targetId: "characters", category: "conversation", label: "キャラクター", icon: MessageSquareText },
  { id: "pet", targetId: "pet", category: "appearance", label: "ペット", icon: Eye, keywords: ["ペット表示", "マスコット"], capability: "entertainment" },
  { id: "llm-model", targetId: "llm-model", category: "conversation", label: "言語モデル", icon: MessageSquareText, keywords: ["LLM", "モデル"] },
  { id: "autonomous-task-execution", targetId: "autonomous-task-execution", category: "tool-permissions", label: "自律タスク実行", icon: Workflow, keywords: ["自動実行"] },
  { id: "embed-card", targetId: "editor-settings", category: "appearance", label: "埋め込みカード", icon: Keyboard, keywords: ["表示", "カード"] },
  { id: "mobile-commands", targetId: "mobile-commands", category: "input", label: "モバイルコマンド", icon: Smartphone, adminOnly: true, keywords: ["端末", "コマンド"] },
  { id: "knowledge-sources", targetId: "knowledge-sources", category: "knowledge", label: "ナレッジソース", icon: Database, keywords: ["知識", "ソース"] },
  { id: "crawler-status", targetId: "crawler-status", category: "knowledge", label: "クローラーステータス", icon: Bug, keywords: ["クローラー"] },
  { id: "tool-permissions", targetId: "tool-permissions", category: "tool-permissions", label: "ツール権限", icon: ShieldCheck, keywords: ["権限"] },
  { id: "account-security", targetId: "password", category: "account", label: "アカウント", icon: KeyRound, keywords: ["個人設定", "セキュリティ"] },
  { id: "memory", targetId: "memory", category: "conversation", label: "メモリ", icon: Database },
  { id: "yomi-linter", targetId: "yomi-linter", category: "tts-yomi", label: "読み検出", icon: AudioLines },
  { id: "yomi-dictionary", targetId: "yomi-dictionary", category: "tts-yomi", label: "読み辞書", icon: AudioLines },
  { id: "yomi-candidates", targetId: "yomi-candidates", category: "tts-yomi", label: "読み候補", icon: AudioLines },
  { id: "editor-settings", targetId: "editor-settings", category: "appearance", label: "エディタ", icon: Keyboard, keywords: ["表示", "編集"] },
  {
    id: "file-transfer-compatibility",
    targetId: "file-transfer-compatibility",
    category: "input",
    label: "転送互換モード",
    icon: ArrowLeftRight,
    keywords: ["ファイル転送", "アップロード", "ダウンロード", "保存", "互換", "通信方式"],
  },
  {
    id: "chat-background-animation",
    targetId: "chat-background-animation",
    category: "appearance",
    label: "チャット背景アニメーション",
    icon: Waves,
    keywords: ["チャット", "背景", "アニメーション", "波紋"],
  },
  { id: "audio-player", targetId: "audio-player", category: "appearance", label: "音楽プレイヤー", icon: Keyboard, keywords: ["音楽", "再生", "プレイヤー"] },
  { id: "search-provider", targetId: "search-provider", category: "knowledge", label: "検索プロバイダ", icon: Search },
  { id: "pc-bridge", targetId: "pc-bridge", category: "knowledge", label: "PC接続", icon: Workflow },
  { id: "browser-agent", targetId: "browser-agent", category: "knowledge", label: "ブラウザ操作", icon: Workflow, adminOnly: true },
  { id: "clip-ingest", targetId: "clip-ingest", category: "knowledge", label: "クリップ取り込み", icon: Database },
  { id: "mcp", targetId: "mcp", category: "integrations", label: "MCP", icon: Plug },
  { id: "remote-server", targetId: "remote-server", category: "notifications", label: "外部AoiTalkサーバー", icon: Plug },
  { id: "spotify", targetId: "spotify", category: "integrations", label: "Spotify", icon: Plug },
  { id: "comfyui", targetId: "comfyui", category: "integrations", label: "ComfyUI", icon: Plug },
  { id: "cookie-management", targetId: "cookie-management", category: "integrations", label: "Cookie管理", icon: FileKey2 },
  { id: "password", targetId: "password", category: "account", label: "パスワード変更", icon: KeyRound },
  { id: "login-history", targetId: "login-history", category: "account", label: "ログイン履歴", icon: KeyRound, adminOnly: true },
  { id: "cost-dashboard", targetId: "cost-dashboard", category: "account", label: "利用状況", icon: KeyRound },
  { id: "user-management", targetId: "user-management", category: "admin", label: "ユーザー管理", icon: UserCog, adminOnly: true },
  { id: "user-export", targetId: "user-export", category: "admin", label: "ユーザーデータ", icon: UserCog, adminOnly: true },
  { id: "skills", targetId: "skills", category: "admin", label: "Skills", icon: UserCog, adminOnly: true },
  { id: "heartbeats", targetId: "heartbeats", category: "admin", label: "Heartbeats", icon: UserCog, adminOnly: true },
  { id: "verification-data", targetId: "verification-data", category: "admin", label: "Verification / Test Data", icon: UserCog, adminOnly: true },
  { id: "command-workspace-unconfined", targetId: "command-workspace-unconfined", category: "admin", label: "コマンド作業場所の制限解除", icon: ShieldCheck, adminOnly: true, keywords: ["workspace", "allowlist", "harness"] },
  { id: "feedback", targetId: "feedback", category: "support", label: "フィードバック", icon: CircleHelp, adminOnly: true },
] as const;

const CATEGORY_IDS = new Set<string>(SETTINGS_CATEGORIES.map((item) => item.id));
const TARGET_BY_PHYSICAL_ID = new Map<string, SettingsTargetDefinition>();
const TARGET_BY_DEFINITION_ID = new Map<string, SettingsTargetDefinition>();
for (const item of SETTINGS_TARGET_REGISTRY) {
  // Physical target ids are the stable DOM/hash contract.  Keep the first
  // definition whose id matches that target as canonical while still
  // accepting aliases for old deep links and persisted references.
  const current = TARGET_BY_PHYSICAL_ID.get(item.targetId);
  if (!current || item.id === item.targetId) {
    TARGET_BY_PHYSICAL_ID.set(item.targetId, item);
  }
  TARGET_BY_DEFINITION_ID.set(item.id, item);
}

export type SettingsTargetSearchDefinition = {
  targetId: string;
  category: SettingsCategoryId;
  label: string;
  aliases: readonly string[];
  keywords: readonly string[];
  icon: ComponentType<{ className?: string }>;
  adminOnly?: boolean;
  capability?: "entertainment";
  openDisclosure?: boolean;
};

export type SettingsTargetVisibilityOptions = {
  /** Targets listed here are hidden from navigation/search regardless of role. */
  excludeTargetIds?: readonly string[];
  /** When provided, capability-gated targets require a matching capability. */
  availableCapabilities?: ReadonlySet<string>;
};

export function getSettingsTarget(targetId: string) {
  return TARGET_BY_PHYSICAL_ID.get(targetId) ?? TARGET_BY_DEFINITION_ID.get(targetId);
}

export function isSettingsCategoryId(value: string): value is SettingsCategoryId {
  return CATEGORY_IDS.has(value);
}

export function getVisibleSettingsTargets(
  isAdmin: boolean,
  options: SettingsTargetVisibilityOptions = {},
) {
  const excluded = new Set(options.excludeTargetIds ?? []);
  return SETTINGS_TARGET_REGISTRY.filter(
    (item) =>
      (!item.adminOnly || isAdmin) &&
      !excluded.has(item.id) &&
      !excluded.has(item.targetId) &&
      (!item.capability ||
        !options.availableCapabilities ||
        options.availableCapabilities.has(item.capability)),
  );
}

/**
 * Return one searchable entry per physical DOM target.  Several historical
 * ids intentionally point at one disclosure (for example `web-search` and
 * `search-provider`), so search must not show duplicate rows.  Their labels
 * and keywords are merged to keep the old vocabulary searchable.
 */
export function getVisibleSettingsTargetSearchDefinitions(
  isAdmin: boolean,
  options: SettingsTargetVisibilityOptions = {},
): SettingsTargetSearchDefinition[] {
  const byTarget = new Map<string, SettingsTargetSearchDefinition>();
  for (const item of getVisibleSettingsTargets(isAdmin, options)) {
    const current = byTarget.get(item.targetId);
    if (!current) {
      byTarget.set(item.targetId, {
        targetId: item.targetId,
        category: item.category,
        label: item.label,
        aliases: [item.label],
        keywords: [...(item.keywords ?? []), item.id],
        icon: item.icon,
        adminOnly: item.adminOnly,
        capability: item.capability,
        openDisclosure: item.openDisclosure,
      });
      continue;
    }
    const aliases = current.aliases.includes(item.label)
      ? current.aliases
      : [...current.aliases, item.label];
    const keywords = new Set([...current.keywords, ...(item.keywords ?? []), item.id]);
    byTarget.set(item.targetId, { ...current, aliases, keywords: [...keywords] });
  }
  return [...byTarget.values()];
}
