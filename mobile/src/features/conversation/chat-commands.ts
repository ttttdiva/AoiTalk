export type ChatCommandCapability =
  | "web_search"
  | "image_generation"
  | "docs_ingest"
  | "work_intake"
  | "project_db_update"
  | "project_progress_review"
  | "task_update"
  | "wbs_sync";

export type MobileChatCommand = {
  command: string;
  label: string;
  description: string;
  capability: ChatCommandCapability;
};

export type SkillSlashCommand = {
  command: string;
  description: string;
  usage: string;
};

export const MOBILE_CHAT_COMMANDS: MobileChatCommand[] = [
  { command: "/inbox", label: "Work Inbox", description: "メールやテキストを整理し、メールは保存して必要なものだけタスク化する", capability: "work_intake" },
  { command: "/clip", label: "Docs取り込み", description: "貼り付けた情報を既存Docsへ整理して統合する", capability: "docs_ingest" },
  { command: "/search", label: "Web検索", description: "次の送信でWeb検索を必ず使う", capability: "web_search" },
  { command: "/image", label: "画像生成", description: "次の送信を画像生成として扱う", capability: "image_generation" },
  { command: "/db", label: "Project Docs", description: "案件情報Docsの更新を扱う", capability: "project_db_update" },
  { command: "/progress", label: "Progress", description: "案件進捗を根拠付きで確認する", capability: "project_progress_review" },
  { command: "/tasks", label: "Tasks", description: "タスクの更新・整理を扱う", capability: "task_update" },
  { command: "/wbs", label: "WBS", description: "WBSの同期・確認を扱う", capability: "wbs_sync" },
];

const HIDDEN_SKILLS = new Set(["weather_check", "weekly_report"]);
const VALID_CAPABILITIES = new Set<ChatCommandCapability>(
  MOBILE_CHAT_COMMANDS.map((command) => command.capability),
);

export function sanitizeChatCommandCapabilities(value: unknown): ChatCommandCapability[] {
  if (!Array.isArray(value)) return [];
  return [...new Set(value)].filter(
    (item): item is ChatCommandCapability =>
      typeof item === "string" && VALID_CAPABILITIES.has(item as ChatCommandCapability),
  );
}

export function skillSlashCommands(
  skills: Array<{ name: string; description?: string; trigger_mode?: string }>,
): SkillSlashCommand[] {
  return skills
    .filter((skill) => skill.trigger_mode !== "auto")
    .filter((skill) => !HIDDEN_SKILLS.has(skill.name))
    .map((skill) => ({
      command: `/${skill.name}`,
      description: skill.description || "スキル",
      usage: `/${skill.name} [入力]`,
    }));
}

export function filterSlashCommands<T extends { command: string }>(
  commands: T[],
  query: string,
): T[] {
  const normalized = query.trim().toLowerCase();
  if (!normalized || normalized === "/") return commands;
  return commands.filter((item) => item.command.toLowerCase().startsWith(normalized));
}

export function resolveMobileCommandSubmission(
  value: string,
  activeCommand: MobileChatCommand | null,
): { content: string; capabilities: ChatCommandCapability[]; error: string | null } {
  const raw = String(value ?? "").trimStart();
  const separatorIndex = raw.search(/\s/);
  const token = (separatorIndex < 0 ? raw : raw.slice(0, separatorIndex)).toLowerCase();
  const inline = MOBILE_CHAT_COMMANDS.find((item) => item.command === token) ?? null;
  const selected = activeCommand ?? inline;
  const content = (inline && separatorIndex >= 0 ? raw.slice(separatorIndex) : inline ? "" : value).trim();
  const capabilities = selected ? [selected.capability] : [];

  if (inline && activeCommand && inline.capability !== activeCommand.capability) {
    return { content, capabilities, error: "複数の組み込みコマンドを同時には実行できません" };
  }
  if (selected?.capability === "docs_ingest" && !content) {
    return { content, capabilities, error: "取り込む情報を入力してください" };
  }
  if (selected?.capability === "work_intake" && !content) {
    return { content, capabilities, error: "処理するテキストを入力してください" };
  }
  return { content, capabilities, error: null };
}
