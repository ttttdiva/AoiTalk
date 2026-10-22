import {
  fetchNewChatLlmDefaultsStrict,
  NewChatLlmDefaultsError,
  type FetchNewChatLlmDefaultsOptions,
  type SessionMainRoute,
} from "@/lib/chat-llm-settings";
import {
  hasExplicitSessionRoute,
  normalizeRouteFragment,
} from "@/lib/chat-session-route";
import {
  getPendingNewChatLlmSettings,
  hydratePendingNewChatLlmSettings,
} from "@/lib/new-chat-llm-settings-store";

export type NewChatSendRouteSource = "pending" | "defaults";

export type NewChatSendRoute = {
  route: SessionMainRoute;
  source: NewChatSendRouteSource;
};

export type NewChatSendRouteErrorCode =
  | "partial_pending"
  | "defaults_http"
  | "defaults_network"
  | "defaults_timeout"
  | "defaults_invalid"
  | "defaults_aborted"
  | "defaults_unresolved";

export class NewChatSendRouteError extends Error {
  readonly code: NewChatSendRouteErrorCode;

  constructor(code: NewChatSendRouteErrorCode, message: string, cause?: unknown) {
    super(message, cause === undefined ? undefined : { cause });
    this.name = "NewChatSendRouteError";
    this.code = code;
  }
}

function hasAnyRouteField(route: SessionMainRoute | null | undefined): boolean {
  const normalized = normalizeRouteFragment(route);
  return Boolean(normalized.provider || normalized.model || normalized.effort);
}

function normalizeExplicitRoute(route: SessionMainRoute): SessionMainRoute {
  const normalized = normalizeRouteFragment(route);
  return {
    provider: normalized.provider,
    model: normalized.model,
    ...(normalized.effort ? { effort: normalized.effort } : {}),
  };
}

function defaultsErrorCode(error: NewChatLlmDefaultsError): NewChatSendRouteErrorCode {
  switch (error.code) {
    case "http":
      return "defaults_http";
    case "network":
      return "defaults_network";
    case "timeout":
      return "defaults_timeout";
    case "invalid":
      return "defaults_invalid";
    case "aborted":
      return "defaults_aborted";
  }
}

function defaultsErrorMessage(error: NewChatLlmDefaultsError): string {
  switch (error.code) {
    case "http":
      return "モデル設定の取得に失敗しました。設定を確認してから再試行してください。";
    case "network":
      return "モデル設定サーバーに接続できませんでした。接続を確認してから再試行してください。";
    case "timeout":
      return "モデル設定の取得がタイムアウトしました。接続を確認してから再試行してください。";
    case "invalid":
      return "モデル設定の応答が不正です。設定を確認してから再試行してください。";
    case "aborted":
      return "モデル設定の取得をキャンセルしました。";
  }
}

/**
 * Resolve the route that a normal new-chat send may use.
 *
 * A complete pending route is the captured user intent. If no pending route
 * exists, only the server's canonical effective_main may authorize creation.
 * In particular, last_used_main is diagnostic preference data and cannot be
 * promoted when server resolution returned an empty effective route.
 */
export async function resolveNewChatSendRoute(
  userId?: string | null,
  options?: FetchNewChatLlmDefaultsOptions,
): Promise<NewChatSendRoute> {
  hydratePendingNewChatLlmSettings(userId);
  const pendingRoute = getPendingNewChatLlmSettings(userId).main_route;

  if (hasAnyRouteField(pendingRoute)) {
    if (!hasExplicitSessionRoute(pendingRoute)) {
      throw new NewChatSendRouteError(
        "partial_pending",
        "選択中の Provider / Model が未確定です。設定を確認してから再試行してください。",
      );
    }
    return {
      route: normalizeExplicitRoute(pendingRoute ?? {}),
      source: "pending",
    };
  }

  let defaults;
  try {
    defaults = await fetchNewChatLlmDefaultsStrict(userId, options);
  } catch (error) {
    if (error instanceof NewChatLlmDefaultsError) {
      throw new NewChatSendRouteError(
        defaultsErrorCode(error),
        defaultsErrorMessage(error),
        error,
      );
    }
    throw new NewChatSendRouteError(
      "defaults_network",
      "モデル設定を取得できませんでした。設定を確認してから再試行してください。",
      error,
    );
  }

  if (!hasExplicitSessionRoute(defaults.effective_main)) {
    throw new NewChatSendRouteError(
      "defaults_unresolved",
      "サーバーで Provider / Model を確定できませんでした。設定を確認してから再試行してください。",
    );
  }
  return {
    route: normalizeExplicitRoute(defaults.effective_main),
    source: "defaults",
  };
}
