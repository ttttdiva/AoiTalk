import { fetchApi } from "./api-client";
import type { ManagedCharacter } from "../types/api";

export const characterApi = {
  async list(enabledOnly = false): Promise<ManagedCharacter[]> {
    const suffix = enabledOnly ? "?enabled_only=true" : "";
    const data = await fetchApi<{
      success: boolean;
      characters: ManagedCharacter[];
    }>(`/api/characters/manage${suffix}`);
    return data.characters;
  },

  async toggle(characterId: string): Promise<ManagedCharacter> {
    const data = await fetchApi<{
      success: boolean;
      character: ManagedCharacter;
    }>(`/api/characters/manage/${characterId}/toggle`, {
      method: "POST",
    });
    return data.character;
  },
};
