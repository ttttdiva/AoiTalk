export type ChatCommandCapability =
  | "web_search"
  | "image_generation"
  | "docs_ingest"
  | "work_intake"
  | "project_db_update"
  | "project_progress_review"
  | "task_update"
  | "wbs_sync";

export type ChatCommandToggleTarget = "project_context" | "deep_research";

export type ChatCommandDefinition =
  | {
      command: string;
      label: string;
      description: string;
      kind: "capability";
      capability: ChatCommandCapability;
    }
  | {
      command: string;
      label: string;
      description: string;
      kind: "toggle";
      target: ChatCommandToggleTarget;
    };

export type ActiveChatCommand = Extract<
  ChatCommandDefinition,
  { kind: "capability" }
>;

const VALID_CHAT_COMMAND_CAPABILITIES = new Set<string>([
  "web_search",
  "image_generation",
  "docs_ingest",
  "work_intake",
  "project_db_update",
  "project_progress_review",
  "task_update",
  "wbs_sync",
]);

export const CHAT_COMMANDS: ChatCommandDefinition[] = [
  {
    command: "/inbox",
    label: "Work Inbox",
    description: "メールやテキストを整理し、メールは保存して必要なものだけタスク化する",
    kind: "capability",
    capability: "work_intake",
  },
  {
    command: "/clip",
    label: "Docs取り込み",
    description: "貼り付けた情報を調査し、既存Docsへ整理して統合する",
    kind: "capability",
    capability: "docs_ingest",
  },
  {
    command: "/search",
    label: "Web検索",
    description: "次の送信でWeb検索を必ず使う",
    kind: "capability",
    capability: "web_search",
  },
  {
    command: "/image",
    label: "Image",
    description: "次の送信を画像生成として扱う",
    kind: "capability",
    capability: "image_generation",
  },
  {
    command: "/db",
    label: "Project Docs",
    description: "次の送信で案件情報Docs更新を必ず扱う",
    kind: "capability",
    capability: "project_db_update",
  },
  {
    command: "/progress",
    label: "Progress",
    description: "次の送信で案件進捗を根拠確認しながら調査する",
    kind: "capability",
    capability: "project_progress_review",
  },
  {
    command: "/tasks",
    label: "Tasks",
    description: "次の送信でタスク更新・整理を必ず扱う",
    kind: "capability",
    capability: "task_update",
  },
  {
    command: "/wbs",
    label: "WBS",
    description: "次の送信でWBS同期・確認を必ず扱う",
    kind: "capability",
    capability: "wbs_sync",
  },
  {
    command: "/project",
    label: "Project",
    description: "Project contextを切り替える",
    kind: "toggle",
    target: "project_context",
  },
  {
    command: "/research",
    label: "Research",
    description: "Deep Researchを切り替える",
    kind: "toggle",
    target: "deep_research",
  },
];

export const HIDDEN_CHAT_SKILL_NAMES = new Set([
  "weather_check",
  "weekly_report",
]);

export function findChatCommand(command: string): ChatCommandDefinition | null {
  const normalized = command.trim().toLowerCase();
  return (
    CHAT_COMMANDS.find((item) => item.command.toLowerCase() === normalized) ??
    null
  );
}

export function isSlashCommandToken(value: string): boolean {
  const trimmed = value.trim();
  return /^\/[^\s/]+$/.test(trimmed);
}

export function filterChatCommands(query: string): ChatCommandDefinition[] {
  const normalized = query.trim().toLowerCase();
  if (!normalized || normalized === "/") return CHAT_COMMANDS;
  return CHAT_COMMANDS.filter((item) =>
    item.command.toLowerCase().startsWith(normalized),
  );
}

export function firstMatchingChatCommand(
  query: string,
): ChatCommandDefinition | null {
  return filterChatCommands(query)[0] ?? null;
}

export function completeChatCommandPrefix(query: string): string | null {
  const normalized = query.trim().toLowerCase();
  if (!normalized || normalized === "/") return null;
  const match = firstMatchingChatCommand(normalized);
  if (!match || match.command.toLowerCase() === normalized) return null;
  return match.command;
}

export function commandCapabilitiesForActiveCommand(
  command: ActiveChatCommand | null,
): ChatCommandCapability[] {
  return command ? [command.capability] : [];
}

export type ChatCommandSubmission = {
  content: string;
  capabilities: ChatCommandCapability[];
  error: string | null;
};

/** Resolve a composer submission without treating source material as commands. */
export function resolveChatCommandSubmission(
  value: string,
  activeCommand: ActiveChatCommand | null,
  hasAttachments = false,
): ChatCommandSubmission {
  const lines = String(value ?? "").split(/\r?\n/);
  const inlineClip = lines.length > 0 && lines[0].trim().toLowerCase() === "/clip";
  const inlineInbox = lines.length > 0 && lines[0].trim().toLowerCase() === "/inbox";
  const activeCapabilities = commandCapabilitiesForActiveCommand(activeCommand);
  const inlineCapabilities: ChatCommandCapability[] = inlineClip
    ? ["docs_ingest"]
    : inlineInbox
      ? ["work_intake"]
      : [];
  const capabilities = sanitizeChatCommandCapabilities([
    ...activeCapabilities,
    ...inlineCapabilities,
  ]);
  const content = (inlineClip || inlineInbox ? lines.slice(1).join("\n") : value).trim();

  if (
    (inlineClip || inlineInbox) &&
    activeCommand &&
    activeCommand.capability !== inlineCapabilities[0]
  ) {
    return {
      content,
      capabilities,
      error: "複数の組み込みコマンドを同時には実行できません",
    };
  }
  if (capabilities.includes("docs_ingest") && !content) {
    return {
      content,
      capabilities,
      error: "取り込む情報を入力してください",
    };
  }
  if (capabilities.includes("work_intake") && !content && !hasAttachments) {
    return {
      content,
      capabilities,
      error: "処理するテキストまたはメールを入力してください",
    };
  }
  return { content, capabilities, error: null };
}

export function sanitizeChatCommandCapabilities(
  value: unknown,
): ChatCommandCapability[] {
  if (!Array.isArray(value)) return [];
  const result: ChatCommandCapability[] = [];
  const seen = new Set<string>();
  for (const raw of value) {
    const capability = typeof raw === "string" ? raw.trim().toLowerCase() : "";
    if (
      !VALID_CHAT_COMMAND_CAPABILITIES.has(capability) ||
      seen.has(capability)
    ) {
      continue;
    }
    seen.add(capability);
    result.push(capability as ChatCommandCapability);
  }
  return result;
}

export function commandCapabilitiesFromMessageMetadata(
  metadata: { command_capabilities?: unknown } | null | undefined,
): ChatCommandCapability[] {
  return sanitizeChatCommandCapabilities(metadata?.command_capabilities);
}
