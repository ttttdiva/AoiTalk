import { StoryApiError } from "@/lib/story/api";
import type { StoryAssistFieldKind, StoryAssistSelection } from "@/components/story/assist/types";

export type StoryAssistProposeRequest = {
  field_kind: StoryAssistFieldKind;
  current_text: string;
  instruction: string;
  work_id?: string;
  episode_id?: string;
  character_id?: string;
  rulebook_id?: string;
  note_id?: string;
  selection?: { start: number; end: number };
  include_private_notes?: boolean;
  model?: Record<string, unknown>;
};

export type StoryAssistProposeResponse = {
  proposal: string;
};

function proxyUrl(apiPath: string): string {
  return apiPath.startsWith("/api/") ? `/api/python-proxy${apiPath.slice(4)}` : `/api/python-proxy${apiPath}`;
}

export async function proposeStoryAssist(
  body: StoryAssistProposeRequest,
): Promise<StoryAssistProposeResponse> {
  const response = await fetch(proxyUrl("/api/story/assist/propose"), {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    cache: "no-store",
  });
  const payload = await response.json().catch(() => null);
  if (!response.ok) throw new StoryApiError(response.status, payload);
  return payload as StoryAssistProposeResponse;
}

export function selectionPayload(
  selection: StoryAssistSelection | null | undefined,
): { start: number; end: number } | undefined {
  if (!selection || selection.start >= selection.end) return undefined;
  return { start: selection.start, end: selection.end };
}
