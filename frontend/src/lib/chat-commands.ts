export type ChatCommandCapability =
  | "web_search"
  | "image_generation"
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
  "project_db_update",
  "project_progress_review",
  "task_update",
  "wbs_sync",
]);

export const CHAT_COMMANDS: ChatCommandDefinition[] = [
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
    label: "Project DB",
    description: "次の送信で案件情報DB更新を必ず扱う",
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
