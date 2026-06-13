import { fetchApi } from './api-client';
import {
  applyRemoteScenarioCharacters,
  applyRemoteScenarioEpisodes,
  applyRemoteScenarios,
  applyRemoteScenarioScenes,
  applyScenarioTombstones,
  scenariosRepo,
} from '../repositories/scenarios';
import type {
  CanonEntry,
  Scenario,
  ScenarioCharacter,
  ScenarioDetail,
  ScenarioEpisode,
  ScenarioScene,
  ScenarioWritingSession,
} from '../types/api';

type ScenarioPayload = {
  title: string;
  description?: string;
  genre?: string;
  perspective?: string;
  setting?: string;
  opening_text?: string;
  gm_instructions?: string;
  tags?: string[];
  cover_image_path?: string;
  is_published?: boolean;
};

async function listScenariosRemote(): Promise<Scenario[]> {
  const data = await fetchApi<{ scenarios: Scenario[] }>('/api/scenarios');
  return data.scenarios;
}

async function getScenarioRemote(scenarioId: string): Promise<ScenarioDetail> {
  return fetchApi<ScenarioDetail>(`/api/scenarios/${scenarioId}`);
}

export const scenarioApi = {
  listRemote: listScenariosRemote,
  getRemote: getScenarioRemote,

  async list(): Promise<Scenario[]> {
    return scenariosRepo.list();
  },

  async get(scenarioId: string): Promise<ScenarioDetail> {
    return scenariosRepo.get(scenarioId);
  },

  async create(payload: ScenarioPayload): Promise<Scenario> {
    const scenario = await fetchApi<Scenario>('/api/scenarios', {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    await applyRemoteScenarios([scenario]);
    return scenario;
  },

  async update(scenarioId: string, payload: Partial<ScenarioPayload>): Promise<Scenario> {
    const scenario = await fetchApi<Scenario>(`/api/scenarios/${scenarioId}`, {
      method: 'PUT',
      body: JSON.stringify(payload),
    });
    await applyRemoteScenarios([scenario]);
    return scenario;
  },

  async delete(scenarioId: string): Promise<void> {
    await fetchApi(`/api/scenarios/${scenarioId}`, { method: 'DELETE' });
    await applyScenarioTombstones([{ id: scenarioId }]);
  },

  async startPlay(scenarioId: string): Promise<{ id: string; conversation_session_id: string }> {
    return fetchApi(`/api/scenarios/${scenarioId}/play`, {
      method: 'POST',
      body: JSON.stringify({}),
    });
  },

  async startWritingSession(
    scenarioId: string,
    payload: {
      target_episode_id?: string;
      target_scene_id?: string;
      writing_prompt?: string;
    },
  ): Promise<ScenarioWritingSession> {
    return fetchApi(`/api/scenarios/${scenarioId}/write`, {
      method: 'POST',
      body: JSON.stringify(payload),
    });
  },

  async getWritingSession(sessionId: string): Promise<ScenarioWritingSession> {
    return fetchApi(`/api/scenarios/write/${sessionId}`);
  },

  async getWritingSessionByConversation(conversationSessionId: string): Promise<ScenarioWritingSession | null> {
    return fetchApi(`/api/scenarios/write/by-conversation/${conversationSessionId}`);
  },

  async updateWritingSession(
    sessionId: string,
    payload: Partial<{
      writing_prompt: string;
      status: string;
      target_episode_id: string;
      target_scene_id: string;
    }>,
  ): Promise<ScenarioWritingSession> {
    return fetchApi(`/api/scenarios/write/${sessionId}`, {
      method: 'PUT',
      body: JSON.stringify(payload),
    });
  },

  async listEpisodes(scenarioId: string): Promise<ScenarioEpisode[]> {
    const data = await fetchApi<{ episodes: ScenarioEpisode[] }>(`/api/scenarios/${scenarioId}/episodes`);
    return data.episodes;
  },

  async createEpisode(
    scenarioId: string,
    payload: {
      title: string;
      synopsis_sentence?: string;
      synopsis_paragraph?: string;
      synopsis_full?: string;
      status?: string;
      sort_order?: number;
    },
  ): Promise<ScenarioEpisode> {
    const episode = await fetchApi<ScenarioEpisode>(`/api/scenarios/${scenarioId}/episodes`, {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    await applyRemoteScenarioEpisodes([episode]);
    return episode;
  },

  async updateEpisode(
    episodeId: string,
    payload: Partial<{
      title: string;
      synopsis_sentence: string;
      synopsis_paragraph: string;
      synopsis_full: string;
      status: string;
      sort_order: number;
    }>,
  ): Promise<ScenarioEpisode> {
    const episode = await fetchApi<ScenarioEpisode>(`/api/scenarios/episodes/${episodeId}`, {
      method: 'PUT',
      body: JSON.stringify(payload),
    });
    await applyRemoteScenarioEpisodes([episode]);
    return episode;
  },

  async deleteEpisode(episodeId: string): Promise<void> {
    await fetchApi(`/api/scenarios/episodes/${episodeId}`, { method: 'DELETE' });
  },

  async createCharacter(
    scenarioId: string,
    payload: {
      name: string;
      role?: string;
      description?: string;
    },
  ): Promise<ScenarioCharacter> {
    const character = await fetchApi<ScenarioCharacter>(`/api/scenarios/${scenarioId}/characters`, {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    await applyRemoteScenarioCharacters([character]);
    return character;
  },

  async updateCharacter(
    scenarioId: string,
    characterId: string,
    payload: Partial<{
      name: string;
      role: string;
      description: string;
    }>,
  ): Promise<ScenarioCharacter> {
    const character = await fetchApi<ScenarioCharacter>(`/api/scenarios/${scenarioId}/characters/${characterId}`, {
      method: 'PUT',
      body: JSON.stringify(payload),
    });
    await applyRemoteScenarioCharacters([character]);
    return character;
  },

  async deleteCharacter(scenarioId: string, characterId: string): Promise<void> {
    await fetchApi(`/api/scenarios/${scenarioId}/characters/${characterId}`, {
      method: 'DELETE',
    });
  },

  async createScene(
    scenarioId: string,
    payload: {
      title: string;
      description?: string;
      scene_type?: string;
      gm_instructions?: string;
      image_prompt?: string;
      sort_order?: number;
    },
  ): Promise<ScenarioScene> {
    const scene = await fetchApi<ScenarioScene>(`/api/scenarios/${scenarioId}/scenes`, {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    await applyRemoteScenarioScenes([scene]);
    return scene;
  },

  async updateScene(
    scenarioId: string,
    sceneId: string,
    payload: Partial<{
      title: string;
      description: string;
      scene_type: string;
      gm_instructions: string;
      image_prompt: string;
      status: string;
      sort_order: number;
    }>,
  ): Promise<ScenarioScene> {
    const scene = await fetchApi<ScenarioScene>(`/api/scenarios/${scenarioId}/scenes/${sceneId}`, {
      method: 'PUT',
      body: JSON.stringify(payload),
    });
    await applyRemoteScenarioScenes([scene]);
    return scene;
  },

  async deleteScene(scenarioId: string, sceneId: string): Promise<void> {
    await fetchApi(`/api/scenarios/${scenarioId}/scenes/${sceneId}`, { method: 'DELETE' });
  },

  async listCanonEntries(scenarioId: string): Promise<CanonEntry[]> {
    const data = await fetchApi<{ entries: CanonEntry[] }>(`/api/scenarios/${scenarioId}/canon`);
    return data.entries;
  },

  async createCanonEntry(
    scenarioId: string,
    payload: {
      category: string;
      fact: string;
      source_scene_id?: string | null;
    },
  ): Promise<CanonEntry> {
    return fetchApi(`/api/scenarios/${scenarioId}/canon`, {
      method: 'POST',
      body: JSON.stringify(payload),
    });
  },

  async updateCanonEntry(
    entryId: string,
    payload: Partial<{
      category: string;
      fact: string;
      source_scene_id: string | null;
    }>,
  ): Promise<CanonEntry> {
    return fetchApi(`/api/scenarios/canon/${entryId}`, {
      method: 'PUT',
      body: JSON.stringify(payload),
    });
  },

  async deleteCanonEntry(entryId: string): Promise<void> {
    await fetchApi(`/api/scenarios/canon/${entryId}`, { method: 'DELETE' });
  },
};
