export type StoryStatus = "unwritten" | "draft" | "revising" | "done" | "on_hold";

export type StoryWorkView = {
  id: string;
  title: string;
  kind: string;
  status: string;
  synopsis: string;
  plot: string;
  styleGuide: string;
  plannedEpisodeCount: number | null;
  targetEpisodeChars: number;
  modelOverride: Record<string, unknown>;
  resolvedModel?: string | null;
  resolvedModelLayer?: string | null;
  startEpisodeId: string | null;
  currentRoute: Record<string, string>;
  episodeCount: number;
  totalChars: number;
  notesCount: number;
  charactersCount: number;
  rulebooksCount: number;
  branchCount: number;
  updatedAt: string | null;
  imageSettings: StoryImageSettings;
};

export type StoryImageSettings = {
  enabled: boolean;
  engine: string;
  maxImagesPerEpisode: number;
  workflowPath: string | null;
  style: string;
  negativePrompt: string;
};

export type StoryIllustrationView = {
  id: string;
  workId: string;
  episodeId: string;
  bodyEtag: string;
  anchorQuote: string;
  ordering: number;
  sceneDescription: string;
  visualPrompt: string;
  status: string;
  generatedMediaId: string | null;
  errorMessage: string | null;
  resolvedIndex: number | null;
  stale: boolean;
  imageUrl: string | null;
};

export type StoryIllustrationListView = {
  active: StoryIllustrationView[];
  stale: StoryIllustrationView[];
};

export type StoryEpisodeView = {
  id: string;
  workId?: string | null;
  title: string;
  plot: string;
  body: string;
  summary: string;
  premiseNote: string;
  status: StoryStatus;
  targetChars: number;
  charCount: number;
  bodyEtag: string;
  currentRevNo: number;
  mapX: number;
  mapY: number;
  unplaced: boolean;
};

export type StoryLinkView = {
  id: string;
  from: string;
  to: string;
  choiceLabel: string;
  isPrimary: boolean;
  position: number;
};

export type StoryGraphView = {
  episodes: StoryEpisodeView[];
  links: StoryLinkView[];
  startEpisodeId: string | null;
};

export type StoryCharacterView = {
  id: string;
  name: string;
  aliases: string[];
  summary: string;
  description: string;
  notes: string;
  aiMode: string;
  keywords: string[];
  roleNote: string;
  position: number;
  included: boolean;
  imagePath: string;
  imageUrl: string | null;
};

export type StoryRulebookView = {
  id: string;
  name: string;
  content: string;
  enabled: boolean;
  position: number;
  applied: boolean;
};

export type StoryNoteView = {
  id: string;
  title: string;
  content: string;
  aiMode: string;
  keywords: string[];
  position: number;
};

export type StoryRevisionView = {
  id: string;
  revNo: number;
  origin: string;
  message: string;
  createdAt: string | null;
  charCount: number;
  body?: string;
};

export type StoryContextView = {
  prompt: string;
  injected: Array<{ kind?: string; label?: string; name?: string; title?: string; [key: string]: unknown }>;
  provider: string | null;
  model: string | null;
  layer: string | null;
  resolvedModel: string | null;
  modelLayer: string | null;
};

export type StoryJobView = {
  id: string;
  status: string;
  message?: string | null;
  error?: string | null;
  result?: Record<string, unknown> | null;
  progress?: Record<string, unknown>;
  items?: Array<{ id: string; title?: string; status: string; error?: string | null }>;
};

export type StoryLegacyCharacterView = {
  id: string;
  name: string;
  role?: string;
  description?: string;
};

export type StoryLegacyScenarioView = {
  id: string;
  title: string;
  description: string;
  characters: StoryLegacyCharacterView[];
};

export function objectOf(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function stringOf(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : value == null ? fallback : String(value);
}

function numberOf(value: unknown, fallback = 0): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function booleanOf(value: unknown, fallback = false): boolean {
  return typeof value === "boolean" ? value : fallback;
}

function stringArrayOf(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

function arrayOf(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function unwrap(value: unknown, key: string): unknown {
  const record = objectOf(value);
  return record[key] ?? value;
}

export function normalizeWork(value: unknown): StoryWorkView {
  const record = objectOf(unwrap(value, "work"));
  const counts = objectOf(record.counts);
  const uiState = objectOf(record.ui_state ?? record.uiState);
  const rawRoute = uiState.current_route ?? uiState.currentRoute ?? uiState.choices;
  const route = Array.isArray(rawRoute)
    ? Object.fromEntries(rawRoute.slice(0, -1).map((from, index) => [String(from), String(rawRoute[index + 1])]))
    : objectOf(rawRoute);
  return {
    id: stringOf(record.id),
    title: stringOf(record.title, "無題の作品"),
    kind: stringOf(record.kind, "novel"),
    status: stringOf(record.status, "planning"),
    synopsis: stringOf(record.synopsis),
    plot: stringOf(record.plot),
    styleGuide: stringOf(record.style_guide ?? record.styleGuide),
    plannedEpisodeCount: record.planned_episode_count == null ? null : numberOf(record.planned_episode_count),
    targetEpisodeChars: numberOf(record.target_episode_chars ?? record.targetEpisodeChars, 6000),
    modelOverride: objectOf(record.model_override ?? record.modelOverride),
    resolvedModel: typeof record.resolved_model === "string" ? record.resolved_model : typeof record.resolvedModel === "string" ? record.resolvedModel : null,
    resolvedModelLayer: typeof record.model_layer === "string" ? record.model_layer : typeof record.resolved_model_layer === "string" ? record.resolved_model_layer : null,
    startEpisodeId: typeof record.start_episode_id === "string" ? record.start_episode_id : typeof record.startEpisodeId === "string" ? record.startEpisodeId : null,
    currentRoute: Object.fromEntries(Object.entries(objectOf(route)).filter(([, value]) => typeof value === "string")) as Record<string, string>,
    episodeCount: numberOf(record.episode_count ?? counts.episodes ?? counts.episode_count),
    totalChars: numberOf(record.total_chars ?? record.char_count ?? counts.total_chars ?? counts.characters),
    notesCount: numberOf(record.notes_count ?? counts.notes),
    charactersCount: numberOf(record.characters_count ?? counts.characters),
    rulebooksCount: numberOf(record.rulebooks_count ?? counts.rulebooks),
    branchCount: numberOf(record.branch_count ?? counts.branches),
    updatedAt: typeof record.updated_at === "string" ? record.updated_at : null,
    imageSettings: normalizeImageSettings(record.image_settings ?? record.imageSettings),
  };
}

export function normalizeImageSettings(value: unknown): StoryImageSettings {
  const record = objectOf(value);
  return {
    enabled: booleanOf(record.enabled),
    engine: stringOf(record.engine, "comfyui"),
    maxImagesPerEpisode: numberOf(record.max_images_per_episode ?? record.maxImagesPerEpisode, 3),
    workflowPath: typeof record.workflow_path === "string" ? record.workflow_path : typeof record.workflowPath === "string" ? record.workflowPath : null,
    style: stringOf(record.style),
    negativePrompt: stringOf(record.negative_prompt ?? record.negativePrompt),
  };
}

function browserGeneratedMediaUrl(mediaId: string): string {
  return `/api/python-proxy/api/generated-media/${mediaId}`;
}

function normalizeStoryMediaUrl(rawUrl: unknown, mediaId: unknown): string | null {
  if (typeof mediaId === "string" && mediaId) {
    return browserGeneratedMediaUrl(mediaId);
  }
  if (typeof rawUrl !== "string" || !rawUrl) {
    return null;
  }
  if (rawUrl.startsWith("/api/generated-media/")) {
    const id = rawUrl.slice("/api/generated-media/".length).split(/[/?#]/, 1)[0];
    return id ? browserGeneratedMediaUrl(id) : rawUrl;
  }
  return rawUrl;
}

function normalizeIllustration(value: unknown): StoryIllustrationView {
  const record = objectOf(value);
  const mediaId = record.generated_media_id ?? record.generatedMediaId;
  const rawUrl = record.image_url ?? record.imageUrl;
  return {
    id: stringOf(record.id),
    workId: stringOf(record.work_id ?? record.workId),
    episodeId: stringOf(record.episode_id ?? record.episodeId),
    bodyEtag: stringOf(record.body_etag ?? record.bodyEtag),
    anchorQuote: stringOf(record.anchor_quote ?? record.anchorQuote),
    ordering: numberOf(record.ordering),
    sceneDescription: stringOf(record.scene_description ?? record.sceneDescription),
    visualPrompt: stringOf(record.visual_prompt ?? record.visualPrompt),
    status: stringOf(record.status, "pending"),
    generatedMediaId: typeof mediaId === "string" ? mediaId : null,
    errorMessage: typeof record.error_message === "string" ? record.error_message : typeof record.errorMessage === "string" ? record.errorMessage : null,
    resolvedIndex: typeof record.resolved_index === "number" ? record.resolved_index : typeof record.resolvedIndex === "number" ? record.resolvedIndex : null,
    stale: booleanOf(record.stale),
    imageUrl: normalizeStoryMediaUrl(rawUrl, mediaId),
  };
}

export function normalizeIllustrations(value: unknown): StoryIllustrationListView {
  const record = objectOf(value);
  return {
    active: arrayOf(record.active).map(normalizeIllustration),
    stale: arrayOf(record.stale).map(normalizeIllustration),
  };
}

/** 旧ロールプレイ画面が読む DTO を story の読み取り互換射影から正規化する。 */
export function normalizeLegacyScenario(value: unknown): StoryLegacyScenarioView {
  const record = objectOf(unwrap(value, "scenario"));
  const characters = arrayOf(record.characters).map((item) => {
    const entry = objectOf(item);
    return {
      id: stringOf(entry.id),
      name: stringOf(entry.name, "名前未設定"),
      role: typeof entry.role === "string" ? entry.role : undefined,
      description: typeof entry.description === "string" ? entry.description : undefined,
    };
  });
  return {
    id: stringOf(record.id),
    title: stringOf(record.title, "無題の作品"),
    description: stringOf(record.description),
    characters,
  };
}

export function normalizeEpisode(value: unknown): StoryEpisodeView {
  const record = objectOf(unwrap(value, "episode"));
  const body = stringOf(record.body ?? record.content);
  const rawStatus = stringOf(record.status, "unwritten");
  const status = ({ in_progress: "draft", editing: "revising", completed: "done" } as Record<string, StoryStatus>)[rawStatus] || (rawStatus === "unwritten" || rawStatus === "draft" || rawStatus === "revising" || rawStatus === "done" || rawStatus === "on_hold" ? rawStatus : "unwritten");
  return {
    id: stringOf(record.id),
    workId: typeof record.work_id === "string" ? record.work_id : null,
    title: stringOf(record.title, "無題の章"),
    plot: stringOf(record.plot),
    body,
    summary: stringOf(record.summary),
    premiseNote: stringOf(record.premise_note ?? record.premiseNote),
    status,
    targetChars: numberOf(record.target_chars, 6000),
    charCount: numberOf(record.char_count, Array.from(body).length),
    bodyEtag: stringOf(record.body_etag ?? record.bodyEtag),
    currentRevNo: numberOf(record.current_rev_no ?? record.currentRevNo, 1),
    mapX: numberOf(record.map_x ?? record.mapX),
    mapY: numberOf(record.map_y ?? record.mapY),
    unplaced: booleanOf(record.unplaced),
  };
}

export function normalizeLink(value: unknown): StoryLinkView {
  const record = objectOf(value);
  return {
    id: stringOf(record.id),
    from: stringOf(record.from ?? record.from_episode_id),
    to: stringOf(record.to ?? record.to_episode_id),
    choiceLabel: stringOf(record.choice_label ?? record.choiceLabel),
    isPrimary: booleanOf(record.is_primary ?? record.isPrimary),
    position: numberOf(record.position),
  };
}

export function normalizeGraph(value: unknown): StoryGraphView {
  const record = objectOf(value);
  return {
    episodes: arrayOf(record.episodes).map(normalizeEpisode),
    links: arrayOf(record.links).map(normalizeLink),
    startEpisodeId: typeof record.start_episode_id === "string" ? record.start_episode_id : null,
  };
}

export function normalizeCharacters(value: unknown): StoryCharacterView[] {
  const record = objectOf(value);
  const values = arrayOf(record.characters ?? value);
  return values.map((item) => {
    const entry = objectOf(item);
    const id = stringOf(entry.id);
    const imagePath = stringOf(entry.image_path ?? entry.imagePath);
    return {
      id,
      name: stringOf(entry.name, "名前未設定"),
      aliases: stringArrayOf(entry.aliases),
      summary: stringOf(entry.summary),
      description: stringOf(entry.description),
      notes: stringOf(entry.notes),
      aiMode: stringOf(entry.ai_mode ?? entry.aiMode, "keyword"),
      keywords: stringArrayOf(entry.keywords),
      roleNote: stringOf(entry.role_note ?? entry.roleNote),
      position: numberOf(entry.position),
      included: booleanOf(entry.included ?? entry.enabled),
      imagePath,
      imageUrl: imagePath
        ? `/api/story/characters/${encodeURIComponent(id)}/image?v=${encodeURIComponent(imagePath.split("/").pop() || "")}`
        : null,
    };
  });
}

export function normalizeRulebooks(value: unknown): StoryRulebookView[] {
  const record = objectOf(value);
  return arrayOf(record.rulebooks ?? value).map((item) => {
    const entry = objectOf(item);
    return { id: stringOf(entry.id), name: stringOf(entry.name, "名称未設定"), content: stringOf(entry.content), enabled: booleanOf(entry.enabled ?? entry.is_enabled), position: numberOf(entry.position), applied: booleanOf(entry.applied ?? entry.enabled ?? entry.is_enabled) };
  });
}

export function normalizeNotes(value: unknown): StoryNoteView[] {
  const record = objectOf(value);
  return arrayOf(record.notes ?? value).map((item) => {
    const entry = objectOf(item);
    return { id: stringOf(entry.id), title: stringOf(entry.title, "資料"), content: stringOf(entry.content), aiMode: stringOf(entry.ai_mode ?? entry.aiMode, "keyword"), keywords: stringArrayOf(entry.keywords), position: numberOf(entry.position) };
  });
}

export function normalizeRevisions(value: unknown): StoryRevisionView[] {
  const record = objectOf(value);
  return arrayOf(record.items ?? record.revisions ?? value).map((item) => {
    const entry = objectOf(item);
    return { id: stringOf(entry.id ?? entry.rev_no), revNo: numberOf(entry.rev_no ?? entry.revNo), origin: stringOf(entry.origin, "manual"), message: stringOf(entry.message), createdAt: typeof entry.created_at === "string" ? entry.created_at : null, charCount: numberOf(entry.char_count), body: typeof entry.body === "string" ? entry.body : undefined };
  });
}

export function normalizeContext(value: unknown): StoryContextView {
  const record = objectOf(value);
  const modelRecord = objectOf(record.model);
  const modelValue = typeof record.model === "string" ? record.model : modelRecord.model ?? record.resolved_model;
  const providerValue = modelRecord.provider ?? record.provider;
  const layerValue = modelRecord.layer ?? record.layer ?? record.model_layer ?? record.resolved_model_layer;
  return {
    prompt: stringOf(record.prompt),
    injected: Array.isArray(record.injected) ? record.injected.map((item) => objectOf(item)) : [],
    provider: typeof providerValue === "string" ? providerValue : null,
    model: typeof modelValue === "string" ? modelValue : null,
    layer: typeof layerValue === "string" ? layerValue : null,
    resolvedModel: typeof record.resolved_model === "string" ? record.resolved_model : modelValue ? String(modelValue) : null,
    modelLayer: typeof record.model_layer === "string" ? record.model_layer : typeof record.resolved_model_layer === "string" ? record.resolved_model_layer : layerValue ? String(layerValue) : null,
  };
}

export function normalizeJob(value: unknown): StoryJobView {
  const record = objectOf(unwrap(value, "job"));
  const progress = objectOf(record.progress);
  const rawItems = Array.isArray(record.items) ? record.items : Array.isArray(progress.items) ? progress.items : [];
  return {
    id: stringOf(record.id),
    status: stringOf(record.status, "queued"),
    message: typeof record.message === "string" ? record.message : null,
    error: typeof record.error === "string" ? record.error : null,
    result: objectOf(record.result),
    progress,
    items: rawItems.map((item) => {
      const entry = objectOf(item);
      return { id: stringOf(entry.id ?? entry.episode_id), title: typeof entry.title === "string" ? entry.title : undefined, status: stringOf(entry.status ?? entry.state, "queued"), error: typeof entry.error === "string" ? entry.error : null };
    }),
  };
}

export function listFrom(value: unknown, key: string): unknown[] {
  const record = objectOf(value);
  return arrayOf(record[key] ?? value);
}
