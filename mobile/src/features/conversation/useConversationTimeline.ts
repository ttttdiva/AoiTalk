import { useMemo } from "react";
import type { ConversationMessage } from "../../types/api";
import type { ConversationJob, PermissionRequest } from "./models";
import { buildDurableTimeline } from "./timeline";

export type DurableConversationTimelineArgs = {
  messages: ConversationMessage[];
  permissions: PermissionRequest[];
  jobs: ConversationJob[];
  activeTool: string | null;
  activityMessage: string | null;
};

/** Streaming content intentionally is not part of this durable projection. */
export function useConversationDurableTimeline(
  args: DurableConversationTimelineArgs,
) {
  return useMemo(
    () => buildDurableTimeline(args),
    [
      args.activeTool,
      args.activityMessage,
      args.jobs,
      args.messages,
      args.permissions,
    ],
  );
}
