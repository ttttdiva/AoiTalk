export type ChatCommandCapability =
  | "web_search"
  | "image_generation"
  | "work_intake"
  | "project_db_update"
  | "project_progress_review"
  | "task_update"
  | "wbs_sync";

export type MobileChatCommand = {
  command: string;
  label: string;
  description: string;
  /**
   * Capability-bearing commands are dispatched with an explicit server
   * capability.  A server-only slash command can intentionally omit this
   * field so the literal command text remains the authoritative contract.
   */
  capability?: ChatCommandCapability;
  /** Keep the literal slash command in the submitted message. */
  serverOnly?: boolean;
};

export type SkillSlashCommand = {
  command: string;
  description: string;
  usage: string;
};

export const MOBILE_CHAT_COMMANDS: MobileChatCommand[] = [
  { command: "/inbox", label: "Work Inbox", description: "メールやテキストを整理し、メールは保存して必要なものだけタスク化する", capability: "work_intake" },
  { command: "/search", label: "Web検索", description: "次の送信でWeb検索を必ず使う", capability: "web_search" },
  { command: "/image", label: "画像生成", description: "次の送信を画像生成として扱う", capability: "image_generation" },
  {
    command: "/masking",
    label: "Masking / マスキング",
    description: "入力したテキストや添付ファイルの機密情報をマスキングする",
    serverOnly: true,
  },
  { command: "/document", label: "Document", description: "添付したXLSX手順書を作成・更新する", serverOnly: true },
  { command: "/template", label: "Template", description: "添付資料からXLSXテンプレートを作成する", serverOnly: true },
  { command: "/app", label: "App", description: "入力資料からAoiTalk Appを設計・実装・検証する", serverOnly: true },
  { command: "/macro", label: "Macro", description: "config/logを判定するApp・マクロを作成する", serverOnly: true },
  { command: "/db", label: "Project Docs", description: "案件情報Docsの更新を扱う", capability: "project_db_update" },
  { command: "/progress", label: "Progress", description: "案件進捗を根拠付きで確認する", capability: "project_progress_review" },
  { command: "/tasks", label: "Tasks", description: "タスクの更新・整理を扱う", capability: "task_update" },
  { command: "/wbs", label: "WBS", description: "WBSの同期・確認を扱う", capability: "wbs_sync" },
];

const HIDDEN_SKILLS = new Set(["weather_check", "weekly_report", "masking", "document", "template", "app", "macro"]);
const VALID_CAPABILITIES = new Set<ChatCommandCapability>(
  MOBILE_CHAT_COMMANDS.flatMap((command) =>
    command.capability ? [command.capability] : [],
  ),
);

const BUILT_IN_COMMANDS = new Set(
  MOBILE_CHAT_COMMANDS.map((command) => command.command.toLowerCase()),
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
  const seen = new Set<string>();
  return skills
    .filter((skill) => skill.trigger_mode !== "auto")
    .filter((skill) =>
      !HIDDEN_SKILLS.has(
        skill.name.trim().replace(/^\/+/, "").toLowerCase(),
      ),
    )
    .map((skill) => ({
      command: `/${skill.name.trim().replace(/^\/+/, "")}`,
      description: skill.description || "スキル",
      usage: `/${skill.name.trim().replace(/^\/+/, "")} [入力]`,
    }))
    .filter((skill) => {
      const command = skill.command.trim().toLowerCase();
      // A canonical built-in owns its slash token.  Keep dynamic Skills out
      // of both discovery surfaces so /masking cannot appear twice.
      if (BUILT_IN_COMMANDS.has(command) || seen.has(command)) return false;
      seen.add(command);
      return true;
    });
}

/**
 * Keep the composer defensive when a caller supplies an un-normalized Skill
 * list (for example, a stale cache).  The canonical built-in command wins and
 * duplicate dynamic slash tokens are collapsed case-insensitively.
 */
export function visibleSkillSlashCommands(
  skills: SkillSlashCommand[],
): SkillSlashCommand[] {
  const seen = new Set<string>();
  return skills.filter((skill) => {
    const command = skill.command.trim().toLowerCase();
    if (BUILT_IN_COMMANDS.has(command) || seen.has(command)) return false;
    seen.add(command);
    return true;
  });
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
  // A directly typed server-only command owns the submission even when an
  // older capability chip is still selected.  Do not attach that unrelated
  // capability to the raw masking payload.
  const selected = inline?.serverOnly ? inline : (activeCommand ?? inline);
  const isServerOnly = Boolean(inline?.serverOnly || activeCommand?.serverOnly);
  const content = isServerOnly
    ? (inline?.serverOnly
        ? raw
        : `${activeCommand?.command ?? ""} ${String(value ?? "")}`).trim()
    : (inline && separatorIndex >= 0
        ? raw.slice(separatorIndex)
        : inline
          ? ""
          : value).trim();
  const capabilities = selected?.capability ? [selected.capability] : [];

  if (
    inline &&
    activeCommand &&
    !inline.serverOnly &&
    (inline.capability !== activeCommand.capability ||
      Boolean(inline.serverOnly) !== Boolean(activeCommand.serverOnly))
  ) {
    return { content, capabilities, error: "複数の組み込みコマンドを同時には実行できません" };
  }
  if (selected?.capability === "work_intake" && !content) {
    return { content, capabilities, error: "処理するテキストを入力してください" };
  }
  return { content, capabilities, error: null };
}
