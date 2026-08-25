import { fetchApi } from "./api-client";
import type {
  MemoryScope,
  ScopedMemory,
  ScopedMemoryMutation,
  ScopedMemorySettings,
} from "../types/api";

export type ScopedMemoryListOptions = {
  scope?: MemoryScope;
  scopeId?: string;
  projectId?: string;
  taskId?: string;
  sessionId?: string;
  status?: string;
  includeHistory?: boolean;
  limit?: number;
};

export type CreateScopedMemoryRequest = {
  content: string;
  scope?: MemoryScope;
  scopeId?: string;
  projectId?: string;
  taskId?: string;
  sessionId?: string;
  memoryType?: string;
  title?: string;
  sourceRef?: string;
  evidenceRefs?: Array<Record<string, unknown>>;
  evidenceSpan?: Record<string, unknown>;
  confidence?: number;
  importance?: number;
  isPinned?: boolean;
  status?: string;
  idempotencyKey?: string;
};

export type UpdateScopedMemoryRequest = {
  version: number;
  content?: string;
  title?: string | null;
  memoryType?: string;
  structuredData?: Record<string, unknown>;
  confidence?: number;
  importance?: number;
  isPinned?: boolean;
  expiresAt?: string | null;
  trustLevel?: string;
  evidenceRefs?: Array<Record<string, unknown>>;
  evidenceSpan?: Record<string, unknown>;
};

export type ScopedMemoryJob = {
  id: string;
  session_id: string;
  project_id?: string | null;
  status: string;
  attempts: number;
  error?: string | null;
  next_retry_at?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
};

export type ScopedMemoryExplanation = {
  success: boolean;
  memory: ScopedMemory;
  lineage: { ancestors: ScopedMemory[]; descendants: ScopedMemory[] };
  explanation: Record<string, unknown>;
};

function memoryQuery(options: ScopedMemoryListOptions = {}): string {
  const params = new URLSearchParams();
  const entries: Array<[string, string | number | boolean | undefined]> = [
    ["scope", options.scope],
    ["scope_id", options.scopeId],
    ["project_id", options.projectId],
    ["task_id", options.taskId],
    ["session_id", options.sessionId],
    ["status", options.status],
    ["include_history", options.includeHistory],
    ["limit", options.limit],
  ];
  for (const [key, value] of entries) {
    if (value === undefined || value === null || value === "") continue;
    params.set(key, String(value));
  }
  const query = params.toString();
  return query ? `?${query}` : "";
}

function createBody(data: CreateScopedMemoryRequest): Record<string, unknown> {
  return {
    content: data.content,
    ...(data.scope !== undefined ? { scope: data.scope } : {}),
    ...(data.scopeId !== undefined ? { scope_id: data.scopeId } : {}),
    ...(data.projectId !== undefined ? { project_id: data.projectId } : {}),
    ...(data.taskId !== undefined ? { task_id: data.taskId } : {}),
    ...(data.sessionId !== undefined ? { session_id: data.sessionId } : {}),
    ...(data.memoryType !== undefined ? { memory_type: data.memoryType } : {}),
    ...(data.title !== undefined ? { title: data.title } : {}),
    ...(data.sourceRef !== undefined ? { source_ref: data.sourceRef } : {}),
    ...(data.evidenceRefs !== undefined ? { evidence_refs: data.evidenceRefs } : {}),
    ...(data.evidenceSpan !== undefined ? { evidence_span: data.evidenceSpan } : {}),
    ...(data.confidence !== undefined ? { confidence: data.confidence } : {}),
    ...(data.importance !== undefined ? { importance: data.importance } : {}),
    ...(data.isPinned !== undefined ? { is_pinned: data.isPinned } : {}),
    ...(data.status !== undefined ? { status: data.status } : {}),
    ...(data.idempotencyKey !== undefined ? { idempotency_key: data.idempotencyKey } : {}),
  };
}

function updateBody(data: UpdateScopedMemoryRequest): Record<string, unknown> {
  return {
    version: data.version,
    ...(data.content !== undefined ? { content: data.content } : {}),
    ...(data.title !== undefined ? { title: data.title } : {}),
    ...(data.memoryType !== undefined ? { memory_type: data.memoryType } : {}),
    ...(data.structuredData !== undefined ? { structured_data: data.structuredData } : {}),
    ...(data.confidence !== undefined ? { confidence: data.confidence } : {}),
    ...(data.importance !== undefined ? { importance: data.importance } : {}),
    ...(data.isPinned !== undefined ? { is_pinned: data.isPinned } : {}),
    ...(data.expiresAt !== undefined ? { expires_at: data.expiresAt } : {}),
    ...(data.trustLevel !== undefined ? { trust_level: data.trustLevel } : {}),
    ...(data.evidenceRefs !== undefined ? { evidence_refs: data.evidenceRefs } : {}),
    ...(data.evidenceSpan !== undefined ? { evidence_span: data.evidenceSpan } : {}),
  };
}

async function unwrapMemory(response: ScopedMemoryMutation): Promise<ScopedMemory> {
  if (!response.memory) {
    throw new Error("メモリAPIの応答にmemoryがありません。");
  }
  return response.memory;
}

export const memoryApi = {
  async list(options?: ScopedMemoryListOptions): Promise<ScopedMemory[]> {
    const data = await fetchApi<{ success: boolean; memories: ScopedMemory[] }>(
      `/api/memories${memoryQuery(options)}`,
    );
    return data.memories ?? [];
  },

  async get(id: string): Promise<ScopedMemory> {
    const response = await fetchApi<{ success: boolean; memory: ScopedMemory }>(
      `/api/memories/${encodeURIComponent(id)}`,
    );
    return response.memory;
  },

  async create(data: CreateScopedMemoryRequest): Promise<ScopedMemory> {
    const response = await fetchApi<ScopedMemoryMutation>("/api/memories", {
      method: "POST",
      body: JSON.stringify(createBody(data)),
    });
    return unwrapMemory(response);
  },

  async update(
    id: string,
    data: UpdateScopedMemoryRequest,
  ): Promise<ScopedMemory> {
    const response = await fetchApi<ScopedMemoryMutation>(
      `/api/memories/${encodeURIComponent(id)}`,
      {
        method: "PATCH",
        body: JSON.stringify(updateBody(data)),
      },
    );
    return unwrapMemory(response);
  },

  async delete(id: string, version: number): Promise<ScopedMemoryMutation> {
    return fetchApi<ScopedMemoryMutation>(
      `/api/memories/${encodeURIComponent(id)}?version=${encodeURIComponent(String(version))}`,
      { method: "DELETE" },
    );
  },

  async deleteAll(): Promise<number> {
    const response = await fetchApi<{ success: boolean; forgotten: number }>(
      "/api/memories/all",
      { method: "DELETE" },
    );
    return response.forgotten ?? 0;
  },

  async approve(id: string, version: number, reason?: string): Promise<ScopedMemory> {
    const response = await fetchApi<ScopedMemoryMutation>(
      `/api/memories/${encodeURIComponent(id)}/approve`,
      {
        method: "POST",
        body: JSON.stringify({ version, ...(reason ? { reason } : {}) }),
      },
    );
    return unwrapMemory(response);
  },

  async reject(id: string, version: number, reason?: string): Promise<ScopedMemory> {
    const response = await fetchApi<ScopedMemoryMutation>(
      `/api/memories/${encodeURIComponent(id)}/reject`,
      {
        method: "POST",
        body: JSON.stringify({ version, ...(reason ? { reason } : {}) }),
      },
    );
    return unwrapMemory(response);
  },

  async moveScope(
    id: string,
    data: {
      version: number;
      scope: MemoryScope;
      scopeId?: string;
      projectId?: string;
      taskId?: string;
      sessionId?: string;
      reason?: string;
    },
  ): Promise<ScopedMemory> {
    const response = await fetchApi<ScopedMemoryMutation>(
      `/api/memories/${encodeURIComponent(id)}/move-scope`,
      {
        method: "POST",
        body: JSON.stringify({
          version: data.version,
          scope: data.scope,
          ...(data.scopeId !== undefined ? { scope_id: data.scopeId } : {}),
          ...(data.projectId !== undefined ? { project_id: data.projectId } : {}),
          ...(data.taskId !== undefined ? { task_id: data.taskId } : {}),
          ...(data.sessionId !== undefined ? { session_id: data.sessionId } : {}),
          ...(data.reason !== undefined ? { reason: data.reason } : {}),
        }),
      },
    );
    return unwrapMemory(response);
  },

  async explain(id: string): Promise<ScopedMemoryExplanation> {
    return fetchApi<ScopedMemoryExplanation>(
      `/api/memories/${encodeURIComponent(id)}/explain`,
    );
  },

  async getSettings(projectId?: string): Promise<ScopedMemorySettings> {
    const query = projectId ? `?project_id=${encodeURIComponent(projectId)}` : "";
    const response = await fetchApi<{
      success: boolean;
      settings: ScopedMemorySettings;
    }>(`/api/memories/settings${query}`);
    return response.settings;
  },

  async updateSettings(data: {
    userAutoEnabled?: boolean;
    projectId?: string;
    projectAutoEnabled?: boolean;
  }): Promise<ScopedMemorySettings> {
    const response = await fetchApi<{
      success: boolean;
      settings: ScopedMemorySettings;
    }>("/api/memories/settings", {
      method: "PATCH",
      body: JSON.stringify({
        ...(data.userAutoEnabled !== undefined
          ? { user_auto_enabled: data.userAutoEnabled }
          : {}),
        ...(data.projectId !== undefined ? { project_id: data.projectId } : {}),
        ...(data.projectAutoEnabled !== undefined
          ? { project_auto_enabled: data.projectAutoEnabled }
          : {}),
      }),
    });
    return response.settings;
  },

  async listJobs(limit = 50): Promise<ScopedMemoryJob[]> {
    const response = await fetchApi<{
      success: boolean;
      jobs: ScopedMemoryJob[];
    }>(`/api/memories/jobs?limit=${encodeURIComponent(String(limit))}`);
    return response.jobs ?? [];
  },
};
