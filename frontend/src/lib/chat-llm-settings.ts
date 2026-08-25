import {
  awaitLastUsedLlmRouteReady,
  enqueueLastUsedLlmRouteSave,
} from "@/lib/last-used-llm-route-save-queue";

export const FREE_TEAM_ROUTING_PROFILE_ID = "free-team";

export type AgentTeamSelection = {
  mode: "auto" | "fixed";
  team_id: string;
  loaded_team_ids: string[];
};

export type SessionMainRoute = {
  provider?: string;
  model?: string;
  effort?: string;
};

export type SessionSpecialRouting = {
  routing_profile_id?: string;
};

export type SessionLlmSettings = {
  agent_team_selection: AgentTeamSelection;
  main_route?: SessionMainRoute;
  special_routing?: SessionSpecialRouting;
  /** "" or omit = None. Chat/session の選択であり Team には保存しない。 */
  execution_profile_id?: string;
};

export type ExecutionProfileSummary = {
  profile_id: string;
  display_name: string;
  enabled?: boolean;
  system?: boolean;
};

export type SessionLlmSettingsResponse = {
  settings: SessionLlmSettings;
  active_execution_profile_id?: string;
  loaded_team_ids?: string[];
  effective_main?: {
    provider?: string | null;
    model?: string | null;
    effort?: string | null;
  };
  warnings?: string[];
};

export type ExecutionProfileEnvelope = {
  active_profile_id: string;
  manual: boolean;
  profiles: ExecutionProfileSummary[];
  effective_main?: {
    provider?: string | null;
    model?: string | null;
    effort?: string | null;
  };
};

export type NewChatLlmDefaultsResponse = {
  last_used_main: SessionMainRoute;
  effective_main: SessionMainRoute;
};

const EMPTY_NEW_CHAT_LLM_DEFAULTS: NewChatLlmDefaultsResponse = {
  last_used_main: {},
  effective_main: {},
};

export async function fetchSessionLlmSettings(
  sessionId: string,
): Promise<SessionLlmSettingsResponse> {
  const response = await fetch(
    `/api/python-proxy/llm/session-settings?session_id=${encodeURIComponent(sessionId)}`,
    { credentials: "include" },
  );
  if (!response.ok) {
    throw new Error(`Session LLM settings fetch failed (${response.status})`);
  }
  return (await response.json()) as SessionLlmSettingsResponse;
}

export async function saveSessionLlmSettings(
  sessionId: string,
  settings: Partial<SessionLlmSettings>,
): Promise<SessionLlmSettingsResponse> {
  const response = await fetch("/api/python-proxy/llm/session-settings", {
    method: "PUT",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: sessionId, settings }),
  });
  const data = (await response.json().catch(() => ({}))) as SessionLlmSettingsResponse & {
    detail?: string;
    message?: string;
  };
  if (!response.ok) {
    throw new Error(
      typeof data.detail === "string"
        ? data.detail
        : typeof data.message === "string"
          ? data.message
          : `Session LLM settings save failed (${response.status})`,
    );
  }
  return data;
}

export async function fetchExecutionProfiles(): Promise<ExecutionProfileEnvelope> {
  const response = await fetch("/api/python-proxy/llm/execution-profiles", {
    credentials: "include",
  });
  if (!response.ok) {
    throw new Error(`Execution profiles fetch failed (${response.status})`);
  }
  return (await response.json()) as ExecutionProfileEnvelope;
}

/** 同一タブの last-used PUT 完了を待ってから GET する。キュー失敗では表示を止めない。 */
export async function fetchNewChatLlmDefaultsAfterLastUsedFlush(
  userId?: string | null,
): Promise<NewChatLlmDefaultsResponse> {
  try {
    await awaitLastUsedLlmRouteReady(userId);
  } catch {
    // last-used の失敗は preference。新規チャット表示は続ける。
  }
  return fetchNewChatLlmDefaults();
}

/** 新規チャット初期 route（C→D 解決済み）。失敗時は空 envelope を返し新規チャット表示を止めない。 */
export async function fetchNewChatLlmDefaults(): Promise<NewChatLlmDefaultsResponse> {
  try {
    const response = await fetch("/api/python-proxy/llm/new-chat-defaults", {
      credentials: "include",
    });
    if (!response.ok) {
      return EMPTY_NEW_CHAT_LLM_DEFAULTS;
    }
    return (await response.json()) as NewChatLlmDefaultsResponse;
  } catch {
    return EMPTY_NEW_CHAT_LLM_DEFAULTS;
  }
}

/** 新規チャットでの選択を last-used (C) に残す。失敗しても呼び出し側の pending 保存は止めない。 */
export async function recordLastUsedLlmRoute(
  route: SessionMainRoute | null | undefined,
  options?: { userId?: string | null; updatedAt?: number },
): Promise<NewChatLlmDefaultsResponse> {
  const provider = typeof route?.provider === "string" ? route.provider.trim() : "";
  const model = typeof route?.model === "string" ? route.model.trim() : "";
  if (!provider || !model) {
    return EMPTY_NEW_CHAT_LLM_DEFAULTS;
  }
  const updatedAt =
    typeof options?.updatedAt === "number" && Number.isFinite(options.updatedAt)
      ? options.updatedAt
      : Date.now();
  const userKey = options?.userId?.trim() || "__default__";
  return enqueueLastUsedLlmRouteSave(userKey, () =>
    putLastUsedLlmRoute({ ...route, provider, model }, updatedAt),
  );
}

async function putLastUsedLlmRoute(
  route: SessionMainRoute,
  updatedAt: number,
): Promise<NewChatLlmDefaultsResponse> {
  try {
    const response = await fetch("/api/python-proxy/llm/new-chat-defaults", {
      method: "PUT",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        main_route: route,
        updated_at: updatedAt,
      }),
    });
    if (!response.ok) {
      return EMPTY_NEW_CHAT_LLM_DEFAULTS;
    }
    return (await response.json()) as NewChatLlmDefaultsResponse;
  } catch {
    return EMPTY_NEW_CHAT_LLM_DEFAULTS;
  }
}

export async function activateExecutionProfile(
  profileId: string,
): Promise<ExecutionProfileEnvelope & { effective_main?: ExecutionProfileEnvelope["effective_main"] }> {
  const response = await fetch(
    `/api/python-proxy/llm/execution-profiles/${encodeURIComponent(profileId)}/activate`,
    {
      method: "POST",
      credentials: "include",
    },
  );
  const data = (await response.json().catch(() => ({}))) as ExecutionProfileEnvelope & {
    detail?: string;
    message?: string;
    effective_main?: ExecutionProfileEnvelope["effective_main"];
  };
  if (!response.ok) {
    throw new Error(
      typeof data.detail === "string"
        ? data.detail
        : typeof data.message === "string"
          ? data.message
          : `Execution profile activation failed (${response.status})`,
    );
  }
  return data;
}

export type AgentTeamExecutionProfileOption = {
  profile_id: string;
  name: string;
  enabled?: boolean;
};

export type AgentTeamOption = {
  team_id: string;
  name: string;
  enabled?: boolean;
  execution_profiles?: AgentTeamExecutionProfileOption[];
};

type AgentTeamRecord = {
  team_id?: string;
  name?: string;
  enabled?: boolean;
  execution_profiles?: unknown;
};

function normalizeExecutionProfileOptions(raw: unknown): AgentTeamExecutionProfileOption[] {
  const fromEntry = (
    id: string,
    value: { profile_id?: string; name?: string; enabled?: boolean } | null | undefined,
  ): AgentTeamExecutionProfileOption | null => {
    const profileId = String(value?.profile_id || id).trim();
    if (!profileId) return null;
    return {
      profile_id: profileId,
      name: String(value?.name || profileId).trim() || profileId,
      enabled: value?.enabled !== false,
    };
  };

  if (Array.isArray(raw)) {
    return raw
      .map((item) => {
        if (!item || typeof item !== "object") return null;
        const profile = item as { profile_id?: string; name?: string; enabled?: boolean };
        return fromEntry(typeof profile.profile_id === "string" ? profile.profile_id : "", profile);
      })
      .filter((item): item is AgentTeamExecutionProfileOption => item !== null);
  }

  if (raw && typeof raw === "object") {
    return Object.entries(raw as Record<string, { profile_id?: string; name?: string; enabled?: boolean }>)
      .map(([id, profile]) => fromEntry(id, profile ?? {}))
      .filter((item): item is AgentTeamExecutionProfileOption => item !== null);
  }

  return [];
}

function normalizeAgentTeamRecord(
  teamId: string,
  team: AgentTeamRecord,
): AgentTeamOption | null {
  const normalizedId = (team.team_id ?? teamId).trim();
  if (!normalizedId) return null;
  if (team.enabled === false) return null;
  return {
    team_id: normalizedId,
    name: (team.name ?? normalizedId).trim() || normalizedId,
    enabled: team.enabled,
    execution_profiles: normalizeExecutionProfileOptions(team.execution_profiles),
  };
}

function normalizeAgentTeamList(raw: unknown): AgentTeamOption[] {
  if (Array.isArray(raw)) {
    return raw
      .map((item) => {
        if (!item || typeof item !== "object") return null;
        const team = item as AgentTeamRecord;
        const teamId = typeof team.team_id === "string" ? team.team_id : "";
        return normalizeAgentTeamRecord(teamId, team);
      })
      .filter((item): item is AgentTeamOption => item !== null);
  }

  if (raw && typeof raw === "object") {
    return Object.entries(raw as Record<string, AgentTeamRecord>)
      .map(([teamId, team]) => normalizeAgentTeamRecord(teamId, team ?? {}))
      .filter((item): item is AgentTeamOption => item !== null);
  }

  return [];
}

export async function fetchAgentTeamOptions(): Promise<AgentTeamOption[]> {
  const response = await fetch("/api/python-proxy/agent-team/config", {
    credentials: "include",
  });
  if (!response.ok) {
    throw new Error(`Agent team config fetch failed (${response.status})`);
  }
  const data = (await response.json()) as {
    agent_team?: { teams?: unknown };
    teams?: unknown;
  };
  const teams = data.agent_team?.teams ?? data.teams;
  return normalizeAgentTeamList(teams);
}
