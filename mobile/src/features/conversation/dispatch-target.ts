import type { ChatResponseModelOption, ConversationSession } from "../../types/api";
import type { ChatResponseTarget } from "./chat-llm-preferences";
import { snapshotServerModelTarget } from "./server-model-effort";

/** Flat server-owned markers only; nested context is not client authority. */
export function isProtectedConversationRoot(session: ConversationSession | null): boolean {
  return Boolean(
    session?.private_agent_root === true ||
    (session?.purpose === "aoidots" && session.readonly_binding === true),
  );
}

/**
 * Freeze ordinary Chat's server target for one turn, while keeping a private
 * AoiDots root on its bound Agent route without importing global model state.
 */
export function resolveConversationDispatchTarget(
  session: ConversationSession | null,
  target: ChatResponseTarget,
  models: readonly ChatResponseModelOption[],
): ChatResponseTarget {
  if (isProtectedConversationRoot(session)) return { kind: "server" };
  return snapshotServerModelTarget(target, models);
}
