import type {
  SessionLlmSettings,
  SessionLlmSettingsResponse,
} from "@/lib/chat-llm-settings";
import {
  hasExplicitSessionRoute,
  normalizeRouteFragment,
  type RouteFragment,
} from "@/lib/chat-session-route";

export class PendingLlmHandoffError extends Error {
  readonly code = "pending_llm_handoff_failed";

  constructor(message: string) {
    super(message);
    this.name = "PendingLlmHandoffError";
  }
}

export function routesMatch(
  expected: RouteFragment | SessionLlmSettings["main_route"] | null | undefined,
  actual:
    | RouteFragment
    | SessionLlmSettingsResponse["effective_main"]
    | null
    | undefined,
): boolean {
  const left = normalizeRouteFragment(expected ?? {});
  const right = normalizeRouteFragment(actual ?? {});
  if (left.provider && left.provider !== right.provider) return false;
  if (left.model && left.model !== right.model) return false;
  if (left.effort && left.effort !== right.effort) return false;
  return true;
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

/** handoff PUT 用。Team/Profile は送らず、明示された session route だけを保存する。 */
export function buildHandoffSettingsPatch(
  settings: SessionLlmSettings,
  generationReadyMain?: RouteFragment | SessionLlmSettings["main_route"] | null,
): Partial<SessionLlmSettings> {
  const mainRoute = pickHandoffMainRoute(settings, generationReadyMain);
  return mainRoute ? { main_route: mainRoute } : {};
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

  const pendingRoute = normalizeRouteFragment(pending.main_route);
  const requestedMainRoute = Boolean(pendingRoute.provider && pendingRoute.model);
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
