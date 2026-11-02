import { fetchApi } from "./api-client";

export type SessionMainRoute = {
  provider: string;
  model: string;
  effort?: string;
};

export type SessionLlmSettingsEnvelope = {
  saved_settings?: { main_route?: unknown } | null;
  settings?: { main_route?: unknown } | null;
  effective_main?: unknown;
  warnings?: unknown;
};

function sessionSettingsPath(sessionId: string): string {
  return `/api/llm/session-settings?session_id=${encodeURIComponent(sessionId)}`;
}

export function normalizeSessionMainRoute(value: unknown): SessionMainRoute | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const route = value as Record<string, unknown>;
  const provider = typeof route.provider === "string" ? route.provider.trim() : "";
  const model = typeof route.model === "string" ? route.model.trim() : "";
  if (!provider || !model) return null;
  const effortValue = route.effort ?? route.reasoning_effort;
  const effort = typeof effortValue === "string" ? effortValue.trim() : "";
  return { provider, model, ...(effort ? { effort } : {}) };
}

export async function getSessionLlmSettings(
  sessionId: string,
): Promise<SessionLlmSettingsEnvelope> {
  return fetchApi<SessionLlmSettingsEnvelope>(sessionSettingsPath(sessionId), { method: "GET" });
}

export async function putSessionMainRoute(
  sessionId: string,
  mainRoute: SessionMainRoute,
): Promise<SessionLlmSettingsEnvelope> {
  return fetchApi<SessionLlmSettingsEnvelope>("/api/llm/session-settings", {
    method: "PUT",
    body: JSON.stringify({
      session_id: sessionId,
      update_last_used: false,
      main_route: mainRoute,
    }),
  });
}
