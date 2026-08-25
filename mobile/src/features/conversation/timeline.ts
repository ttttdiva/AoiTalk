import type { ConversationMessage } from "../../types/api";
import type {
  ConversationEvent,
  ConversationJob,
  PermissionRequest,
  TimelineItem,
} from "./models";

export function groupMessageKey(message: ConversationMessage): string {
  return message.parent_message_id || "__root__";
}

export function upsertConversationMessage(
  messages: ConversationMessage[],
  next: ConversationMessage,
): ConversationMessage[] {
  const index = messages.findIndex((message) => message.id === next.id);
  if (index < 0) return [...messages, next];
  const updated = [...messages];
  updated[index] = next;
  return updated;
}

export type ConversationMessageDisplayRevision = {
  id: string;
  updatedAt: string | null;
  localRevision: string | number | null;
  role: ConversationMessage["role"];
  content: string;
  branchIndex: number;
  branchCount: number | null;
  activeBranch: boolean;
  generationStatus: unknown;
  pending: boolean;
  error: unknown;
  agentRunId: unknown;
  toolResults: unknown;
};

/**
 * ChatMessageRow が実際に表示する値だけを取り出す。
 * metadata 全体の stringify は行わず、local-only message は local_revision を使う。
 */
export function conversationMessageDisplayRevision(
  message: ConversationMessage,
): ConversationMessageDisplayRevision {
  return {
    id: message.id,
    updatedAt: message.updated_at ?? null,
    localRevision:
      (message.metadata?.local_revision as string | number | undefined) ?? null,
    role: message.role,
    content: message.content,
    branchIndex: message.branch_index,
    branchCount: message.branch_count ?? null,
    activeBranch: message.is_active_branch,
    generationStatus: message.metadata?.generation_status,
    pending: Boolean(message.metadata?.pending),
    error: message.metadata?.error,
    agentRunId: message.metadata?.agent_run_id,
    toolResults: message.metadata?.tool_results,
  };
}

export function sameConversationMessageDisplayRevision(
  previous: ConversationMessage,
  next: ConversationMessage,
): boolean {
  if (previous === next) return true;
  const left = conversationMessageDisplayRevision(previous);
  const right = conversationMessageDisplayRevision(next);
  return (
    left.id === right.id &&
    left.updatedAt === right.updatedAt &&
    left.localRevision === right.localRevision &&
    left.role === right.role &&
    left.content === right.content &&
    left.branchIndex === right.branchIndex &&
    left.branchCount === right.branchCount &&
    left.activeBranch === right.activeBranch &&
    left.generationStatus === right.generationStatus &&
    left.pending === right.pending &&
    left.error === right.error &&
    left.agentRunId === right.agentRunId &&
    left.toolResults === right.toolResults
  );
}

export type ConversationBranchPresentation = {
  index: number;
  count: number;
};

export function buildConversationBranchPresentations(
  messages: ConversationMessage[],
): Map<string, ConversationBranchPresentation> {
  const groups: Record<string, ConversationMessage[]> = {};
  for (const message of messages) {
    const key = groupMessageKey(message);
    if (!groups[key]) groups[key] = [];
    groups[key].push(message);
  }

  const result = new Map<string, ConversationBranchPresentation>();
  for (const [key, siblings] of Object.entries(groups)) {
    siblings.sort((a, b) => (a.branch_index ?? 0) - (b.branch_index ?? 0));
    for (let index = 0; index < siblings.length; index += 1) {
      const projectedCount = siblings[index].branch_count;
      const hasProjection =
        typeof projectedCount === "number" &&
        Number.isInteger(projectedCount) &&
        projectedCount > 1;
      const count = hasProjection
        ? Math.max(siblings.length, projectedCount)
        : siblings.length;
      result.set(siblings[index].id, {
        index:
          count <= 1 || (key === "__root__" && !hasProjection)
            ? -1
            : hasProjection
              ? siblings[index].branch_index ?? index
              : index,
        count,
      });
    }
  }
  return result;
}

export function selectVisibleMessages(
  messages: ConversationMessage[],
  branchSelections: Record<string, number>,
): ConversationMessage[] {
  const groups: Record<string, ConversationMessage[]> = {};
  for (const message of messages) {
    const key = groupMessageKey(message);
    if (!groups[key]) groups[key] = [];
    groups[key].push(message);
  }
  for (const siblings of Object.values(groups)) {
    siblings.sort((a, b) => {
      const branchDiff = (a.branch_index ?? 0) - (b.branch_index ?? 0);
      if (branchDiff !== 0) return branchDiff;
      return (a.created_at || "").localeCompare(b.created_at || "");
    });
  }

  const result: ConversationMessage[] = [];
  const emitted = new Set<string>();
  for (const message of messages) {
    const key = groupMessageKey(message);
    const siblings = groups[key];
    if (key === "__root__" || !siblings || siblings.length <= 1) {
      if (!emitted.has(message.id)) {
        result.push(message);
        emitted.add(message.id);
      }
      continue;
    }
    const emitKey = `${key}__branch__`;
    if (emitted.has(emitKey)) continue;
    emitted.add(emitKey);
    const activeIndex = siblings.findIndex((entry) => entry.is_active_branch);
    const selectedIndex = branchSelections[key] ?? (activeIndex >= 0 ? activeIndex : 0);
    if (siblings[selectedIndex]) {
      result.push(siblings[selectedIndex]);
    }
  }
  return result;
}

export function buildPermissionEvent(request: PermissionRequest): ConversationEvent {
  return {
    id: `permission-${request.requestId}`,
    kind: "permission",
    title: `${request.toolName} の確認`,
    description: request.description,
    severity: request.status === "pending" ? "warning" : "info",
    request,
  };
}

export function buildJobEvent(job: ConversationJob): ConversationEvent {
  const failed = job.status === "failed" || job.status === "cancelled";
  return {
    id: `job-${job.id}`,
    kind: "job",
    title: job.title,
    description:
      job.progressText ||
      (job.status === "completed" ? "完了しました" : `${job.status} / ${job.progress}%`),
    severity: failed ? "danger" : job.status === "completed" ? "info" : "warning",
    job,
  };
}

export function buildToolEvent(activeTool: string | null): ConversationEvent | null {
  if (!activeTool) return null;
  return {
    id: `tool-${activeTool}`,
    kind: "tool",
    title: "ツール実行中",
    description: activeTool,
    severity: "info",
    toolName: activeTool,
  };
}

export function buildProgressEvent(message: string | null): ConversationEvent | null {
  const text = message?.trim();
  if (!text) return null;
  return {
    id: "progress-current",
    kind: "progress",
    title: "実行状況",
    description: text,
    severity: "info",
  };
}

export function buildDurableTimeline(args: {
  messages: ConversationMessage[];
  permissions: PermissionRequest[];
  jobs: ConversationJob[];
  activeTool: string | null;
  activityMessage: string | null;
}): TimelineItem[] {
  const items: TimelineItem[] = args.messages.map((message) => ({
    id: message.id,
    type: "message",
    message,
  }));
  const events = [
    buildProgressEvent(args.activityMessage),
    buildToolEvent(args.activeTool),
    ...args.permissions.map(buildPermissionEvent),
    ...args.jobs.map(buildJobEvent),
  ].filter((event): event is ConversationEvent => Boolean(event));

  for (const event of events) {
    items.push({ id: event.id, type: "event", event });
  }
  return items;
}

export function buildTimeline(
  args: Parameters<typeof buildDurableTimeline>[0] & { streamContent: string },
): TimelineItem[] {
  const items = buildDurableTimeline(args);
  if (args.streamContent) {
    items.push({ id: "streaming-assistant", type: "stream", content: args.streamContent });
  }
  return items;
}
