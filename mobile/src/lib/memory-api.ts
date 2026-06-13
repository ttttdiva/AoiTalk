import { fetchApi } from "./api-client";
import type { UserMemory } from "../types/api";

export const memoryApi = {
  async list(activeOnly = false): Promise<UserMemory[]> {
    const query = activeOnly ? "?active_only=true" : "";
    const data = await fetchApi<{ success: boolean; memories: UserMemory[] }>(
      `/api/memories${query}`,
    );
    return data.memories ?? [];
  },

  async create(data: { content: string; category?: string }): Promise<UserMemory> {
    const response = await fetchApi<{ success: boolean; memory: UserMemory }>(
      "/api/memories",
      {
        method: "POST",
        body: JSON.stringify(data),
      },
    );
    return response.memory;
  },

  async update(id: string, data: Partial<UserMemory>): Promise<UserMemory> {
    const response = await fetchApi<{ success: boolean; memory: UserMemory }>(
      `/api/memories/${encodeURIComponent(id)}`,
      {
        method: "PATCH",
        body: JSON.stringify(data),
      },
    );
    return response.memory;
  },

  async delete(id: string): Promise<void> {
    await fetchApi(`/api/memories/${encodeURIComponent(id)}`, {
      method: "DELETE",
    });
  },

  async toggle(id: string): Promise<UserMemory> {
    const response = await fetchApi<{ success: boolean; memory: UserMemory }>(
      `/api/memories/${encodeURIComponent(id)}/toggle`,
      { method: "POST" },
    );
    return response.memory;
  },

  async deleteAll(): Promise<number> {
    const response = await fetchApi<{ success: boolean; deleted: number }>(
      "/api/memories/all",
      { method: "DELETE" },
    );
    return response.deleted ?? 0;
  },
};
