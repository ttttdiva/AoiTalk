/** Canonical Story Studio client; legacy Scenario endpoints are not used. */

import {
  ApiHttpError,
  fetchApi,
  fetchApiText,
  isApiConnectionError,
  isApiHttpError,
} from "./api-client";
import {
  applyRemoteStoryCharacters,
  applyRemoteStoryEpisodes,
  applyRemoteStoryGraph,
  applyRemoteStoryOverview,
  applyRemoteStoryJob,
  applyRemoteStoryNotes,
  applyRemoteStoryRevisions,
  applyRemoteStoryRulebooks,
  applyRemoteStoryWorks,
  applyRemoteStoryWritingSession,
  applyStoryBodyToCache,
  clearStoryLocalDraft,
  markStoryEpisodeArchived,
  markStoryCharacterArchived,
  markStoryRulebookArchived,
  markStoryWorkArchived,
  migrateLegacyStoryWritingDraft,
  removeStoryNote,
  replaceRemoteStoryCharacters,
  replaceRemoteStoryNotes,
  replaceRemoteStoryRulebooks,
  replaceRemoteStoryWorkCharacters,
  replaceRemoteStoryWorkRulebooksForWork,
  replaceRemoteStoryWorks,
  replaceRemoteStoryWorkRulebooks,
  saveStoryLocalDraft,
  storyRepo,
} from "../repositories/story";
import type {
  StoryBodyUpdateResponse,
  StoryCharacter,
  StoryContextPreview,
  StoryEpisode,
  StoryEpisodeRevision,
  StoryGraph,
  StoryJob,
  StoryNote,
  StoryOverview,
  StoryRevisionList,
  StoryRulebook,
  StorySearchResponse,
  StorySplitResponse,
  StoryStructureOperation,
  StoryWork,
  StoryWorkRulebook,
  StoryWritingSession,
} from "../types/api";

export interface StoryWorkCreatePayload {
  title: string;
  synopsis?: string | null;
  plot?: string | null;
  style_guide?: string | null;
  kind?: "novel" | "trpg";
  status?: string;
  target_episode_chars?: number;
  planned_episode_count?: number | null;
  ui_state?: Record<string, unknown>;
  model_override?: Record<string, unknown>;
  image_settings?: Record<string, unknown> | null;
}

export type StoryWorkPatchPayload = Partial<
  Omit<StoryWorkCreatePayload, "kind">
> & {
  start_episode_id?: string | null;
};

export interface StoryEpisodeCreatePayload {
  title: string;
  plot?: string | null;
  body?: string;
  summary?: string | null;
  premise_note?: string | null;
  status?: string;
  target_chars?: number | null;
  sort_hint?: number;
  after_episode_id?: string | null;
  choice_label?: string | null;
}

export type StoryEpisodePatchPayload = Partial<{
  title: string;
  plot: string | null;
  summary: string | null;
  premise_note: string | null;
  status: string;
  target_chars: number | null;
  map_x: number | null;
  map_y: number | null;
  sort_hint: number;
}>;

export interface StoryBodyUpdatePayload {
  body: string;
  expected_etag: string;
  commit?: boolean;
  message?: string | null;
  origin?: string;
  created_by?: string;
}

export interface StoryCharacterPayload {
  name: string;
  aliases?: string[];
  summary?: string | null;
  description?: string | null;
  notes?: string | null;
  ai_mode?: string;
  keywords?: string[];
}

export type StoryCharacterPatchPayload = Partial<StoryCharacterPayload> & {
  image_path?: string | null;
};

export interface StoryRulebookPayload {
  name: string;
  content?: string | null;
}

export interface StoryNotePayload {
  title: string;
  content?: string | null;
  ai_mode?: string;
  keywords?: string[];
  position?: number;
}

export interface StoryJobPayload {
  episode_id?: string | null;
  episode_ids?: string[];
  episode_count?: number | null;
  instruction?: string | null;
  model?: Record<string, unknown> | null;
  mode?: string | null;
}

export interface StoryRevisionRestoreResponse {
  episode: StoryEpisode;
  pre_restore: StoryEpisodeRevision;
  restore: StoryEpisodeRevision;
}

export interface StoryStructureResponse extends StoryGraph {
  results: Array<Record<string, unknown>>;
}

export interface StoryComposeApplyResponse {
  episodes: StoryEpisode[];
  graph: StoryGraph | StoryStructureResponse;
}

/** Association request DTOs intentionally omit the server-added work_id. */
export interface StoryWorkCharacterInput {
  character_id: string;
  role_note?: string | null;
  position?: number;
}
export interface StoryWorkRulebookInput {
  rulebook_id: string;
  enabled?: boolean;
  position?: number;
}

export interface StoryDeleteResponse {
  id: string;
  archived_at?: string | null;
  deleted?: boolean | null;
  restore_token?: Record<string, unknown> | null;
}

export class StoryBodyConflictError extends ApiHttpError {
  readonly episodeId: string;
  readonly currentEtag: string | null;
  readonly serverSnapshot: StoryEpisode | null;

  constructor(
    episodeId: string,
    cause: ApiHttpError,
    serverSnapshot: StoryEpisode | null,
  ) {
    super(cause.status, cause.responseBody, cause.message);
    this.name = "StoryBodyConflictError";
    this.episodeId = episodeId;
    this.currentEtag = parseConflictCurrentEtag(cause.responseBody);
    this.serverSnapshot = serverSnapshot;
  }
}

function parseConflictCurrentEtag(body: string): string | null {
  try {
    const parsed = JSON.parse(body) as { detail?: { current_etag?: unknown } };
    const value = parsed.detail?.current_etag;
    return typeof value === "string" ? value : null;
  } catch {
    return null;
  }
}

async function cacheWork(item: StoryWork): Promise<StoryWork> {
  await applyRemoteStoryWorks([item]);
  return item;
}

async function cacheEpisode(item: StoryEpisode): Promise<StoryEpisode> {
  await applyRemoteStoryEpisodes([item]);
  return item;
}

async function cacheCharacter(item: StoryCharacter): Promise<StoryCharacter> {
  await applyRemoteStoryCharacters([item]);
  return item;
}

async function cacheRulebook(item: StoryRulebook): Promise<StoryRulebook> {
  await applyRemoteStoryRulebooks([item]);
  return item;
}

async function cacheNote(item: StoryNote): Promise<StoryNote> {
  await applyRemoteStoryNotes([item]);
  return item;
}

async function cacheJob(item: StoryJob): Promise<StoryJob> {
  await applyRemoteStoryJob(item);
  return item;
}

async function migrateLegacyStoryDraftBestEffort(workId: string): Promise<void> {
  await migrateLegacyStoryWritingDraft(workId).catch(() => undefined);
}

export const storyApi = {
  // ---------- Work ----------
  async listWorks(): Promise<StoryWork[]> {
    try {
      const items = await fetchApi<StoryWork[]>("/api/story/works");
      await replaceRemoteStoryWorks(items);
      // Migration is deliberately best-effort per work.  A failed or
      // wrong-auth copy keeps the legacy key, so one stale account/key cannot
      // prevent the canonical work list from loading.
      await Promise.all(
        items.map((item) => migrateLegacyStoryDraftBestEffort(item.id)),
      );
      return items;
    } catch (error) {
      if (isApiConnectionError(error)) return storyRepo.listWorks();
      throw error;
    }
  },

  async getWork(workId: string): Promise<StoryWork> {
    try {
      return cacheWork(await fetchApi<StoryWork>(`/api/story/works/${workId}`));
    } catch (error) {
      if (isApiConnectionError(error)) {
        const local = await storyRepo.getWork(workId);
        if (local) return local;
      }
      throw error;
    }
  },

  async createWork(payload: StoryWorkCreatePayload): Promise<StoryWork> {
    return cacheWork(
      await fetchApi<StoryWork>("/api/story/works", {
        method: "POST",
        body: JSON.stringify(payload),
      }),
    );
  },

  async updateWork(workId: string, payload: StoryWorkPatchPayload): Promise<StoryWork> {
    return cacheWork(
      await fetchApi<StoryWork>(`/api/story/works/${workId}`, {
        method: "PATCH",
        body: JSON.stringify(payload),
      }),
    );
  },

  async archiveWork(workId: string): Promise<StoryDeleteResponse> {
    const result = await fetchApi<StoryDeleteResponse>(`/api/story/works/${workId}`, {
      method: "DELETE",
    });
    await markStoryWorkArchived(workId, result.archived_at ?? null);
    return result;
  },

  async getOverview(workId: string): Promise<StoryOverview> {
    try {
      const result = await fetchApi<StoryOverview>(`/api/story/works/${workId}/overview`);
      await applyRemoteStoryOverview(result);
      await migrateLegacyStoryDraftBestEffort(workId);
      return result;
    } catch (error) {
      if (isApiConnectionError(error)) {
        const local = await storyRepo.getOverview(workId);
        if (local) return local;
      }
      throw error;
    }
  },

  /** Search episode metadata/manuscripts through the canonical Story route. */
  async searchWork(workId: string, query: string): Promise<StorySearchResponse> {
    return fetchApi<StorySearchResponse>(
      `/api/story/works/${workId}/search?q=${encodeURIComponent(query)}`,
    );
  },

  /** Export the selected route or every episode as canonical text. */
  async exportWork(workId: string, scope: "route" | "all" = "route"): Promise<string> {
    return fetchApiText(
      `/api/story/works/${workId}/export?scope=${scope}&format=txt`,
    );
  },

  // ---------- Episodes / manuscript ----------
  async listEpisodes(workId: string): Promise<StoryEpisode[]> {
    try {
      const items = await fetchApi<StoryEpisode[]>(`/api/story/works/${workId}/episodes`);
      await applyRemoteStoryEpisodes(items);
      await migrateLegacyStoryDraftBestEffort(workId);
      return items;
    } catch (error) {
      if (isApiConnectionError(error)) return storyRepo.listEpisodes(workId);
      throw error;
    }
  },

  async createEpisode(workId: string, payload: StoryEpisodeCreatePayload): Promise<StoryEpisode> {
    return cacheEpisode(
      await fetchApi<StoryEpisode>(`/api/story/works/${workId}/episodes`, {
        method: "POST",
        body: JSON.stringify(payload),
      }),
    );
  },

  async getEpisode(episodeId: string): Promise<StoryEpisode> {
    try {
      return cacheEpisode(await fetchApi<StoryEpisode>(`/api/story/episodes/${episodeId}`));
    } catch (error) {
      if (isApiConnectionError(error)) {
        const local = await storyRepo.getEpisode(episodeId);
        if (local) return local;
      }
      throw error;
    }
  },

  async updateEpisode(
    episodeId: string,
    payload: StoryEpisodePatchPayload,
  ): Promise<StoryEpisode> {
    return cacheEpisode(
      await fetchApi<StoryEpisode>(`/api/story/episodes/${episodeId}`, {
        method: "PATCH",
        body: JSON.stringify(payload),
      }),
    );
  },

  async splitEpisode(
    episodeId: string,
    payload: { offset: number; new_title: string; expected_etag: string },
  ): Promise<StorySplitResponse> {
    return fetchApi<StorySplitResponse>(`/api/story/episodes/${episodeId}/split`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  async regenerateSummary(episodeId: string): Promise<StoryEpisode> {
    return cacheEpisode(
      await fetchApi<StoryEpisode>(`/api/story/episodes/${episodeId}/summary/regenerate`, {
        method: "POST",
      }),
    );
  },

  async updateEpisodeBody(
    episodeId: string,
    payload: StoryBodyUpdatePayload,
  ): Promise<StoryBodyUpdateResponse> {
    // Save before the request so timeout/offline errors never lose the local
    // manuscript.  A successful canonical commit clears this durable draft.
    await saveStoryLocalDraft({
      episodeId,
      body: payload.body,
      expectedEtag: payload.expected_etag,
    });
    try {
      const result = await fetchApi<StoryBodyUpdateResponse>(
        `/api/story/episodes/${episodeId}/body`,
        {
          method: "PUT",
          body: JSON.stringify(payload),
        },
      );
      await applyStoryBodyToCache(
        episodeId,
        payload.body,
        result.body_etag,
        result.char_count,
        result.current_rev_no,
      );
      if (result.revision) await applyRemoteStoryRevisions([result.revision]);
      if (result.pre_revision) await applyRemoteStoryRevisions([result.pre_revision]);
      await clearStoryLocalDraft(episodeId);
      return result;
    } catch (error) {
      if (isApiHttpError(error) && error.status === 409) {
        // The conflict detail has metadata only.  Fetch the canonical episode
        // body separately when reachable; if that GET fails, the draft still
        // remains durable with the prior cache and etag.
        let serverSnapshot: StoryEpisode | null = null;
        try {
          serverSnapshot = await this.getEpisode(episodeId);
        } catch {
          serverSnapshot = await storyRepo.getEpisode(episodeId);
        }
        await saveStoryLocalDraft({
          episodeId,
          body: payload.body,
          expectedEtag: payload.expected_etag,
          ...(serverSnapshot ? { serverSnapshot } : {}),
          conflict: true,
        });
        if (error instanceof ApiHttpError) {
          throw new StoryBodyConflictError(episodeId, error, serverSnapshot);
        }
      }
      throw error;
    }
  },

  async deleteEpisode(episodeId: string): Promise<StoryDeleteResponse> {
    const result = await fetchApi<StoryDeleteResponse>(`/api/story/episodes/${episodeId}`, {
      method: "DELETE",
    });
    await markStoryEpisodeArchived(episodeId, result.archived_at ?? null);
    return result;
  },

  async restoreEpisode(
    episodeId: string,
    restoreToken?: Record<string, unknown> | null,
  ): Promise<StoryEpisode> {
    await fetchApi<{ id: string; archived_at: string | null }>(
      `/api/story/episodes/${episodeId}/restore-archived`,
      {
        method: "POST",
        body: JSON.stringify({ restore_token: restoreToken ?? null }),
      },
    );
    return this.getEpisode(episodeId);
  },

  async getGraph(workId: string): Promise<StoryGraph> {
    try {
      const result = await fetchApi<StoryGraph>(`/api/story/works/${workId}/graph`);
      await applyRemoteStoryGraph(result, undefined, workId);
      await migrateLegacyStoryDraftBestEffort(workId);
      return result;
    } catch (error) {
      if (isApiConnectionError(error)) {
        const local = await storyRepo.getGraph(workId);
        if (local) return local;
      }
      throw error;
    }
  },

  async applyStructure(
    workId: string,
    ops: Array<StoryStructureOperation | Record<string, unknown>>,
  ): Promise<StoryStructureResponse> {
    const result = await fetchApi<StoryStructureResponse>(`/api/story/works/${workId}/structure`, {
      method: "POST",
      body: JSON.stringify({ ops }),
    });
    await applyRemoteStoryGraph(result, undefined, workId);
    return result;
  },

  /** Naming aligned with the Web Story client; retains applyStructure alias. */
  async updateStructure(
    workId: string,
    input: Array<StoryStructureOperation | Record<string, unknown>> | { ops: Array<StoryStructureOperation | Record<string, unknown>> },
  ): Promise<StoryStructureResponse> {
    return this.applyStructure(workId, Array.isArray(input) ? input : input.ops);
  },

  async removeLink(workId: string, linkId: string): Promise<StoryStructureResponse> {
    return this.applyStructure(workId, [{ op: "remove_link", id: linkId }]);
  },

  async updateLink(
    workId: string,
    linkId: string,
    patch: { choice_label?: string | null; position?: number; is_primary?: boolean },
  ): Promise<StoryStructureResponse> {
    return this.applyStructure(workId, [{ op: "update_link", id: linkId, ...patch }]);
  },

  async insertEpisodeBetween(
    workId: string,
    linkId: string,
    episodeId: string,
  ): Promise<StoryStructureResponse> {
    return this.applyStructure(workId, [{ op: "insert_between", link_id: linkId, episode_id: episodeId }]);
  },

  async reorderEpisodes(workId: string, episodeIds: string[]): Promise<StoryStructureResponse> {
    return this.applyStructure(workId, [{ op: "reorder_linear", episode_ids: episodeIds }]);
  },

  async duplicateAsBranch(
    workId: string,
    episodeId: string,
    payload: { choice_label?: string | null; new_title?: string | null } = {},
  ): Promise<StoryStructureResponse> {
    return this.applyStructure(workId, [{ op: "duplicate_as_branch", episode_id: episodeId, ...payload }]);
  },

  // ---------- History ----------
  async listRevisions(
    episodeId: string,
    limit = 50,
    offset = 0,
  ): Promise<StoryRevisionList> {
    try {
      const result = await fetchApi<StoryRevisionList>(
        `/api/story/episodes/${episodeId}/revisions?limit=${limit}&offset=${offset}`,
      );
      await applyRemoteStoryRevisions(result.items);
      return result;
    } catch (error) {
      if (isApiConnectionError(error)) {
        const items = await storyRepo.listRevisions(episodeId);
        return { items: items.slice(offset, offset + limit), limit, offset };
      }
      throw error;
    }
  },

  async getRevision(episodeId: string, revNo: number): Promise<StoryEpisodeRevision> {
    try {
      const result = await fetchApi<StoryEpisodeRevision>(
        `/api/story/episodes/${episodeId}/revisions/${revNo}`,
      );
      await applyRemoteStoryRevisions([result]);
      return result;
    } catch (error) {
      if (isApiConnectionError(error)) {
        const local = await storyRepo.getRevision(episodeId, revNo);
        if (local) return local;
      }
      throw error;
    }
  },

  async checkpoint(episodeId: string, message: string): Promise<StoryEpisodeRevision> {
    const result = await fetchApi<StoryEpisodeRevision>(`/api/story/episodes/${episodeId}/checkpoint`, {
      method: "POST",
      body: JSON.stringify({ message }),
    });
    await applyRemoteStoryRevisions([result]);
    return result;
  },

  async restoreRevision(
    episodeId: string,
    revNo: number,
  ): Promise<StoryRevisionRestoreResponse> {
    const result = await fetchApi<StoryRevisionRestoreResponse>(`/api/story/episodes/${episodeId}/restore`, {
      method: "POST",
      body: JSON.stringify({ rev_no: revNo }),
    });
    await applyRemoteStoryEpisodes([result.episode]);
    await applyRemoteStoryRevisions([result.pre_restore, result.restore]);
    return result;
  },

  // ---------- Cast / rules / notes ----------
  async listCharacters(): Promise<StoryCharacter[]> {
    try {
      const result = await fetchApi<StoryCharacter[]>("/api/story/characters");
      await replaceRemoteStoryCharacters(result);
      return result;
    } catch (error) {
      if (isApiConnectionError(error)) return storyRepo.listCharacters();
      throw error;
    }
  },

  async createCharacter(payload: StoryCharacterPayload): Promise<StoryCharacter> {
    return cacheCharacter(
      await fetchApi<StoryCharacter>("/api/story/characters", {
        method: "POST",
        body: JSON.stringify(payload),
      }),
    );
  },

  async getCharacter(characterId: string): Promise<StoryCharacter> {
    try {
      return cacheCharacter(await fetchApi<StoryCharacter>(`/api/story/characters/${characterId}`));
    } catch (error) {
      if (isApiConnectionError(error)) {
        const local = await storyRepo.getCharacter(characterId);
        if (local) return local;
      }
      throw error;
    }
  },

  async updateCharacter(
    characterId: string,
    payload: StoryCharacterPatchPayload,
  ): Promise<StoryCharacter> {
    return cacheCharacter(
      await fetchApi<StoryCharacter>(`/api/story/characters/${characterId}`, {
        method: "PATCH",
        body: JSON.stringify(payload),
      }),
    );
  },

  async archiveCharacter(characterId: string): Promise<StoryDeleteResponse> {
    const result = await fetchApi<StoryDeleteResponse>(`/api/story/characters/${characterId}`, {
      method: "DELETE",
    });
    await markStoryCharacterArchived(characterId, result.archived_at ?? null);
    return result;
  },

  async listWorkCharacters(workId: string): Promise<StoryCharacter[]> {
    try {
      const result = await fetchApi<StoryCharacter[]>(`/api/story/works/${workId}/characters`);
      await replaceRemoteStoryWorkCharacters(workId, result);
      return result;
    } catch (error) {
      if (isApiConnectionError(error)) return storyRepo.listWorkCharacters(workId);
      throw error;
    }
  },

  async replaceWorkCharacters(
    workId: string,
    payload: StoryWorkCharacterInput[],
  ): Promise<StoryCharacter[]> {
    const result = await fetchApi<StoryCharacter[]>(`/api/story/works/${workId}/characters`, {
      method: "PUT",
      body: JSON.stringify(payload),
    });
    await replaceRemoteStoryWorkCharacters(workId, result);
    return result;
  },

  async listRulebooks(): Promise<StoryRulebook[]> {
    try {
      const result = await fetchApi<StoryRulebook[]>("/api/story/rulebooks");
      await replaceRemoteStoryRulebooks(result);
      return result;
    } catch (error) {
      if (isApiConnectionError(error)) return storyRepo.listRulebooks();
      throw error;
    }
  },

  async createRulebook(payload: StoryRulebookPayload): Promise<StoryRulebook> {
    return cacheRulebook(
      await fetchApi<StoryRulebook>("/api/story/rulebooks", {
        method: "POST",
        body: JSON.stringify(payload),
      }),
    );
  },

  async getRulebook(rulebookId: string): Promise<StoryRulebook> {
    try {
      return cacheRulebook(await fetchApi<StoryRulebook>(`/api/story/rulebooks/${rulebookId}`));
    } catch (error) {
      if (isApiConnectionError(error)) {
        const local = await storyRepo.getRulebook(rulebookId);
        if (local) return local;
      }
      throw error;
    }
  },

  async updateRulebook(
    rulebookId: string,
    payload: Partial<StoryRulebookPayload>,
  ): Promise<StoryRulebook> {
    return cacheRulebook(
      await fetchApi<StoryRulebook>(`/api/story/rulebooks/${rulebookId}`, {
        method: "PATCH",
        body: JSON.stringify(payload),
      }),
    );
  },

  async archiveRulebook(rulebookId: string): Promise<StoryDeleteResponse> {
    const result = await fetchApi<StoryDeleteResponse>(`/api/story/rulebooks/${rulebookId}`, {
      method: "DELETE",
    });
    await markStoryRulebookArchived(rulebookId, result.archived_at ?? null);
    return result;
  },

  async listWorkRulebooks(workId: string): Promise<StoryRulebook[]> {
    try {
      const result = await fetchApi<StoryRulebook[]>(`/api/story/works/${workId}/rulebooks`);
      await replaceRemoteStoryWorkRulebooksForWork(workId, result);
      return result;
    } catch (error) {
      if (isApiConnectionError(error)) return storyRepo.listWorkRulebooks(workId);
      throw error;
    }
  },

  async replaceWorkRulebooks(
    workId: string,
    payload: StoryWorkRulebookInput[],
  ): Promise<StoryWorkRulebook[]> {
    const result = await fetchApi<StoryWorkRulebook[]>(`/api/story/works/${workId}/rulebooks`, {
      method: "PUT",
      body: JSON.stringify(payload),
    });
    await replaceRemoteStoryWorkRulebooks(workId, result);
    return result;
  },

  async listNotes(workId: string): Promise<StoryNote[]> {
    try {
      const result = await fetchApi<StoryNote[]>(`/api/story/works/${workId}/notes`);
      await replaceRemoteStoryNotes(workId, result);
      return result;
    } catch (error) {
      if (isApiConnectionError(error)) return storyRepo.listNotes(workId);
      throw error;
    }
  },

  async createNote(workId: string, payload: StoryNotePayload): Promise<StoryNote> {
    return cacheNote(
      await fetchApi<StoryNote>(`/api/story/works/${workId}/notes`, {
        method: "POST",
        body: JSON.stringify(payload),
      }),
    );
  },

  async updateNote(noteId: string, payload: Partial<StoryNotePayload>): Promise<StoryNote> {
    return cacheNote(
      await fetchApi<StoryNote>(`/api/story/notes/${noteId}`, {
        method: "PATCH",
        body: JSON.stringify(payload),
      }),
    );
  },

  async deleteNote(noteId: string): Promise<StoryDeleteResponse> {
    const result = await fetchApi<StoryDeleteResponse>(`/api/story/notes/${noteId}`, {
      method: "DELETE",
    });
    await removeStoryNote(noteId);
    return result;
  },

  async contextPreview(episodeId: string): Promise<StoryContextPreview> {
    return fetchApi<StoryContextPreview>(`/api/story/episodes/${episodeId}/context-preview`);
  },

  // ---------- Generation jobs ----------
  async compose(workId: string, payload: StoryJobPayload = {}): Promise<StoryJob> {
    return cacheJob(
      await fetchApi<StoryJob>(`/api/story/works/${workId}/compose`, {
        method: "POST",
        body: JSON.stringify(payload),
      }),
    );
  },

  async composeApply(
    workId: string,
    payload: {
      episodes: Array<Record<string, unknown>>;
      links: Array<Record<string, unknown>>;
    },
  ): Promise<StoryComposeApplyResponse> {
    const result = await fetchApi<StoryComposeApplyResponse>(
      `/api/story/works/${workId}/compose/apply`,
      {
        method: "POST",
        body: JSON.stringify(payload),
      },
    );
    await applyRemoteStoryEpisodes(result.episodes);
    await applyRemoteStoryGraph(result.graph, undefined, workId);
    return result;
  },

  async generate(episodeId: string, payload: StoryJobPayload = {}): Promise<StoryJob> {
    return cacheJob(
      await fetchApi<StoryJob>(`/api/story/episodes/${episodeId}/generate`, {
        method: "POST",
        body: JSON.stringify(payload),
      }),
    );
  },

  async revise(episodeId: string, payload: StoryJobPayload = {}): Promise<StoryJob> {
    return cacheJob(
      await fetchApi<StoryJob>(`/api/story/episodes/${episodeId}/revise`, {
        method: "POST",
        body: JSON.stringify(payload),
      }),
    );
  },

  async batchGenerate(workId: string, payload: StoryJobPayload = {}): Promise<StoryJob> {
    return cacheJob(
      await fetchApi<StoryJob>(`/api/story/works/${workId}/batch-generate`, {
        method: "POST",
        body: JSON.stringify(payload),
      }),
    );
  },

  async getJob(jobId: string): Promise<StoryJob> {
    try {
      return cacheJob(await fetchApi<StoryJob>(`/api/story/jobs/${jobId}`));
    } catch (error) {
      if (isApiConnectionError(error)) {
        const local = await storyRepo.getJob(jobId);
        if (local) return local;
      }
      throw error;
    }
  },

  async cancelJob(jobId: string): Promise<StoryJob> {
    return cacheJob(
      await fetchApi<StoryJob>(`/api/story/jobs/${jobId}/cancel`, { method: "POST" }),
    );
  },

  async resumeJob(jobId: string): Promise<StoryJob> {
    return cacheJob(
      await fetchApi<StoryJob>(`/api/story/jobs/${jobId}/resume`, { method: "POST" }),
    );
  },

  // ---------- Canonical writing session ----------
  async getWritingSessionByConversation(
    conversationSessionId: string,
  ): Promise<StoryWritingSession | null> {
    try {
      const result = await fetchApi<StoryWritingSession | null>(
        `/api/story/writing-sessions/by-conversation/${conversationSessionId}`,
      );
      if (result) await applyRemoteStoryWritingSession(result);
      return result;
    } catch (error) {
      if (isApiConnectionError(error)) {
        return storyRepo.getWritingSessionByConversation(conversationSessionId);
      }
      throw error;
    }
  },

  async startWriting(
    workId: string,
    payload: {
      episode_id?: string | null;
      conversation_session_id?: string | null;
    } = {},
  ): Promise<StoryWritingSession> {
    const result = await fetchApi<StoryWritingSession>(`/api/story/works/${workId}/write`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
    await applyRemoteStoryWritingSession(result);
    return result;
  },

  async updateWritingSession(
    writingSessionId: string,
    payload: { episode_id?: string | null },
  ): Promise<StoryWritingSession> {
    const result = await fetchApi<StoryWritingSession>(
      `/api/story/writing-sessions/${writingSessionId}`,
      {
        method: "PATCH",
        body: JSON.stringify(payload),
      },
    );
    await applyRemoteStoryWritingSession(result);
    return result;
  },
};

export default storyApi;
