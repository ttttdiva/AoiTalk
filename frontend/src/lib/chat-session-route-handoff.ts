import {
  FREE_TEAM_ROUTING_PROFILE_ID,
  type SessionLlmSettings,
  type SessionLlmSettingsResponse,
} from "@/lib/chat-llm-settings";
import {
  hasExplicitSessionRoute,
  normalizeRouteFragment,
  type RouteFragment,
} from "@/lib/chat-session-route";

export const FREE_TEAM_EFFECTIVE_ROUTE = {
  provider: "routing-profile",
  model: FREE_TEAM_ROUTING_PROFILE_ID,
} as const;

export class PendingLlmHandoffError extends Error {
  readonly code = "pending_llm_handoff_failed";

  constructor(message: string) {
    super(message);
    this.name = "PendingLlmHandoffError";
  }
}

export function normalizeTeamIdArray(ids: string[] | undefined | null): string[] {
  if (!Array.isArray(ids)) return [];
  const seen = new Set<string>();
  const normalized: string[] = [];
  for (const item of ids) {
    const teamId = typeof item === "string" ? item.trim() : "";
    if (!teamId || seen.has(teamId)) continue;
    seen.add(teamId);
    normalized.push(teamId);
  }
  return normalized.sort();
}

export function routesMatch(
  expected: RouteFragment | SessionLlmSettings["main_route"] | null | undefined,
  actual: RouteFragment | SessionLlmSettingsResponse["effective_main"] | null | undefined,
): boolean {
  const left = normalizeRouteFragment(expected ?? {});
  const right = normalizeRouteFragment(actual ?? {});
  if (left.provider && left.provider !== right.provider) return false;
  if (left.model && left.model !== right.model) return false;
  if (left.effort && left.effort !== right.effort) return false;
  return true;
}

export function agentTeamSelectionsMatch(
  expected: SessionLlmSettings["agent_team_selection"] | null | undefined,
  actual: SessionLlmSettings["agent_team_selection"] | null | undefined,
): boolean {
  const left = expected ?? {
    mode: "auto" as const,
    team_id: "",
    loaded_team_ids: [],
  };
  const right = actual ?? {
    mode: "auto" as const,
    team_id: "",
    loaded_team_ids: [],
  };
  if (left.mode !== right.mode) return false;
  if (left.team_id.trim() !== right.team_id.trim()) return false;
  const leftLoaded = normalizeTeamIdArray(left.loaded_team_ids);
  const rightLoaded = normalizeTeamIdArray(right.loaded_team_ids);
  if (leftLoaded.length !== rightLoaded.length) return false;
  for (let index = 0; index < leftLoaded.length; index += 1) {
    if (leftLoaded[index] !== rightLoaded[index]) return false;
  }
  return true;
}

export function specialRoutingMatches(
  expected: SessionLlmSettings["special_routing"] | null | undefined,
  actual: SessionLlmSettings["special_routing"] | null | undefined,
): boolean {
  const left = expected?.routing_profile_id?.trim() ?? "";
  const right = actual?.routing_profile_id?.trim() ?? "";
  return left === right;
}

export function normalizeExecutionProfileId(value?: string | null): string {
  return typeof value === "string" ? value.trim() : "";
}

export function executionProfilesMatch(
  expected?: string | null,
  actual?: string | null,
): boolean {
  return normalizeExecutionProfileId(expected) === normalizeExecutionProfileId(actual);
}

export function isFreeTeamPending(pending: SessionLlmSettings): boolean {
  return pending.special_routing?.routing_profile_id?.trim() === FREE_TEAM_ROUTING_PROFILE_ID;
}

function pickHandoffMainRoute(
  settings: SessionLlmSettings,
  generationReadyMain?: RouteFragment | SessionLlmSettings["main_route"] | null,
): SessionLlmSettings["main_route"] | undefined {
  if (hasExplicitSessionRoute(generationReadyMain)) {
    return generationReadyMain ? { ...generationReadyMain } : {};
  }
  if (hasExplicitSessionRoute(settings.main_route)) {
    return settings.main_route ? { ...settings.main_route } : {};
  }
  return undefined;
}

/** handoff PUT 用。generation-ready / explicit な main_route があるときは必ず含める。 */
export function buildHandoffSettingsPatch(
  settings: SessionLlmSettings,
  generationReadyMain?: RouteFragment | SessionLlmSettings["main_route"] | null,
): Partial<SessionLlmSettings> {
  const patch: Partial<SessionLlmSettings> = {
    agent_team_selection: {
      mode: settings.agent_team_selection.mode,
      team_id: settings.agent_team_selection.team_id,
      loaded_team_ids: [...settings.agent_team_selection.loaded_team_ids],
    },
  };
  const mainRoute = pickHandoffMainRoute(settings, generationReadyMain);
  if (mainRoute) {
    patch.main_route = mainRoute;
  }
  if (settings.special_routing !== undefined) {
    patch.special_routing = settings.special_routing
      ? { ...settings.special_routing }
      : {};
  }
  patch.execution_profile_id = normalizeExecutionProfileId(settings.execution_profile_id);
  return patch;
}

export function assertPendingHandoffApplied(
  pending: SessionLlmSettings,
  response: SessionLlmSettingsResponse | null | undefined,
): SessionLlmSettingsResponse {
  if (!response) {
    throw new PendingLlmHandoffError(
      "新規チャットのモデル設定をセッションへ反映できませんでした。",
    );
  }

  if (response.warnings?.length) {
    throw new PendingLlmHandoffError(
      response.warnings.join(" ") ||
        "新規チャットのモデル設定をセッションへ反映できませんでした。",
    );
  }

  if (!agentTeamSelectionsMatch(pending.agent_team_selection, response.settings.agent_team_selection)) {
    throw new PendingLlmHandoffError(
      "新規チャットの Agent Team 設定がセッションに正しく反映されませんでした。",
    );
  }

  if (!specialRoutingMatches(pending.special_routing, response.settings.special_routing)) {
    throw new PendingLlmHandoffError(
      "新規チャットの Free Team 設定がセッションに正しく反映されませんでした。",
    );
  }

  if (!executionProfilesMatch(pending.execution_profile_id, response.settings.execution_profile_id)) {
    throw new PendingLlmHandoffError(
      "新規チャットの Execution Profile 設定がセッションに正しく反映されませんでした。",
    );
  }

  const pendingRoute = normalizeRouteFragment(pending.main_route);
  const requestedMainRoute = Boolean(pendingRoute.provider && pendingRoute.model);

  if (isFreeTeamPending(pending)) {
    if (
      !routesMatch(FREE_TEAM_EFFECTIVE_ROUTE, response.effective_main) &&
      !routesMatch(FREE_TEAM_EFFECTIVE_ROUTE, response.settings.main_route)
    ) {
      throw new PendingLlmHandoffError(
        "新規チャットの Free Team ルートがセッションに正しく反映されませんでした。",
      );
    }
    return response;
  }

  if (requestedMainRoute) {
    if (!routesMatch(pendingRoute, response.effective_main)) {
      throw new PendingLlmHandoffError(
        "新規チャットのモデル設定がセッションに正しく反映されませんでした。",
      );
    }
    if (
      hasExplicitSessionRoute(response.settings.main_route) &&
      !routesMatch(pendingRoute, response.settings.main_route)
    ) {
      throw new PendingLlmHandoffError(
        "新規チャットのモデル設定がセッションに正しく反映されませんでした。",
      );
    }
    if (!hasExplicitSessionRoute(response.settings.main_route)) {
      throw new PendingLlmHandoffError(
        "新規チャットのモデル設定がセッションに正しく反映されませんでした。",
      );
    }
  }

  return response;
}
