import type { paths } from "@/lib/api-types.gen";

type HttpMethod = "get" | "post" | "put" | "patch" | "delete";
type OperationAt<Path extends string, Method extends HttpMethod> =
  Path extends keyof paths
    ? Method extends keyof paths[Path]
      ? paths[Path][Method]
      : never
    : never;

type JsonBody<Content> = Content extends object
  ? "application/json" extends keyof Content
    ? Content["application/json"]
    : never
  : never;

type RequestBodyAt<Path extends string, Method extends HttpMethod> =
  [OperationAt<Path, Method>] extends [never]
    ? unknown
    : OperationAt<Path, Method> extends { requestBody?: infer RequestBody }
      ? RequestBody extends { content?: infer Content }
        ? JsonBody<Content>
        : never
      : never;

type SuccessfulJsonResponse<Responses> = Responses extends object
  ? {
      [Status in keyof Responses]: Status extends `2${string}`
        ? Responses[Status] extends { content?: infer Content }
          ? JsonBody<Content>
          : never
        : `${Status & (string | number)}` extends `2${string}`
          ? Responses[Status] extends { content?: infer Content }
            ? JsonBody<Content>
            : never
        : never;
    }[keyof Responses]
  : never;

type ResponseBodyAt<Path extends string, Method extends HttpMethod> =
  OperationAt<Path, Method> extends { responses?: infer Responses }
    ? SuccessfulJsonResponse<Responses>
    : unknown;

export type StoryRequestBody<Path extends string, Method extends HttpMethod> = RequestBodyAt<Path, Method>;
export type StoryResponseBody<Path extends string, Method extends HttpMethod> = ResponseBodyAt<Path, Method>;

export class StoryApiError extends Error {
  readonly status: number;
  readonly detail: unknown;

  constructor(status: number, detail: unknown) {
    const message = typeof detail === "object" && detail !== null && "detail" in detail && typeof detail.detail === "string"
      ? detail.detail
      : `Story API エラー (${status})`;
    super(message);
    this.name = "StoryApiError";
    this.status = status;
    this.detail = detail;
  }
}

function proxyUrl(apiPath: string): string {
  return apiPath.startsWith("/api/") ? `/api/python-proxy${apiPath.slice(4)}` : `/api/python-proxy${apiPath}`;
}

function buildUrl(template: string, params: Record<string, string> = {}, query?: Record<string, string | number | undefined>): string {
  let path = template;
  for (const [key, value] of Object.entries(params)) {
    path = path.replace(`{${key}}`, encodeURIComponent(value));
  }
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(query ?? {})) {
    if (value !== undefined) search.set(key, String(value));
  }
  return search.size ? `${path}?${search.toString()}` : path;
}

async function storyRequest<Path extends string, Method extends HttpMethod>(
  schemaPath: Path,
  actualPath: string,
  method: Method,
  body?: RequestBodyAt<Path, Method>,
): Promise<ResponseBodyAt<Path, Method>> {
  const response = await fetch(proxyUrl(actualPath), {
    method: method.toUpperCase(),
    credentials: "include",
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
    cache: "no-store",
  });
  const payload = await response.json().catch(() => null);
  if (!response.ok) throw new StoryApiError(response.status, payload);
  return payload as ResponseBodyAt<Path, Method>;
}

async function storyDownload(schemaPath: "/api/story/works/{work_id}/export", actualPath: string): Promise<Blob> {
  const response = await fetch(proxyUrl(actualPath), { credentials: "include", cache: "no-store" });
  if (!response.ok) throw new StoryApiError(response.status, await response.json().catch(() => null));
  return response.blob();
}

export type StoryRestoreToken = {
  incoming: Array<{
    from_episode_id: string;
    choice_label?: string | null;
    position?: number;
    is_primary?: boolean;
  }>;
  outgoing: Array<{
    to_episode_id: string;
    choice_label?: string | null;
    position?: number;
    is_primary?: boolean;
  }>;
  was_start_episode?: boolean;
  bridged?: Array<{ from_episode_id: string; to_episode_id: string }>;
};

export type StorySearchHit = {
  episode_id: string;
  title: string;
  snippet: string;
  field?: string | null;
  match_start?: number | null;
  match_end?: number | null;
};

export type StorySearchResponse = {
  query: string;
  results: StorySearchHit[];
};

export type StoryRestoreArchivedResponse = {
  id: string;
  archived_at: string | null;
};

export type StoryDeleteEpisodeResponse = {
  id: string;
  archived_at: string | null;
  restore_token: StoryRestoreToken;
};

async function storyRequestUntyped(
  actualPath: string,
  method: HttpMethod,
  body?: unknown,
): Promise<unknown> {
  const response = await fetch(proxyUrl(actualPath), {
    method: method.toUpperCase(),
    credentials: "include",
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
    cache: "no-store",
  });
  const payload = await response.json().catch(() => null);
  if (!response.ok) throw new StoryApiError(response.status, payload);
  return payload;
}

export const storyApi = {
  listWorks: () => storyRequest("/api/story/works", "/api/story/works", "get"),
  createWork: (body: RequestBodyAt<"/api/story/works", "post">) => storyRequest("/api/story/works", "/api/story/works", "post", body),
  getWork: (id: string) => storyRequest("/api/story/works/{work_id}", buildUrl("/api/story/works/{work_id}", { work_id: id }), "get"),
  updateWork: (id: string, body: RequestBodyAt<"/api/story/works/{work_id}", "patch">) => storyRequest("/api/story/works/{work_id}", buildUrl("/api/story/works/{work_id}", { work_id: id }), "patch", body),
  deleteWork: (id: string) => storyRequest("/api/story/works/{work_id}", buildUrl("/api/story/works/{work_id}", { work_id: id }), "delete"),
  getOverview: (id: string) => storyRequest("/api/story/works/{work_id}/overview", buildUrl("/api/story/works/{work_id}/overview", { work_id: id }), "get"),
  searchWork: async (workId: string, q: string): Promise<StorySearchResponse> => {
    const payload = await storyRequestUntyped(
      buildUrl("/api/story/works/{work_id}/search", { work_id: workId }, { q }),
      "get",
    );
    return payload as StorySearchResponse;
  },
  getLegacyScenario: (id: string) => storyRequest("/api/story/legacy/scenarios/{scenario_id}", buildUrl("/api/story/legacy/scenarios/{scenario_id}", { scenario_id: id }), "get"),
  exportWork: (id: string, scope: "route" | "all") => storyDownload("/api/story/works/{work_id}/export", buildUrl("/api/story/works/{work_id}/export", { work_id: id }, { scope, format: "txt" })),

  listEpisodes: (workId: string) => storyRequest("/api/story/works/{work_id}/episodes", buildUrl("/api/story/works/{work_id}/episodes", { work_id: workId }), "get"),
  createEpisode: (workId: string, body: RequestBodyAt<"/api/story/works/{work_id}/episodes", "post">) => storyRequest("/api/story/works/{work_id}/episodes", buildUrl("/api/story/works/{work_id}/episodes", { work_id: workId }), "post", body),
  getEpisode: (id: string) => storyRequest("/api/story/episodes/{episode_id}", buildUrl("/api/story/episodes/{episode_id}", { episode_id: id }), "get"),
  updateEpisode: (id: string, body: RequestBodyAt<"/api/story/episodes/{episode_id}", "patch">) => storyRequest("/api/story/episodes/{episode_id}", buildUrl("/api/story/episodes/{episode_id}", { episode_id: id }), "patch", body),
  updateBody: (id: string, body: RequestBodyAt<"/api/story/episodes/{episode_id}/body", "put">) => storyRequest("/api/story/episodes/{episode_id}/body", buildUrl("/api/story/episodes/{episode_id}/body", { episode_id: id }), "put", body),
  deleteEpisode: async (id: string): Promise<StoryDeleteEpisodeResponse> => {
    const payload = await storyRequestUntyped(
      buildUrl("/api/story/episodes/{episode_id}", { episode_id: id }),
      "delete",
    );
    return payload as StoryDeleteEpisodeResponse;
  },
  restoreArchivedEpisode: async (
    id: string,
    restoreToken?: StoryRestoreToken | null,
  ): Promise<StoryRestoreArchivedResponse> => {
    const payload = await storyRequestUntyped(
      buildUrl("/api/story/episodes/{episode_id}/restore-archived", { episode_id: id }),
      "post",
      restoreToken ? { restore_token: restoreToken } : {},
    );
    return payload as StoryRestoreArchivedResponse;
  },
  splitEpisode: (id: string, body: RequestBodyAt<"/api/story/episodes/{episode_id}/split", "post">) => storyRequest("/api/story/episodes/{episode_id}/split", buildUrl("/api/story/episodes/{episode_id}/split", { episode_id: id }), "post", body),

  getGraph: (workId: string) => storyRequest("/api/story/works/{work_id}/graph", buildUrl("/api/story/works/{work_id}/graph", { work_id: workId }), "get"),
  updateStructure: (workId: string, body: RequestBodyAt<"/api/story/works/{work_id}/structure", "post">) => storyRequest("/api/story/works/{work_id}/structure", buildUrl("/api/story/works/{work_id}/structure", { work_id: workId }), "post", body),

  listRevisions: (episodeId: string) => storyRequest("/api/story/episodes/{episode_id}/revisions", buildUrl("/api/story/episodes/{episode_id}/revisions", { episode_id: episodeId }), "get"),
  getRevision: (episodeId: string, revNo: number) => storyRequest("/api/story/episodes/{episode_id}/revisions/{rev_no}", buildUrl("/api/story/episodes/{episode_id}/revisions/{rev_no}", { episode_id: episodeId, rev_no: String(revNo) }), "get"),
  checkpoint: (episodeId: string, body: RequestBodyAt<"/api/story/episodes/{episode_id}/checkpoint", "post">) => storyRequest("/api/story/episodes/{episode_id}/checkpoint", buildUrl("/api/story/episodes/{episode_id}/checkpoint", { episode_id: episodeId }), "post", body),
  restore: (episodeId: string, body: RequestBodyAt<"/api/story/episodes/{episode_id}/restore", "post">) => storyRequest("/api/story/episodes/{episode_id}/restore", buildUrl("/api/story/episodes/{episode_id}/restore", { episode_id: episodeId }), "post", body),

  listCharacters: () => storyRequest("/api/story/characters", "/api/story/characters", "get"),
  createCharacter: (body: RequestBodyAt<"/api/story/characters", "post">) => storyRequest("/api/story/characters", "/api/story/characters", "post", body),
  getCharacter: (id: string) => storyRequest("/api/story/characters/{character_id}", buildUrl("/api/story/characters/{character_id}", { character_id: id }), "get"),
  updateCharacter: (id: string, body: RequestBodyAt<"/api/story/characters/{character_id}", "patch">) => storyRequest("/api/story/characters/{character_id}", buildUrl("/api/story/characters/{character_id}", { character_id: id }), "patch", body),
  deleteCharacter: (id: string) => storyRequest("/api/story/characters/{character_id}", buildUrl("/api/story/characters/{character_id}", { character_id: id }), "delete"),
  getWorkCharacters: (workId: string) => storyRequest("/api/story/works/{work_id}/characters", buildUrl("/api/story/works/{work_id}/characters", { work_id: workId }), "get"),
  updateWorkCharacters: (workId: string, body: RequestBodyAt<"/api/story/works/{work_id}/characters", "put">) => storyRequest("/api/story/works/{work_id}/characters", buildUrl("/api/story/works/{work_id}/characters", { work_id: workId }), "put", body),

  listRulebooks: () => storyRequest("/api/story/rulebooks", "/api/story/rulebooks", "get"),
  createRulebook: (body: RequestBodyAt<"/api/story/rulebooks", "post">) => storyRequest("/api/story/rulebooks", "/api/story/rulebooks", "post", body),
  getRulebook: (id: string) => storyRequest("/api/story/rulebooks/{rulebook_id}", buildUrl("/api/story/rulebooks/{rulebook_id}", { rulebook_id: id }), "get"),
  updateRulebook: (id: string, body: RequestBodyAt<"/api/story/rulebooks/{rulebook_id}", "patch">) => storyRequest("/api/story/rulebooks/{rulebook_id}", buildUrl("/api/story/rulebooks/{rulebook_id}", { rulebook_id: id }), "patch", body),
  deleteRulebook: (id: string) => storyRequest("/api/story/rulebooks/{rulebook_id}", buildUrl("/api/story/rulebooks/{rulebook_id}", { rulebook_id: id }), "delete"),
  getWorkRulebooks: (workId: string) => storyRequest("/api/story/works/{work_id}/rulebooks", buildUrl("/api/story/works/{work_id}/rulebooks", { work_id: workId }), "get"),
  updateWorkRulebooks: (workId: string, body: RequestBodyAt<"/api/story/works/{work_id}/rulebooks", "put">) => storyRequest("/api/story/works/{work_id}/rulebooks", buildUrl("/api/story/works/{work_id}/rulebooks", { work_id: workId }), "put", body),
  listNotes: (workId: string) => storyRequest("/api/story/works/{work_id}/notes", buildUrl("/api/story/works/{work_id}/notes", { work_id: workId }), "get"),
  createNote: (workId: string, body: RequestBodyAt<"/api/story/works/{work_id}/notes", "post">) => storyRequest("/api/story/works/{work_id}/notes", buildUrl("/api/story/works/{work_id}/notes", { work_id: workId }), "post", body),
  updateNote: (id: string, body: RequestBodyAt<"/api/story/notes/{note_id}", "patch">) => storyRequest("/api/story/notes/{note_id}", buildUrl("/api/story/notes/{note_id}", { note_id: id }), "patch", body),
  deleteNote: (id: string) => storyRequest("/api/story/notes/{note_id}", buildUrl("/api/story/notes/{note_id}", { note_id: id }), "delete"),

  compose: (workId: string, body: RequestBodyAt<"/api/story/works/{work_id}/compose", "post">) => storyRequest("/api/story/works/{work_id}/compose", buildUrl("/api/story/works/{work_id}/compose", { work_id: workId }), "post", body),
  applyCompose: (workId: string, body: RequestBodyAt<"/api/story/works/{work_id}/compose/apply", "post">) => storyRequest("/api/story/works/{work_id}/compose/apply", buildUrl("/api/story/works/{work_id}/compose/apply", { work_id: workId }), "post", body),
  generate: (episodeId: string, body: RequestBodyAt<"/api/story/episodes/{episode_id}/generate", "post">) => storyRequest("/api/story/episodes/{episode_id}/generate", buildUrl("/api/story/episodes/{episode_id}/generate", { episode_id: episodeId }), "post", body),
  revise: (episodeId: string, body: RequestBodyAt<"/api/story/episodes/{episode_id}/revise", "post">) => storyRequest("/api/story/episodes/{episode_id}/revise", buildUrl("/api/story/episodes/{episode_id}/revise", { episode_id: episodeId }), "post", body),
  batchGenerate: (workId: string, body: RequestBodyAt<"/api/story/works/{work_id}/batch-generate", "post">) => storyRequest("/api/story/works/{work_id}/batch-generate", buildUrl("/api/story/works/{work_id}/batch-generate", { work_id: workId }), "post", body),
  contextPreview: (episodeId: string) => storyRequest("/api/story/episodes/{episode_id}/context-preview", buildUrl("/api/story/episodes/{episode_id}/context-preview", { episode_id: episodeId }), "get"),
  getJob: (jobId: string) => storyRequest("/api/story/jobs/{job_id}", buildUrl("/api/story/jobs/{job_id}", { job_id: jobId }), "get"),
  cancelJob: (jobId: string) => storyRequest("/api/story/jobs/{job_id}/cancel", buildUrl("/api/story/jobs/{job_id}/cancel", { job_id: jobId }), "post"),
  resumeJob: (jobId: string) => storyRequest("/api/story/jobs/{job_id}/resume", buildUrl("/api/story/jobs/{job_id}/resume", { job_id: jobId }), "post"),
  listIllustrations: async (episodeId: string) => storyRequestUntyped(buildUrl("/api/story/episodes/{episode_id}/illustrations", { episode_id: episodeId }), "get"),
  generateIllustrations: async (episodeId: string) => storyRequestUntyped(buildUrl("/api/story/episodes/{episode_id}/illustrations/generate", { episode_id: episodeId }), "post", {}),
  regenerateIllustration: async (illustrationId: string) => storyRequestUntyped(buildUrl("/api/story/illustrations/{illustration_id}/regenerate", { illustration_id: illustrationId }), "post", {}),
  deleteIllustration: async (illustrationId: string) => storyRequestUntyped(buildUrl("/api/story/illustrations/{illustration_id}", { illustration_id: illustrationId }), "delete"),
  getWritingSessionByConversation: (conversationSessionId: string) => storyRequest("/api/story/writing-sessions/by-conversation/{conversation_session_id}", buildUrl("/api/story/writing-sessions/by-conversation/{conversation_session_id}", { conversation_session_id: conversationSessionId }), "get"),
  updateWritingSession: (writingSessionId: string, body: RequestBodyAt<"/api/story/writing-sessions/{writing_session_id}", "patch">) => storyRequest("/api/story/writing-sessions/{writing_session_id}", buildUrl("/api/story/writing-sessions/{writing_session_id}", { writing_session_id: writingSessionId }), "patch", body),
  startWriting: (workId: string, body: RequestBodyAt<"/api/story/works/{work_id}/write", "post">) => storyRequest("/api/story/works/{work_id}/write", buildUrl("/api/story/works/{work_id}/write", { work_id: workId }), "post", body),
};
