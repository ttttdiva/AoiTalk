import {
  Ban,
  Brain,
  Check,
  Circle,
  FilePenLine,
  Loader2,
  MessageSquareText,
  RefreshCw,
  Search,
  Terminal,
  Users,
  Wrench,
  X,
} from "lucide-react";
import type { AgentRunTimelineItem } from "@/lib/chat-api";
import { isFileEdit, operationCommand } from "@/lib/agent-run-timeline-format";
import {
  isProgressRow,
  isReviewRow,
  timelineRowKind,
} from "@/lib/agent-run-timeline-rows";

/** 実行結果（成功・失敗・実行中）を示すアイコン */
export function operationIcon(item: AgentRunTimelineItem) {
  const running =
    item.display_status === "started" || item.status === "running";
  const cancelled = item.status === "cancelled";
  const failed =
    item.success === false || item.status === "failed" || Boolean(item.error);
  if (running) return <Loader2 className="size-3.5 animate-spin" />;
  if (cancelled) return <Ban className="size-3.5" />;
  if (failed) return <X className="size-3.5" />;
  if (item.success === true || item.status === "succeeded") {
    return <Check className="size-3.5" />;
  }
  return <Circle className="size-3.5" />;
}

/** 操作の種類を示すアイコン */
export function operationTypeIcon(item: AgentRunTimelineItem) {
  const kind = timelineRowKind(item);
  if (kind === "text") return <MessageSquareText className="size-3.5" />;
  if (kind === "thinking") return <Brain className="size-3.5" />;
  if (isReviewRow(item) || isProgressRow(item))
    return <RefreshCw className="size-3.5" />;
  if (item.event_type === "agent_operation")
    return <Users className="size-3.5" />;
  if (operationCommand(item)) return <Terminal className="size-3.5" />;
  if (isFileEdit(item)) return <FilePenLine className="size-3.5" />;
  if (
    String(item.tool_name ?? "")
      .toLowerCase()
      .includes("search")
  ) {
    return <Search className="size-3.5" />;
  }
  return <Wrench className="size-3.5" />;
}
