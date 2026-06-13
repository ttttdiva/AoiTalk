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

export function buildSyncEvent(args: {
  pendingMessages: number;
  disconnected: boolean;
}): ConversationEvent | null {
  if (args.pendingMessages > 0) {
    return {
      id: "sync-pending-messages",
      kind: "sync",
      title: "未送信キュー",
      description: `${args.pendingMessages} 件のメッセージがローカルに残っています。接続後に再送できます。`,
      severity: "warning",
    };
  }
  if (args.disconnected) {
    return {
      id: "sync-rest-dispatch",
      kind: "sync",
      title: "REST dispatch",
      description:
        "WebSocket は未接続です。送信は REST で行い、保存済みメッセージを遅延再取得します。",
      severity: "info",
    };
  }
  return null;
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

export function buildTimeline(args: {
  messages: ConversationMessage[];
  permissions: PermissionRequest[];
  jobs: ConversationJob[];
  activeTool: string | null;
  activityMessage: string | null;
  streamContent: string;
  pendingMessages: number;
  disconnected: boolean;
}): TimelineItem[] {
  const items: TimelineItem[] = args.messages.map((message) => ({
    id: message.id,
    type: "message",
    message,
  }));
  const events = [
    buildSyncEvent({
      pendingMessages: args.pendingMessages,
      disconnected: args.disconnected,
    }),
    buildProgressEvent(args.activityMessage),
    buildToolEvent(args.activeTool),
    ...args.permissions.map(buildPermissionEvent),
    ...args.jobs.map(buildJobEvent),
  ].filter((event): event is ConversationEvent => Boolean(event));

  for (const event of events) {
    items.push({ id: event.id, type: "event", event });
  }
  if (args.streamContent) {
    items.push({ id: "streaming-assistant", type: "stream", content: args.streamContent });
  }
  return items;
}
