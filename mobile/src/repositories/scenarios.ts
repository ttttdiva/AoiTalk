import { eq, inArray, isNull } from "drizzle-orm";
import { getDb, schema } from "../db/client";
import { getToken } from "../lib/auth";
import { fetchApi } from "../lib/api-client";
import { useNetworkStore } from "../stores/network";
import type {
  Scenario,
  ScenarioCharacter,
  ScenarioDetail,
  ScenarioEpisode,
  ScenarioScene,
} from "../types/api";

type DbScenario = typeof schema.scenarios.$inferSelect;
type DbScenarioCharacter = typeof schema.scenarioCharacters.$inferSelect;
type DbScenarioScene = typeof schema.scenarioScenes.$inferSelect;
type DbScenarioEpisode = typeof schema.scenarioEpisodes.$inferSelect;

async function canUseServer(): Promise<boolean> {
  const network = useNetworkStore.getState();
  return network.online && network.serverReachable && Boolean(await getToken());
}

function arrayValue<T>(value: unknown, fallback: T[] = []): T[] {
  return Array.isArray(value) ? (value as T[]) : fallback;
}

function parseRelationships(value: unknown): unknown[] | string {
  if (Array.isArray(value)) return value;
  if (typeof value === "string") {
    try {
      const parsed = JSON.parse(value) as unknown;
      return Array.isArray(parsed) ? parsed : value;
    } catch {
      return value;
    }
  }
  return [];
}

function toScenario(row: DbScenario): Scenario {
  return {
    id: row.id,
    title: row.title,
    scenario_kind: row.scenarioKind ?? undefined,
    ruleset: row.ruleset ?? undefined,
    description: row.description ?? null,
    genre: row.genre ?? null,
    perspective: row.perspective ?? null,
    setting: row.setting ?? null,
    opening_text: row.openingText ?? null,
    gm_instructions: row.gmInstructions ?? null,
    tags: arrayValue<string>(row.tags),
    cover_image_path: row.coverImagePath ?? undefined,
    is_published: row.isPublished ?? undefined,
    created_by: row.createdBy ?? undefined,
    voice_tone: row.voiceTone ?? undefined,
    voice_tense_rules: row.voiceTenseRules ?? undefined,
    voice_vocabulary_register: row.voiceVocabularyRegister ?? undefined,
    voice_banned_expressions: arrayValue<string>(row.voiceBannedExpressions),
    voice_example_passages: row.voiceExamplePassages ?? undefined,
    created_at: row.createdAt ?? null,
    updated_at: row.updatedAt ?? null,
  } as Scenario;
}

function toCharacter(row: DbScenarioCharacter): ScenarioCharacter {
  const relationships = parseRelationships(row.relationships);
  return {
    id: row.id,
    scenario_id: row.scenarioId,
    character_id: row.characterId ?? undefined,
    role: row.role ?? "npc",
    name: row.name,
    description: row.description ?? null,
    personality_override: row.personalityOverride ?? undefined,
    appearance_tags_override: row.appearanceTagsOverride ?? undefined,
    sort_order: row.sortOrder ?? undefined,
    backstory: row.backstory ?? undefined,
    psychology: row.psychology ?? undefined,
    speech_patterns: row.speechPatterns ?? undefined,
    speech_pattern: row.speechPatterns ?? undefined,
    relationships,
    relationships_data: Array.isArray(relationships) ? relationships : [],
    character_arc: row.characterArc ?? undefined,
    arc: row.characterArc ?? undefined,
    importance: row.importance ?? undefined,
    example_dialogues: row.exampleDialogues ?? undefined,
    dialogue_samples: row.exampleDialogues ?? undefined,
    trpg_ruleset: row.trpgRuleset ?? undefined,
    trpg_pc_state: row.trpgPcState ?? undefined,
  } as ScenarioCharacter;
}

function toScene(row: DbScenarioScene): ScenarioScene {
  return {
    id: row.id,
    scenario_id: row.scenarioId,
    episode_id: row.episodeId ?? undefined,
    title: row.title,
    description: row.description ?? null,
    scene_type: row.sceneType ?? null,
    gm_instructions: row.gmInstructions ?? null,
    image_prompt: row.imagePrompt ?? null,
    transitions: arrayValue(row.transitions),
    sort_order: row.sortOrder ?? undefined,
    order_index: row.sortOrder ?? undefined,
    content: row.content ?? undefined,
    body: row.content ?? undefined,
    content_versions: arrayValue(row.contentVersions),
    word_count: row.wordCount ?? undefined,
    status: row.status ?? null,
    state_snapshot: row.stateSnapshot ?? undefined,
  } as ScenarioScene;
}

function toEpisode(row: DbScenarioEpisode): ScenarioEpisode {
  return {
    id: row.id,
    scenario_id: row.scenarioId,
    title: row.title,
    synopsis_sentence: row.synopsisSentence ?? null,
    one_line_summary: row.synopsisSentence ?? null,
    synopsis_paragraph: row.synopsisParagraph ?? null,
    paragraph_summary: row.synopsisParagraph ?? null,
    synopsis_full: row.synopsisFull ?? null,
    full_summary: row.synopsisFull ?? null,
    beat_sheet: arrayValue(row.beatSheet),
    status: row.status ?? null,
    sort_order: row.sortOrder ?? undefined,
    created_at: row.createdAt ?? null,
    updated_at: row.updatedAt ?? null,
  } as ScenarioEpisode;
}

export async function applyRemoteScenarios(list: Scenario[]): Promise<void> {
  if (!list.length) return;
  const db = getDb();
  const now = new Date().toISOString();
  for (const scenario of list) {
    await db
      .insert(schema.scenarios)
      .values({
        id: scenario.id,
        title: scenario.title,
        scenarioKind: (scenario as { scenario_kind?: string }).scenario_kind ?? "writing",
        ruleset: (scenario as { ruleset?: string }).ruleset ?? "",
        description: scenario.description ?? null,
        genre: scenario.genre ?? null,
        perspective: scenario.perspective ?? null,
        setting: scenario.setting ?? null,
        openingText: scenario.opening_text ?? null,
        gmInstructions: scenario.gm_instructions ?? null,
        tags: scenario.tags ?? [],
        coverImagePath: (scenario as { cover_image_path?: string }).cover_image_path ?? null,
        isPublished: (scenario as { is_published?: boolean }).is_published ?? false,
        createdBy: (scenario as { created_by?: string | null }).created_by ?? null,
        voiceTone: (scenario as { voice_tone?: string }).voice_tone ?? null,
        voiceTenseRules: (scenario as { voice_tense_rules?: string }).voice_tense_rules ?? null,
        voiceVocabularyRegister:
          (scenario as { voice_vocabulary_register?: string }).voice_vocabulary_register ?? null,
        voiceBannedExpressions:
          (scenario as { voice_banned_expressions?: string[] }).voice_banned_expressions ?? [],
        voiceExamplePassages:
          (scenario as { voice_example_passages?: string }).voice_example_passages ?? null,
        createdAt: scenario.created_at ?? now,
        updatedAt: scenario.updated_at ?? now,
        deletedAt: null,
      })
      .onConflictDoUpdate({
        target: schema.scenarios.id,
        set: {
          title: scenario.title,
          scenarioKind: (scenario as { scenario_kind?: string }).scenario_kind ?? "writing",
          ruleset: (scenario as { ruleset?: string }).ruleset ?? "",
          description: scenario.description ?? null,
          genre: scenario.genre ?? null,
          perspective: scenario.perspective ?? null,
          setting: scenario.setting ?? null,
          openingText: scenario.opening_text ?? null,
          gmInstructions: scenario.gm_instructions ?? null,
          tags: scenario.tags ?? [],
          coverImagePath: (scenario as { cover_image_path?: string }).cover_image_path ?? null,
          isPublished: (scenario as { is_published?: boolean }).is_published ?? false,
          createdBy: (scenario as { created_by?: string | null }).created_by ?? null,
          voiceTone: (scenario as { voice_tone?: string }).voice_tone ?? null,
          voiceTenseRules: (scenario as { voice_tense_rules?: string }).voice_tense_rules ?? null,
          voiceVocabularyRegister:
            (scenario as { voice_vocabulary_register?: string }).voice_vocabulary_register ?? null,
          voiceBannedExpressions:
            (scenario as { voice_banned_expressions?: string[] }).voice_banned_expressions ?? [],
          voiceExamplePassages:
            (scenario as { voice_example_passages?: string }).voice_example_passages ?? null,
          updatedAt: scenario.updated_at ?? now,
          deletedAt: null,
        },
      });
  }
}

export async function applyScenarioTombstones(
  tombstones: Array<{ id: string; deleted_at?: string | null }>,
): Promise<void> {
  if (!tombstones.length) return;
  const db = getDb();
  for (const item of tombstones) {
    const deletedAt = item.deleted_at ?? new Date().toISOString();
    await db
      .update(schema.scenarios)
      .set({ deletedAt, updatedAt: deletedAt })
      .where(eq(schema.scenarios.id, item.id));
  }
}

export async function applyRemoteScenarioCharacters(
  list: ScenarioCharacter[],
): Promise<void> {
  if (!list.length) return;
  const db = getDb();
  for (const character of list) {
    const relationships = (character as { relationships_data?: unknown }).relationships_data ??
      (character as { relationships?: unknown }).relationships ?? [];
    await db
      .insert(schema.scenarioCharacters)
      .values({
        id: character.id,
        scenarioId: character.scenario_id,
        characterId: (character as { character_id?: string | null }).character_id ?? null,
        role: character.role ?? "npc",
        name: character.name,
        description: character.description ?? null,
        personalityOverride:
          (character as { personality_override?: string }).personality_override ?? null,
        appearanceTagsOverride:
          (character as { appearance_tags_override?: string }).appearance_tags_override ?? null,
        sortOrder: (character as { sort_order?: number }).sort_order ?? 0,
        backstory: (character as { backstory?: string }).backstory ?? null,
        psychology: (character as { psychology?: string }).psychology ?? null,
        speechPatterns:
          (character as { speech_patterns?: string; speech_pattern?: string }).speech_patterns ??
          (character as { speech_patterns?: string; speech_pattern?: string }).speech_pattern ??
          null,
        relationships: Array.isArray(relationships) ? relationships : [],
        characterArc:
          (character as { character_arc?: string; arc?: string }).character_arc ??
          (character as { character_arc?: string; arc?: string }).arc ??
          null,
        importance: (character as { importance?: number }).importance ?? 0,
        exampleDialogues:
          (character as { example_dialogues?: string; dialogue_samples?: string }).example_dialogues ??
          (character as { example_dialogues?: string; dialogue_samples?: string }).dialogue_samples ??
          null,
        trpgRuleset: (character as { trpg_ruleset?: string }).trpg_ruleset ?? null,
        trpgPcState: (character as { trpg_pc_state?: Record<string, unknown> }).trpg_pc_state ?? {},
        deletedAt: null,
      })
      .onConflictDoUpdate({
        target: schema.scenarioCharacters.id,
        set: {
          scenarioId: character.scenario_id,
          characterId: (character as { character_id?: string | null }).character_id ?? null,
          role: character.role ?? "npc",
          name: character.name,
          description: character.description ?? null,
          personalityOverride:
            (character as { personality_override?: string }).personality_override ?? null,
          appearanceTagsOverride:
            (character as { appearance_tags_override?: string }).appearance_tags_override ?? null,
          sortOrder: (character as { sort_order?: number }).sort_order ?? 0,
          backstory: (character as { backstory?: string }).backstory ?? null,
          psychology: (character as { psychology?: string }).psychology ?? null,
          speechPatterns:
            (character as { speech_patterns?: string; speech_pattern?: string }).speech_patterns ??
            (character as { speech_patterns?: string; speech_pattern?: string }).speech_pattern ??
            null,
          relationships: Array.isArray(relationships) ? relationships : [],
          characterArc:
            (character as { character_arc?: string; arc?: string }).character_arc ??
            (character as { character_arc?: string; arc?: string }).arc ??
            null,
          importance: (character as { importance?: number }).importance ?? 0,
          exampleDialogues:
            (character as { example_dialogues?: string; dialogue_samples?: string }).example_dialogues ??
            (character as { example_dialogues?: string; dialogue_samples?: string }).dialogue_samples ??
            null,
          trpgRuleset: (character as { trpg_ruleset?: string }).trpg_ruleset ?? null,
          trpgPcState: (character as { trpg_pc_state?: Record<string, unknown> }).trpg_pc_state ?? {},
          deletedAt: null,
        },
      });
  }
}

export async function applyRemoteScenarioScenes(list: ScenarioScene[]): Promise<void> {
  if (!list.length) return;
  const db = getDb();
  for (const scene of list) {
    await db
      .insert(schema.scenarioScenes)
      .values({
        id: scene.id,
        scenarioId: scene.scenario_id,
        episodeId: (scene as { episode_id?: string | null }).episode_id ?? null,
        title: scene.title,
        description: scene.description ?? null,
        sceneType: scene.scene_type ?? null,
        gmInstructions: scene.gm_instructions ?? null,
        imagePrompt: scene.image_prompt ?? null,
        transitions: (scene as { transitions?: unknown[] }).transitions ?? [],
        sortOrder: scene.sort_order ?? 0,
        content: (scene as { content?: string; body?: string }).content ??
          (scene as { content?: string; body?: string }).body ?? null,
        contentVersions: (scene as { content_versions?: unknown[] }).content_versions ?? [],
        wordCount: (scene as { word_count?: number }).word_count ?? 0,
        status: scene.status ?? "draft",
        stateSnapshot: (scene as { state_snapshot?: Record<string, unknown> }).state_snapshot ?? {},
        deletedAt: null,
      })
      .onConflictDoUpdate({
        target: schema.scenarioScenes.id,
        set: {
          scenarioId: scene.scenario_id,
          episodeId: (scene as { episode_id?: string | null }).episode_id ?? null,
          title: scene.title,
          description: scene.description ?? null,
          sceneType: scene.scene_type ?? null,
          gmInstructions: scene.gm_instructions ?? null,
          imagePrompt: scene.image_prompt ?? null,
          transitions: (scene as { transitions?: unknown[] }).transitions ?? [],
          sortOrder: scene.sort_order ?? 0,
          content: (scene as { content?: string; body?: string }).content ??
            (scene as { content?: string; body?: string }).body ?? null,
          contentVersions: (scene as { content_versions?: unknown[] }).content_versions ?? [],
          wordCount: (scene as { word_count?: number }).word_count ?? 0,
          status: scene.status ?? "draft",
          stateSnapshot: (scene as { state_snapshot?: Record<string, unknown> }).state_snapshot ?? {},
          deletedAt: null,
        },
      });
  }
}

export async function applyRemoteScenarioEpisodes(
  list: ScenarioEpisode[],
): Promise<void> {
  if (!list.length) return;
  const db = getDb();
  const now = new Date().toISOString();
  for (const episode of list) {
    await db
      .insert(schema.scenarioEpisodes)
      .values({
        id: episode.id,
        scenarioId: episode.scenario_id,
        title: episode.title,
        synopsisSentence: episode.synopsis_sentence ?? null,
        synopsisParagraph: episode.synopsis_paragraph ?? null,
        synopsisFull: episode.synopsis_full ?? null,
        beatSheet: (episode as { beat_sheet?: unknown[] }).beat_sheet ?? [],
        status: episode.status ?? "draft",
        sortOrder: episode.sort_order ?? 0,
        createdAt: (episode as { created_at?: string | null }).created_at ?? now,
        updatedAt: (episode as { updated_at?: string | null }).updated_at ?? now,
        deletedAt: null,
      })
      .onConflictDoUpdate({
        target: schema.scenarioEpisodes.id,
        set: {
          scenarioId: episode.scenario_id,
          title: episode.title,
          synopsisSentence: episode.synopsis_sentence ?? null,
          synopsisParagraph: episode.synopsis_paragraph ?? null,
          synopsisFull: episode.synopsis_full ?? null,
          beatSheet: (episode as { beat_sheet?: unknown[] }).beat_sheet ?? [],
          status: episode.status ?? "draft",
          sortOrder: episode.sort_order ?? 0,
          updatedAt: (episode as { updated_at?: string | null }).updated_at ?? now,
          deletedAt: null,
        },
      });
  }
}

async function markMissingRowsDeleted(
  table: "scenarios" | "scenarioCharacters" | "scenarioScenes" | "scenarioEpisodes",
  ids: string[] | undefined,
): Promise<void> {
  if (!ids) return;
  const db = getDb();
  const deletedAt = new Date().toISOString();
  const target = schema[table];
  const rows = await db
    .select({ id: target.id })
    .from(target)
    .where(isNull(target.deletedAt));
  const authoritative = new Set(ids);
  const missing = rows.map((row) => row.id).filter((id) => !authoritative.has(id));
  if (!missing.length) return;
  await db.update(target).set({ deletedAt }).where(inArray(target.id, missing));
}

export async function reconcileScenariosWithServer(ids?: string[]): Promise<void> {
  await markMissingRowsDeleted("scenarios", ids);
}

export async function reconcileScenarioCharactersWithServer(ids?: string[]): Promise<void> {
  await markMissingRowsDeleted("scenarioCharacters", ids);
}

export async function reconcileScenarioScenesWithServer(ids?: string[]): Promise<void> {
  await markMissingRowsDeleted("scenarioScenes", ids);
}

export async function reconcileScenarioEpisodesWithServer(ids?: string[]): Promise<void> {
  await markMissingRowsDeleted("scenarioEpisodes", ids);
}

export const scenariosRepo = {
  async listLocal(): Promise<Scenario[]> {
    const db = getDb();
    const rows = await db
      .select()
      .from(schema.scenarios)
      .where(isNull(schema.scenarios.deletedAt));
    return rows.map(toScenario).sort((a, b) =>
      String(b.updated_at ?? "").localeCompare(String(a.updated_at ?? "")),
    );
  },

  async getLocal(scenarioId: string): Promise<ScenarioDetail | null> {
    const db = getDb();
    const rows = await db
      .select()
      .from(schema.scenarios)
      .where(eq(schema.scenarios.id, scenarioId));
    const scenario = rows.find((row) => !row.deletedAt);
    if (!scenario) return null;
    const [characters, scenes, episodes] = await Promise.all([
      db
        .select()
        .from(schema.scenarioCharacters)
        .where(eq(schema.scenarioCharacters.scenarioId, scenarioId)),
      db
        .select()
        .from(schema.scenarioScenes)
        .where(eq(schema.scenarioScenes.scenarioId, scenarioId)),
      db
        .select()
        .from(schema.scenarioEpisodes)
        .where(eq(schema.scenarioEpisodes.scenarioId, scenarioId)),
    ]);
    return {
      ...toScenario(scenario),
      characters: characters
        .filter((item) => !item.deletedAt)
        .map(toCharacter)
        .sort((a, b) =>
          ((a as { sort_order?: number }).sort_order ?? 0) -
          ((b as { sort_order?: number }).sort_order ?? 0),
        ),
      scenes: scenes
        .filter((item) => !item.deletedAt)
        .map(toScene)
        .sort((a, b) => (a.sort_order ?? 0) - (b.sort_order ?? 0)),
      episodes: episodes
        .filter((item) => !item.deletedAt)
        .map(toEpisode)
        .sort((a, b) => (a.sort_order ?? 0) - (b.sort_order ?? 0)),
    };
  },

  async list(): Promise<Scenario[]> {
    const local = await this.listLocal();
    if (!(await canUseServer())) return local;

    const refresh = async (): Promise<Scenario[]> => {
      const data = await fetchApi<{ scenarios: Scenario[] }>("/api/scenarios");
      await applyRemoteScenarios(data.scenarios);
      return data.scenarios;
    };

    if (!local.length) {
      try {
        return await refresh();
      } catch {
        return local;
      }
    }

    refresh().catch(() => undefined);
    return local;
  },

  async get(scenarioId: string): Promise<ScenarioDetail> {
    if (await canUseServer()) {
      try {
        const remote = await fetchApi<ScenarioDetail>(`/api/scenarios/${scenarioId}`);
        await applyRemoteScenarios([remote]);
        await applyRemoteScenarioCharacters(remote.characters ?? []);
        await applyRemoteScenarioScenes(remote.scenes ?? []);
        await applyRemoteScenarioEpisodes(remote.episodes ?? []);
        return remote;
      } catch {
        // fall back to local cache below
      }
    }
    const local = await this.getLocal(scenarioId);
    if (local) return local;
    throw new Error("Scenario is not available offline");
  },
};
