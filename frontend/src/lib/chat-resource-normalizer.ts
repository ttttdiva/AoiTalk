import type {
  AgentRun,
  AgentResourceMutation,
  ChatAttachmentMetadata,
  ConversationMessage,
  ConversationSession,
} from "@/lib/chat-api";

/** A resource explicitly associated with a conversation session. */
export type ChatResourceKind =
  | "attachment"
  | "task"
  | "docs"
  | "chat_session"
  | "file"
  | "project"
  | "app";

export type ChatResource = {
  key: string;
  kind: ChatResourceKind;
  id?: string;
  name: string;
  path?: string;
  href?: string;
  mimeType?: string;
  size?: number;
  projectId?: string | null;
  projectName?: string | null;
  operation?: AgentResourceMutation["operation"];
  source:
    | "attachment"
    | "mention"
    | "task_reference"
    | "agent_mutation"
    | "session_metadata";
};

export type RelatedTaskResourceInput = {
  id: string;
  title: string;
  status?: string | null;
  priority?: string | null;
  project_id?: string | null;
  project_name?: string | null;
  updated_at?: string | null;
};

type ChatResourceNormalizerInput = {
  session: ConversationSession | null;
  projectName?: string | null;
  messages?: ConversationMessage[];
  relatedTasks?: RelatedTaskResourceInput[];
  agentRun?: AgentRun | null;
};

const MENTION_TOKEN_RE =
  /@\[\[(file|task|project|app|docs|chat_session):([^:\]]+):([^\]]+)\]\]/gi;
const DOCS_WIKILINK_RE = /\[\[node:([0-9a-f-]{36})\|([^\]]+)\]\]/gi;

function cleanString(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const result = value.trim();
  return result ? result : null;
}

function canonicalPath(value: string): string {
  return value.replaceAll("\\", "/").replace(/\/+/g, "/").trim();
}

function canonicalKey(kind: ChatResourceKind, identity: string): string {
  const normalized =
    kind === "attachment" || kind === "file"
      ? canonicalPath(identity)
      : identity.trim();
  return `${kind}:${normalized}`;
}

function resourceHref(kind: ChatResourceKind, id: string): string | undefined {
  switch (kind) {
    case "task":
      return `/tasks/${encodeURIComponent(id)}`;
    case "docs":
      return `/docs/${encodeURIComponent(id)}`;
    case "chat_session":
      return `/chat?s=${encodeURIComponent(id)}`;
    case "file":
      return `/filer?open=${encodeURIComponent(id)}`;
    case "app":
      return `/apps/${encodeURIComponent(id)}`;
    default:
      return undefined;
  }
}

function addResource(
  target: Map<string, ChatResource>,
  resource: ChatResource,
): void {
  const previous = target.get(resource.key);
  if (!previous) {
    target.set(resource.key, resource);
    return;
  }
  // Keep the first explicit association but enrich it with metadata from a
  // later message/task response.  Never merge by title or other guessed data.
  target.set(resource.key, {
    ...previous,
    id: previous.id ?? resource.id,
    name: previous.name || resource.name,
    path: previous.path ?? resource.path,
    href: previous.href ?? resource.href,
    mimeType: previous.mimeType ?? resource.mimeType,
    size: previous.size ?? resource.size,
    projectId: previous.projectId ?? resource.projectId,
    projectName: previous.projectName ?? resource.projectName,
    operation: previous.operation ?? resource.operation,
  });
}

function addMentionResources(
  target: Map<string, ChatResource>,
  content: string,
): void {
  for (const match of content.matchAll(MENTION_TOKEN_RE)) {
    const kind = match[1]?.toLowerCase() as Exclude<ChatResourceKind, "attachment">;
    const id = cleanString(match[2]);
    const name = cleanString(match[3]);
    if (!id || !name) continue;
    addResource(target, {
      key: canonicalKey(kind, id),
      kind,
      id,
      name,
      href: resourceHref(kind, id),
      source: "mention",
    });
  }
  for (const match of content.matchAll(DOCS_WIKILINK_RE)) {
    const id = cleanString(match[1]);
    const name = cleanString(match[2]);
    if (!id || !name) continue;
    addResource(target, {
      key: canonicalKey("docs", id),
      kind: "docs",
      id,
      name,
      href: resourceHref("docs", id),
      source: "mention",
    });
  }
}

function addAttachmentResources(
  target: Map<string, ChatResource>,
  message: ConversationMessage,
): void {
  const attachments = message.metadata?.attachments;
  if (!Array.isArray(attachments)) return;
  attachments.forEach((raw, index) => {
    if (!raw || typeof raw !== "object") return;
    const attachment = raw as ChatAttachmentMetadata;
    const name = cleanString(attachment.name);
    if (!name || attachment.upload_failed) return;
    const path = cleanString(attachment.path) ?? cleanString(attachment.project_relative_path);
    // Persisted attachments normally have a canonical path.  Keep a
    // name-only persisted attachment visible with a message-scoped key rather
    // than incorrectly deduping two unrelated files that share a name.
    const identity = path ?? `${message.id}:${index}:${name}`;
    addResource(target, {
      key: canonicalKey("attachment", identity),
      kind: "attachment",
      name,
      path: path ? canonicalPath(path) : undefined,
      mimeType: cleanString(attachment.mime_type) ?? undefined,
      size: typeof attachment.size === "number" ? attachment.size : undefined,
      source: "attachment",
    });
  });
}

function addAgentMutationResources(
  target: Map<string, ChatResource>,
  mutations: AgentResourceMutation[] | undefined,
): void {
  if (!Array.isArray(mutations)) return;
  for (const mutation of mutations) {
    if (!mutation?.success) continue;
    const id = cleanString(mutation.resource_id);
    const title = cleanString(mutation.title) ?? "名称未取得";
    if (!id) continue;
    const kind: ChatResourceKind = mutation.resource_type === "task" ? "task" : "docs";
    addResource(target, {
      key: canonicalKey(kind, id),
      kind,
      id,
      name: title,
      href: resourceHref(kind, id),
      projectName: mutation.project_name ?? undefined,
      operation: mutation.operation,
      source: "agent_mutation",
    });
  }
}

/**
 * Build the persistent Chat information rail's resource list.
 *
 * Only server/session metadata, persisted active-branch messages, explicit
 * canonical mentions, authorized related-task rows, and successful AgentRun
 * mutations are accepted.  Transient/local draft messages and attachment
 * `data_url` values are intentionally excluded.
 */
export function normalizeChatResources({
  session,
  projectName,
  messages = [],
  relatedTasks = [],
  agentRun,
}: ChatResourceNormalizerInput): ChatResource[] {
  const resources = new Map<string, ChatResource>();
  const persistedMessages = messages.filter(
    (message) =>
      message?.is_active_branch !== false &&
      !message.id.startsWith("temp-") &&
      !message.id.startsWith("msg-"),
  );

  for (const message of persistedMessages) {
    addAttachmentResources(resources, message);
    addMentionResources(resources, message.content ?? "");
  }

  for (const task of relatedTasks) {
    const id = cleanString(task?.id);
    const title = cleanString(task?.title);
    if (!id || !title) continue;
    addResource(resources, {
      key: canonicalKey("task", id),
      kind: "task",
      id,
      name: title,
      href: resourceHref("task", id),
      projectId: task.project_id ?? undefined,
      projectName: task.project_name ?? undefined,
      source: "task_reference",
    });
  }
  addAgentMutationResources(resources, agentRun?.resource_mutations);

  // Session-level associations are explicit API fields, not inferred from
  // titles/content.  Do not surface context/privacy metadata in the rail.
  const projectId = cleanString(session?.project_id);
  if (projectId) {
    addResource(resources, {
      key: canonicalKey("project", projectId),
      kind: "project",
      id: projectId,
      name: projectName || "プロジェクト",
      projectId,
      projectName: projectName ?? undefined,
      source: "session_metadata",
    });
  }
  const appId = cleanString(session?.app_id);
  if (appId) {
    addResource(resources, {
      key: canonicalKey("app", appId),
      kind: "app",
      id: appId,
      name: "App",
      href: resourceHref("app", appId),
      source: "session_metadata",
    });
  }

  const kindOrder: Record<ChatResourceKind, number> = {
    attachment: 0,
    file: 1,
    task: 2,
    docs: 3,
    chat_session: 4,
    project: 5,
    app: 6,
  };
  return [...resources.values()].sort(
    (left, right) =>
      kindOrder[left.kind] - kindOrder[right.kind] ||
      left.name.localeCompare(right.name, "ja") ||
      left.key.localeCompare(right.key),
  );
}
