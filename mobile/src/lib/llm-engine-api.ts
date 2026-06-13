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
  async get(): Promise<LlmEngineState> {
    return fetchApi<LlmEngineState>("/api/llm/engine");
  },

  async set(provider: string, model: string): Promise<LlmEngineState> {
    await fetchApi<unknown>("/api/llm/engine", {
      method: "POST",
      body: JSON.stringify({ provider, model }),
    });
    return this.get();
  },
};
