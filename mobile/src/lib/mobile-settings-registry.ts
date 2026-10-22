export type MobileSettingsCategoryId =
  | "personal"
  | "ai"
  | "connections"
  | "data";

export type MobileSettingsVisibilityContext = {
  isAuthenticated: boolean;
  isAnonymous: boolean;
  isAdmin: boolean;
};

export type MobileSettingsItem = {
  id: string;
  category: MobileSettingsCategoryId;
  label: string;
  icon: string;
  route: string;
  keywords: readonly string[];
  visible?: (context: MobileSettingsVisibilityContext) => boolean;
};

export const MOBILE_SETTINGS_CATEGORIES: readonly {
  id: MobileSettingsCategoryId;
  label: string;
}[] = [
  { id: "personal", label: "個人設定" },
  { id: "ai", label: "AI・会話" },
  { id: "connections", label: "通知・連携" },
  { id: "data", label: "データ・アプリ" },
] as const;

export const MOBILE_SETTINGS_ITEMS: readonly MobileSettingsItem[] = [
  { id: "profile", category: "personal", label: "プロフィール", icon: "account-circle-outline", route: "/(tabs)/settings/profile", keywords: ["アカウント","ログイン","ログアウト","ユーザー","profile","account"] },
  { id: "audio", category: "personal", label: "音声・再生", icon: "music-note-outline", route: "/(tabs)/settings/audio", keywords: ["音声","再生","audio","player","shuffle","repeat"] },
  { id: "appearance", category: "personal", label: "表示・外観", icon: "palette-outline", route: "/(tabs)/settings/appearance", keywords: ["表示","外観","テーマ","チャット","背景","アニメーション","波紋","appearance","background","animation"] },
  { id: "ai-model", category: "ai", label: "AIモデル", icon: "brain", route: "/(tabs)/settings/ai-model", keywords: ["モデル","LLM","API","provider","reasoning","effort","fallback","フォールバック","クリップ"] },
  { id: "custom-instructions", category: "ai", label: "会話カスタム指示", icon: "message-text-outline", route: "/(tabs)/settings/custom-instructions", keywords: ["カスタム指示","指示","prompt","instruction","会話"], visible: ({ isAuthenticated }) => isAuthenticated },
  { id: "character", category: "ai", label: "キャラクター", icon: "account-star-outline", route: "/(tabs)/settings/character", keywords: ["キャラクター","character","persona"] },
  { id: "memory", category: "ai", label: "ユーザーメモリ", icon: "database-outline", route: "/(tabs)/settings/memory", keywords: ["メモリ","memory","ユーザー情報"] },
  { id: "notifications", category: "connections", label: "通知・カレンダー", icon: "bell-outline", route: "/(tabs)/settings/notifications", keywords: ["通知","カレンダー","calendar","Google Calendar","予定"] },
  { id: "connection", category: "connections", label: "サーバー / ネットワーク", icon: "lan-connect", route: "/(tabs)/settings/connection", keywords: ["サーバー","ネットワーク","API URL","接続","server","network"] },
  { id: "mcp", category: "connections", label: "MCP / エージェント", icon: "robot-outline", route: "/(tabs)/settings/mcp", keywords: ["MCP","エージェント","agent","tool","ツール"] },
  { id: "remote-servers", category: "connections", label: "外部サーバー接続", icon: "server-network", route: "/(tabs)/settings/remote-servers", keywords: ["外部サーバー","remote","server","接続"] },
  { id: "docs-resync", category: "data", label: "Docs再同期", icon: "database-sync-outline", route: "/(tabs)/settings/docs-resync", keywords: ["Docs","同期","再同期","resync","データ"], visible: ({ isAuthenticated }) => isAuthenticated },
  { id: "apps", category: "data", label: "Apps", icon: "application-brackets-outline", route: "/(tabs)/apps", keywords: ["Apps","アプリ","workspace","ワークスペース"] },
  { id: "about", category: "data", label: "アプリ情報", icon: "information-outline", route: "/(tabs)/settings/about", keywords: ["バージョン","version","診断","about","アプリ情報"] },
] as const;

export function filterMobileSettingsItems(query: string, context: MobileSettingsVisibilityContext) {
  const normalized = query.trim().toLocaleLowerCase("ja-JP");
  return MOBILE_SETTINGS_ITEMS.filter((item) => {
    if (item.visible && !item.visible(context)) return false;
    if (!normalized) return true;
    return [item.label, ...item.keywords].join(" ").toLocaleLowerCase("ja-JP").includes(normalized);
  });
}
