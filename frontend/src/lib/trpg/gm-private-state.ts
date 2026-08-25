import type { TrpgPlayGmPrivateState, TrpgPlayParticipant, TrpgPlaySession } from "@/lib/trpg/play-api";

export function resolvePlayViewerParticipant(
  session: TrpgPlaySession | null | undefined,
  currentUserId?: string | null,
): TrpgPlayParticipant | null {
  if (!session?.participants?.length) return null;
  if (session.viewer_participant_id) {
    const matched = session.participants.find((item) => item.id === session.viewer_participant_id);
    if (matched) return matched;
  }
  if (currentUserId) {
    const byUser = session.participants.find((item) => item.user_id === currentUserId);
    if (byUser) return byUser;
  }
  return null;
}

export function removeGmPrivateStateForParticipant(
  current: TrpgPlayGmPrivateState[],
  participantId: string | null | undefined,
): TrpgPlayGmPrivateState[] {
  const id = String(participantId || "").trim();
  if (!id) return current;
  return current.filter((item) => item.participant_id !== id);
}

export function applyGmPrivateStateView(
  current: TrpgPlayGmPrivateState[],
  incoming: TrpgPlayGmPrivateState,
): TrpgPlayGmPrivateState[] {
  const entries = incoming.state?.entries ?? {};
  if (Object.keys(entries).length === 0) {
    return removeGmPrivateStateForParticipant(current, incoming.participant_id);
  }
  const existingIndex = current.findIndex((item) => item.participant_id === incoming.participant_id);
  const existing = existingIndex >= 0 ? current[existingIndex] : undefined;
  const merged: TrpgPlayGmPrivateState = {
    participant_id: incoming.participant_id,
    display_name: incoming.display_name ?? existing?.display_name ?? null,
    state: incoming.state,
    updated_at: incoming.updated_at ?? existing?.updated_at ?? null,
  };
  if (existingIndex < 0) {
    return [...current, merged];
  }
  const next = [...current];
  next[existingIndex] = merged;
  return next;
}

export function gmPrivateStateDisplayName(
  item: TrpgPlayGmPrivateState,
  participants: TrpgPlayParticipant[],
): string {
  return (
    item.display_name ||
    participants.find((participant) => participant.id === item.participant_id)?.display_name ||
    "参加者"
  );
}
