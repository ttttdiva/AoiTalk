import { fetchApi } from "./api-client";

export type LlmEngineOption = {
  provider: string;
  model: string;
  label: string;
};

export type LlmEngineState = {
  provider: string;
  model: string;
  available: LlmEngineOption[];
};

export const llmEngineApi = {
  async get(includeAvailable = false): Promise<LlmEngineState> {
    const query = includeAvailable ? "?include_available=true" : "";
    return fetchApi<LlmEngineState>(`/api/llm/engine${query}`);
  },

  async set(provider: string, model: string): Promise<LlmEngineState> {
    await fetchApi<unknown>("/api/llm/engine", {
      method: "POST",
      body: JSON.stringify({ provider, model }),
    });
    return this.get(true);
  },
};
