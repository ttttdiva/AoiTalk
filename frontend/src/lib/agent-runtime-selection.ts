import type {
  AgentTeamExecutionProfileOption,
  AgentTeamOption,
} from "@/lib/chat-llm-settings";

/**
 * Per-Chat runtime (ExecutionProfile) choice for an Agent-bound Chat. It is a
 * send-time admission value only: the Agent revision, its role Team and the
 * session LLM settings are never rewritten from it.
 */
export type AgentRuntimeSelection = {
  scope: string;
  profileId: string;
};

export function agentRuntimeScope(
  userId: string | null | undefined,
  sessionId: string | null | undefined,
  agentId: string | null | undefined,
): string {
  return JSON.stringify([userId ?? "", sessionId ?? "", agentId ?? ""]);
}

/** Unset/blank means "use the role revision default": never an explicit value. */
export function normalizeRuntimeProfileId(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const trimmed = value.trim();
  return trimmed ? trimmed : null;
}

export function selectedRuntimeProfileId(
  selection: AgentRuntimeSelection | null,
  scope: string,
): string | null {
  return selection && selection.scope === scope
    ? normalizeRuntimeProfileId(selection.profileId)
    : null;
}

/**
 * Formal system ExecutionProfile ids (server SYSTEM_EXECUTION_PROFILE_IDS).
 * They are offered next to the role Team's profiles, but this list is not an
 * authority: the server validates every explicit value at admission.
 */
export const SYSTEM_RUNTIME_PROFILE_OPTIONS: AgentTeamExecutionProfileOption[] = [
  { profile_id: "manual", name: "Manual", enabled: true },
  { profile_id: "free-team", name: "Free Team", enabled: true },
];

/**
 * Candidates: formal system profiles plus the role Team's enabled profiles
 * from the server catalog. A Team with zero own profiles still offers the
 * system choices; foreign/disabled profiles are never listed.
 */
export function runtimeProfileOptionsForTeam(
  teamId: string | null | undefined,
  teams: AgentTeamOption[],
): AgentTeamExecutionProfileOption[] {
  const id = normalizeRuntimeProfileId(teamId);
  const own = id
    ? (teams.find((team) => team.team_id === id)?.execution_profiles ?? []).filter(
        (profile) => profile.enabled !== false,
      )
    : [];
  const systemIds = new Set(SYSTEM_RUNTIME_PROFILE_OPTIONS.map((option) => option.profile_id));
  return [
    ...SYSTEM_RUNTIME_PROFILE_OPTIONS,
    ...own.filter((profile) => !systemIds.has(profile.profile_id)),
  ];
}
