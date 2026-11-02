/** Role → model table: only what has no other home (chat fallbacks, work roles, image generation). */
export type ModelRoute =
  | { kind: "llm"; provider: string; model: string; effort?: string | null }
  | { kind: "pool"; pool_id: string };

/** Work roles an Agent can run; an empty route list inherits the chat model. */
export const WORK_ROLES = ["code", "review", "research", "summarize"] as const;
export type WorkRole = typeof WORK_ROLES[number];
export const WORK_ROLE_LABELS: Record<"chat" | WorkRole, string> = { chat: "会話", code: "コード", review: "レビュー", research: "調査", summarize: "要約" };

export type ModelRoles = {
  version: number;
  roles: {
    chat?: { fallbacks?: ModelRoute[]; allow_paid_fallback?: boolean };
    image_generation?: { primary?: ModelRoute | null };
  } & Partial<Record<WorkRole, { routes?: ModelRoute[] }>>;
};

export class ModelRolesConflictError extends Error {
  constructor() {
    super("ほかの画面で役割の設定が更新されました。読み込み直してから保存してください。");
  }
}

async function request<T>(method: "GET" | "PUT", body?: unknown): Promise<T> {
  const response = await fetch("/api/python-proxy/model-roles", {
    method,
    credentials: "include",
    cache: "no-store",
    headers: { "Content-Type": "application/json" },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  if (response.status === 409) throw new ModelRolesConflictError();
  if (!response.ok) throw new Error(`役割の設定を${method === "GET" ? "読み込め" : "保存でき"}ませんでした (${response.status})`);
  return response.json() as Promise<T>;
}

function normalize(value: Partial<ModelRoles> | null | undefined): ModelRoles {
  return { version: Number(value?.version ?? 0) || 0, roles: value?.roles && typeof value.roles === "object" ? value.roles : {} };
}

export const modelRolesApi = {
  get: async () => normalize(await request<Partial<ModelRoles>>("GET")),
  put: async (expectedVersion: number, roles: ModelRoles["roles"]) => normalize(await request<Partial<ModelRoles>>("PUT", { expected_version: expectedVersion, roles })),
};

export function routeLabel(route: ModelRoute): string {
  return route.kind === "pool" ? `無料Team: ${route.pool_id}` : `${route.provider} / ${route.model}`;
}

export function sameRoute(left: ModelRoute, right: ModelRoute): boolean {
  if (left.kind !== right.kind) return false;
  if (left.kind === "pool" && right.kind === "pool") return left.pool_id === right.pool_id;
  return left.kind === "llm" && right.kind === "llm" && left.provider === right.provider && left.model === right.model;
}
