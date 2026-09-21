export type ChatCommandCapability =
  | "aoitalk_help"
  | "web_search"
  | "image_generation"
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
      /**
       * A literal command is kept in the user message.  Unlike a capability
       * command it must not become an active LLM mode or be stripped by the
       * composer; the server-side command parser owns its execution.
       */
      kind: "literal";
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
  "aoitalk_help",
  "web_search",
  "image_generation",
  "work_intake",
  "project_db_update",
  "project_progress_review",
  "task_update",
  "wbs_sync",
]);

export const CHAT_COMMANDS: ChatCommandDefinition[] = [
  {
    command: "/help",
    label: "Help",
    description: "AoiTalkの使い方をガイドから確認する（スクリーンショット可）",
    kind: "capability",
    capability: "aoitalk_help",
  },
  {
    command: "/inbox",
    label: "Inbox",
    description: "問い合わせ・依頼・情報を1件のInbox項目として整理し、必要な対応だけタスク化する",
    kind: "capability",
    capability: "work_intake",
  },
  {
    command: "/masking",
    label: "Masking / マスキング",
    description: "入力したテキストや添付ファイルの機密情報をマスキングする",
    kind: "literal",
  },
  {
    command: "/document",
    label: "Document",
    description: "添付したXLSX手順書を新しい案件向けに作成・更新する",
    kind: "literal",
  },
  {
    command: "/template",
    label: "Template",
    description: "添付資料からXLSXテンプレートを作成する",
    kind: "literal",
  },
  {
    command: "/app",
    label: "App",
    description: "入力資料からAoiTalk Appを設計・実装・検証する",
    kind: "literal",
  },
  {
    command: "/macro",
    label: "Macro",
    description: "config/logを判定するApp・マクロを作成してテストする",
    kind: "literal",
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
  // `/help` is a trusted product command; a dynamic Skill with the same
  // name (or a legacy alias) must never shadow it in the slash menu.
  "help",
  "weather_check",
  "weekly_report",
  // The trusted built-in literal command is the sole visible masking entry.
  "masking",
  // System-owned workflow commands must not be shadowed by a user Skill with
  // the same name.  Their backend parser is the sole execution authority.
  "document",
  "template",
  "app",
  "macro",
]);

/**
 * Normalize a skill identifier before comparing it with hidden/built-in
 * command names.  Skill APIs normally return bare names, but accepting a
 * leading slash keeps duplicate filtering robust across older responses.
 */
export function normalizeChatSkillName(name: unknown): string {
  return String(name ?? "")
    .trim()
    .replace(/^\/+/, "")
    .toLowerCase();
}

export function isHiddenChatSkillName(name: unknown): boolean {
  return HIDDEN_CHAT_SKILL_NAMES.has(normalizeChatSkillName(name));
}

/**
 * Remove dynamic skill rows that would duplicate a built-in command and
 * collapse repeated rows from the skills endpoint.  The generic return type
 * lets the composer retain skill metadata while sharing the canonical
 * visibility rule with tests/other clients.
 */
export function filterVisibleChatSkillCommands<T extends { command: string }>(
  commands: readonly T[],
): T[] {
  const seen = new Set<string>();
  return commands.filter((item) => {
    const normalizedCommand = item.command.trim().toLowerCase();
    if (
      !normalizedCommand ||
      findChatCommand(normalizedCommand) !== null ||
      seen.has(normalizedCommand)
    ) {
      return false;
    }
    seen.add(normalizedCommand);
    return true;
  });
}

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

/** Return true when the first token is the reserved built-in `/help`. */
export function isChatHelpCommand(value: unknown): boolean {
  const trimmed = String(value ?? "").trim();
  return /^\/help(?:$|[\s\u3000])/i.test(trimmed);
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
  const inlineInbox = lines.length > 0 && lines[0].trim().toLowerCase() === "/inbox";
  const firstLineToken = lines[0].trim().split(/\s+/, 1)[0]?.toLowerCase();
  const inlineHelp = firstLineToken === "/help";
  const inlineMasking = firstLineToken === "/masking";
  // /masking is a server-owned literal operation.  If a user typed it while
  // an older capability chip was still active, do not attach that unrelated
  // capability to the raw masking invocation.
  const activeCapabilities = inlineMasking
    ? []
    : commandCapabilitiesForActiveCommand(activeCommand);
  const inlineCapabilities: ChatCommandCapability[] = inlineHelp
    ? ["aoitalk_help"]
    : inlineInbox
      ? ["work_intake"]
      : [];
  const capabilities = sanitizeChatCommandCapabilities([
    ...activeCapabilities,
    ...inlineCapabilities,
  ]);
  // Keep a directly typed `/help` prefix in the submitted content.  Menu
  // activation also materializes that exact server-owned token at transport
  // time, so an arbitrary client-provided capability cannot silently turn
  // ordinary prose into Help while direct and menu submissions converge.
  const menuHelp = activeCommand?.capability === "aoitalk_help" && !inlineHelp;
  const content = (inlineHelp
    ? value
    : inlineInbox
      ? lines.slice(1).join("\n")
      : menuHelp
        ? value.trim()
          ? `/help ${value.trim()}`
          : "/help"
        : value
  ).trim();

  if (
    (inlineInbox || inlineHelp) &&
    activeCommand &&
    activeCommand.capability !== inlineCapabilities[0]
  ) {
    return {
      content,
      capabilities,
      error: "複数の組み込みコマンドを同時には実行できません",
    };
  }
  if (capabilities.includes("work_intake") && !content && !hasAttachments) {
    return {
      content,
      capabilities,
      error: "処理するテキストまたは添付ファイルを入力してください",
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
  // Help is a turn-local, read-only product capability.  If stale draft or
  // client metadata contains another capability alongside it, never allow a
  // mixed turn to escape the trusted Help boundary.
  if (result.includes("aoitalk_help")) return ["aoitalk_help"];
  return result;
}

export function commandCapabilitiesFromMessageMetadata(
  metadata: { command_capabilities?: unknown } | null | undefined,
): ChatCommandCapability[] {
  return sanitizeChatCommandCapabilities(metadata?.command_capabilities);
}
