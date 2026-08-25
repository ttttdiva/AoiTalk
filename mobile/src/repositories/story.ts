/**
 * Canonical Story Studio local cache.
 *
 * Story intentionally does not use the generic outbox.  All server mutations
 * go through /api/story and successful responses refresh this cache.  The only
 * local-first write is storyLocalDrafts, which protects manuscript edits when
 * the network is unavailable or the server returns an optimistic-concurrency
 * conflict.
 */

import { and, asc, desc, eq, notInArray } from "drizzle-orm";
import AsyncStorage from "@react-native-async-storage/async-storage";
import { getDb, schema } from "../db/client";
import { getToken, getTokenAuthScope } from "../lib/auth";
import type {
  StoryCharacter,
  StoryEpisode,
  StoryEpisodeRevision,
  StoryGraph,
  StoryJob,
  StoryLink,
  StoryLocalDraft,
  StoryNote,
  StoryOverview,
  StoryRulebook,
  StoryWork,
  StoryWorkCharacter,
  StoryWorkRulebook,
  StoryWritingSession,
} from "../types/api";

type AuthScope = string;
type DbWork = typeof schema.storyWorks.$inferSelect;
type DbEpisode = typeof schema.storyEpisodes.$inferSelect;
type DbLink = typeof schema.storyLinks.$inferSelect;
type DbCharacter = typeof schema.storyCharacters.$inferSelect;
type DbWorkCharacter = typeof schema.storyWorkCharacters.$inferSelect;
type DbRulebook = typeof schema.storyRulebooks.$inferSelect;
type DbWorkRulebook = typeof schema.storyWorkRulebooks.$inferSelect;
type DbNote = typeof schema.storyNotes.$inferSelect;
type DbRevision = typeof schema.storyEpisodeRevisions.$inferSelect;
type DbJob = typeof schema.storyGenerationJobs.$inferSelect;
type DbWritingSession = typeof schema.storyWritingSessions.$inferSelect;
type DbDraft = typeof schema.storyLocalDrafts.$inferSelect;

const LEGACY_WRITING_DRAFT_PREFIX = "scenario-session-draft:";

function jsonArray<T>(value: unknown): T[] {
  return Array.isArray(value) ? (value as T[]) : [];
}

function jsonRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function nowIso(): string {
  return new Date().toISOString();
}

function legacyWritingDraftKey(workId: string): string {
  return `${LEGACY_WRITING_DRAFT_PREFIX}${workId}`;
}

export type LegacyStoryWritingDraftMigrationStatus =
  | "migrated"
  | "not_found"
  | "wrong_auth"
  | "invalid_payload"
  | "invalid_target_episode"
  | "failed";

export interface LegacyStoryWritingDraftMigrationResult {
  status: LegacyStoryWritingDraftMigrationStatus;
  workId: string;
  legacyKey: string;
  targetEpisodeId?: string | null;
  targetSceneId?: string | null;
  reason?: string;
}

type LegacyWritingDraftPayload = {
  targetEpisodeId?: unknown;
  targetSceneId?: unknown;
  prompt?: unknown;
  [key: string]: unknown;
};

function ownerIdForAuthScope(scope: AuthScope): string | null {
  if (!scope.startsWith("auth:") || scope.startsWith("auth:opaque:")) return null;
  const userId = scope.slice("auth:".length);
  return userId || null;
}

function parseLegacyWritingDraft(raw: string): LegacyWritingDraftPayload | null {
  try {
    const parsed: unknown = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return null;
    return parsed as LegacyWritingDraftPayload;
  } catch {
    return null;
  }
}

/** Resolve the current account scope; no token is represented by `anonymous`. */
export async function resolveStoryAuthScope(): Promise<AuthScope> {
  return getTokenAuthScope(await getToken());
}

function workFromRow(row: DbWork): StoryWork {
  return {
    id: row.id,
    user_id: row.userId ?? "",
    title: row.title,
    synopsis: row.synopsis,
    plot: row.plot,
    style_guide: row.styleGuide,
    kind: row.kind,
    status: row.status,
    target_episode_chars: row.targetEpisodeChars ?? 6000,
    planned_episode_count: row.plannedEpisodeCount,
    start_episode_id: row.startEpisodeId,
    ui_state: jsonRecord(row.uiState),
    model_override: jsonRecord(row.modelOverride),
    image_settings: jsonRecord(row.imageSettings),
    resolved_model: row.resolvedModel,
    model_layer: row.modelLayer,
    episode_count: row.episodeCount,
    char_count: row.charCount,
    created_at: row.createdAt,
    updated_at: row.updatedAt,
    archived_at: row.archivedAt,
  };
}

function episodeFromRow(row: DbEpisode): StoryEpisode {
  return {
    id: row.id,
    work_id: row.workId,
    title: row.title,
    plot: row.plot,
    summary: row.summary,
    summary_locked: row.summaryLocked ?? false,
    premise_note: row.premiseNote,
    status: row.status,
    target_chars: row.targetChars,
    char_count: row.charCount ?? 0,
    body: row.body,
    body_etag: row.bodyEtag,
    map_x: row.mapX,
    map_y: row.mapY,
    sort_hint: row.sortHint ?? 0,
    current_rev_no: row.currentRevNo ?? 0,
    created_at: row.createdAt,
    updated_at: row.updatedAt,
    archived_at: row.archivedAt,
  };
}

function linkFromRow(row: DbLink): StoryLink {
  return {
    id: row.id,
    work_id: row.workId,
    from_episode_id: row.fromEpisodeId,
    to_episode_id: row.toEpisodeId,
    choice_label: row.choiceLabel,
    position: row.position ?? 0,
    is_primary: row.isPrimary ?? false,
    created_at: row.createdAt,
  };
}

function characterFromRow(row: DbCharacter): StoryCharacter {
  return {
    id: row.id,
    user_id: row.userId ?? "",
    name: row.name,
    aliases: jsonArray<string>(row.aliases),
    summary: row.summary,
    description: row.description,
    notes: row.notes,
    ai_mode: row.aiMode,
    keywords: jsonArray<string>(row.keywords),
    image_path: row.imagePath,
    created_at: row.createdAt,
    updated_at: row.updatedAt,
    archived_at: row.archivedAt,
  };
}

function workCharacterFromRow(row: DbWorkCharacter): StoryWorkCharacter {
  return {
    work_id: row.workId,
    character_id: row.characterId,
    role_note: row.roleNote,
    position: row.position ?? 0,
  };
}

function rulebookFromRow(row: DbRulebook): StoryRulebook {
  return {
    id: row.id,
    user_id: row.userId ?? "",
    name: row.name,
    content: row.content,
    created_at: row.createdAt,
    updated_at: row.updatedAt,
    archived_at: row.archivedAt,
  };
}

function workRulebookFromRow(row: DbWorkRulebook): StoryWorkRulebook {
  return {
    work_id: row.workId,
    rulebook_id: row.rulebookId,
    enabled: row.enabled ?? true,
    position: row.position ?? 0,
  };
}

function noteFromRow(row: DbNote): StoryNote {
  return {
    id: row.id,
    work_id: row.workId,
    title: row.title,
    content: row.content,
    ai_mode: row.aiMode,
    keywords: jsonArray<string>(row.keywords),
    position: row.position ?? 0,
    created_at: row.createdAt,
    updated_at: row.updatedAt,
  };
}

function revisionFromRow(row: DbRevision): StoryEpisodeRevision {
  return {
    id: row.id,
    episode_id: row.episodeId,
    rev_no: row.revNo,
    title: row.title,
    plot: row.plot,
    body: row.body,
    message: row.message,
    origin: row.origin,
    body_sha256: row.bodySha256,
    char_count: row.charCount ?? 0,
    created_by: row.createdBy,
    created_at: row.createdAt,
  };
}

function jobFromRow(row: DbJob): StoryJob {
  return {
    id: row.id,
    work_id: row.workId,
    kind: row.kind,
    payload: jsonRecord(row.payload),
    status: row.status,
    progress: jsonRecord(row.progress),
    result: row.result == null ? null : jsonRecord(row.result),
    error: row.error,
    created_at: row.createdAt,
    started_at: row.startedAt,
    finished_at: row.finishedAt,
  };
}

function writingSessionFromRow(row: DbWritingSession): StoryWritingSession {
  return {
    id: row.id,
    work_id: row.workId,
    episode_id: row.episodeId,
    conversation_session_id: row.conversationSessionId,
    created_at: row.createdAt,
    updated_at: row.updatedAt,
  };
}

function draftFromRow(row: DbDraft): StoryLocalDraft {
  return {
    auth_scope: row.authScope,
    episode_id: row.episodeId,
    body: row.body,
    expected_etag: row.expectedEtag,
    server_snapshot: row.serverSnapshot
      ? (row.serverSnapshot as StoryEpisode)
      : null,
    conflict_status: row.conflictStatus === "conflict" ? "conflict" : "draft",
    created_at: row.createdAt,
    updated_at: row.updatedAt,
  };
}

function workRow(scope: AuthScope, item: StoryWork) {
  return {
    authScope: scope,
    id: item.id,
    userId: item.user_id || null,
    title: item.title,
    synopsis: item.synopsis ?? null,
    plot: item.plot ?? null,
    styleGuide: item.style_guide ?? null,
    kind: item.kind || "novel",
    status: item.status || "planning",
    targetEpisodeChars: item.target_episode_chars ?? 6000,
    plannedEpisodeCount: item.planned_episode_count ?? null,
    startEpisodeId: item.start_episode_id ?? null,
    uiState: item.ui_state ?? {},
    modelOverride: item.model_override ?? {},
    imageSettings: item.image_settings ?? {},
    resolvedModel: item.resolved_model ?? null,
    modelLayer: item.model_layer ?? null,
    episodeCount: item.episode_count ?? null,
    charCount: item.char_count ?? null,
    createdAt: item.created_at ?? null,
    updatedAt: item.updated_at ?? null,
    archivedAt: item.archived_at ?? null,
  };
}

function episodeRow(scope: AuthScope, item: StoryEpisode) {
  const row: Record<string, unknown> = {
    authScope: scope,
    id: item.id,
    workId: item.work_id,
    title: item.title,
    plot: item.plot ?? null,
    summary: item.summary ?? null,
    summaryLocked: item.summary_locked ?? false,
    premiseNote: item.premise_note ?? null,
    status: item.status || "unwritten",
    targetChars: item.target_chars ?? null,
    charCount: item.char_count ?? 0,
    bodyEtag: item.body_etag ?? null,
    mapX: item.map_x ?? null,
    mapY: item.map_y ?? null,
    sortHint: item.sort_hint ?? 0,
    currentRevNo: item.current_rev_no ?? 0,
    createdAt: item.created_at ?? null,
    updatedAt: item.updated_at ?? null,
    archivedAt: item.archived_at ?? null,
  };
  if (Object.prototype.hasOwnProperty.call(item, "body")) {
    row.body = item.body ?? null;
  }
  return row as {
    authScope: string;
    id: string;
    workId: string;
    title: string;
    plot: string | null;
    summary: string | null;
    summaryLocked: boolean;
    premiseNote: string | null;
    status: string;
    targetChars: number | null;
    charCount: number;
    body?: string | null;
    bodyEtag: string | null;
    mapX: number | null;
    mapY: number | null;
    sortHint: number;
    currentRevNo: number;
    createdAt: string | null;
    updatedAt: string | null;
    archivedAt: string | null;
  };
}

function linkRow(scope: AuthScope, item: StoryLink) {
  return {
    authScope: scope,
    id: item.id,
    workId: item.work_id,
    fromEpisodeId: item.from_episode_id,
    toEpisodeId: item.to_episode_id,
    choiceLabel: item.choice_label ?? null,
    position: item.position ?? 0,
    isPrimary: item.is_primary ?? false,
    createdAt: item.created_at ?? null,
  };
}

function characterRow(scope: AuthScope, item: StoryCharacter) {
  return {
    authScope: scope,
    id: item.id,
    userId: item.user_id || null,
    name: item.name,
    aliases: item.aliases ?? [],
    summary: item.summary ?? null,
    description: item.description ?? null,
    notes: item.notes ?? null,
    aiMode: item.ai_mode || "keyword",
    keywords: item.keywords ?? [],
    imagePath: item.image_path ?? null,
    createdAt: item.created_at ?? null,
    updatedAt: item.updated_at ?? null,
    archivedAt: item.archived_at ?? null,
  };
}

function rulebookRow(scope: AuthScope, item: StoryRulebook) {
  return {
    authScope: scope,
    id: item.id,
    userId: item.user_id || null,
    name: item.name,
    content: item.content ?? null,
    createdAt: item.created_at ?? null,
    updatedAt: item.updated_at ?? null,
    archivedAt: item.archived_at ?? null,
  };
}

function updateSetWithoutKeys<T extends Record<string, unknown>>(
  row: T,
  keys: string[],
): Record<string, unknown> {
  const set: Record<string, unknown> = { ...row };
  for (const key of keys) delete set[key];
  return set;
}

/** Synchronous Drizzle transaction helpers (Expo SQLite `.run()` API). */
function upsertStoryWorkTx(tx: any, row: ReturnType<typeof workRow>): void {
  tx
    .insert(schema.storyWorks)
    .values(row)
    .onConflictDoUpdate({
      target: [schema.storyWorks.authScope, schema.storyWorks.id],
      set: updateSetWithoutKeys(row, ["authScope", "id"]),
    })
    .run();
}

function upsertStoryEpisodeTx(tx: any, row: ReturnType<typeof episodeRow>): void {
  const set = updateSetWithoutKeys(row, ["authScope", "id"]);
  if (!Object.prototype.hasOwnProperty.call(row, "body")) delete set.body;
  tx
    .insert(schema.storyEpisodes)
    .values(row)
    .onConflictDoUpdate({
      target: [schema.storyEpisodes.authScope, schema.storyEpisodes.id],
      set,
    })
    .run();
}

function upsertStoryLinkTx(tx: any, row: ReturnType<typeof linkRow>): void {
  tx
    .insert(schema.storyLinks)
    .values(row)
    .onConflictDoUpdate({
      target: [schema.storyLinks.authScope, schema.storyLinks.id],
      set: updateSetWithoutKeys(row, ["authScope", "id"]),
    })
    .run();
}

function upsertStoryCharacterTx(tx: any, row: ReturnType<typeof characterRow>): void {
  tx
    .insert(schema.storyCharacters)
    .values(row)
    .onConflictDoUpdate({
      target: [schema.storyCharacters.authScope, schema.storyCharacters.id],
      set: updateSetWithoutKeys(row, ["authScope", "id"]),
    })
    .run();
}

function upsertStoryRulebookTx(tx: any, row: ReturnType<typeof rulebookRow>): void {
  tx
    .insert(schema.storyRulebooks)
    .values(row)
    .onConflictDoUpdate({
      target: [schema.storyRulebooks.authScope, schema.storyRulebooks.id],
      set: updateSetWithoutKeys(row, ["authScope", "id"]),
    })
    .run();
}

function upsertStoryWorkCharacterTx(tx: any, row: ReturnType<typeof storyWorkCharacterRow>): void {
  tx
    .insert(schema.storyWorkCharacters)
    .values(row)
    .onConflictDoUpdate({
      target: [
        schema.storyWorkCharacters.authScope,
        schema.storyWorkCharacters.workId,
        schema.storyWorkCharacters.characterId,
      ],
      set: { roleNote: row.roleNote, position: row.position },
    })
    .run();
}

function upsertStoryWorkRulebookTx(tx: any, row: ReturnType<typeof storyWorkRulebookRow>): void {
  tx
    .insert(schema.storyWorkRulebooks)
    .values(row)
    .onConflictDoUpdate({
      target: [
        schema.storyWorkRulebooks.authScope,
        schema.storyWorkRulebooks.workId,
        schema.storyWorkRulebooks.rulebookId,
      ],
      set: { enabled: row.enabled, position: row.position },
    })
    .run();
}

function upsertStoryNoteTx(
  tx: any,
  row: {
    authScope: string;
    id: string;
    workId: string;
    title: string;
    content: string | null;
    aiMode: string;
    keywords: string[];
    position: number;
    createdAt: string | null;
    updatedAt: string | null;
  },
): void {
  tx
    .insert(schema.storyNotes)
    .values(row)
    .onConflictDoUpdate({
      target: [schema.storyNotes.authScope, schema.storyNotes.id],
      set: updateSetWithoutKeys(row, ["authScope", "id"]),
    })
    .run();
}

function storyWorkCharacterRow(scope: AuthScope, item: StoryCharacter) {
  return {
    authScope: scope,
    workId: item.work_id,
    characterId: item.character_id,
    roleNote: item.role_note ?? null,
    position: item.position ?? 0,
  };
}

function storyWorkRulebookRow(scope: AuthScope, item: StoryRulebook) {
  return {
    authScope: scope,
    workId: item.work_id,
    rulebookId: item.rulebook_id,
    enabled: item.enabled ?? true,
    position: item.position ?? 0,
  };
}

async function scopeOrCurrent(scope?: string): Promise<AuthScope> {
  return scope ?? resolveStoryAuthScope();
}

/** Upsert remote works into the account-specific read cache. */
export async function applyRemoteStoryWorks(
  items: StoryWork[],
  requestedScope?: string,
): Promise<void> {
  if (!items.length) return;
  const scope = await scopeOrCurrent(requestedScope);
  const db = getDb();
  for (const item of items) {
    const row = workRow(scope, item);
    await db
      .insert(schema.storyWorks)
      .values(row)
      .onConflictDoUpdate({
        target: [schema.storyWorks.authScope, schema.storyWorks.id],
        set: { ...row, authScope: undefined, id: undefined },
      });
  }
}

/** Replace the authoritative active work projection without deleting archives. */
export async function replaceRemoteStoryWorks(
  items: StoryWork[],
  requestedScope?: string,
): Promise<void> {
  const scope = await scopeOrCurrent(requestedScope);
  const db = getDb();
  const ids = items.map((item) => item.id);
  db.transaction((tx) => {
    if (ids.length) {
      tx
        .update(schema.storyWorks)
        .set({ archivedAt: nowIso() })
        .where(
          and(
            eq(schema.storyWorks.authScope, scope),
            notInArray(schema.storyWorks.id, ids),
          ),
        )
        .run();
    } else {
      tx
        .update(schema.storyWorks)
        .set({ archivedAt: nowIso() })
        .where(eq(schema.storyWorks.authScope, scope))
        .run();
    }
    for (const item of items) upsertStoryWorkTx(tx, workRow(scope, item));
  });
}

export async function applyRemoteStoryEpisodes(
  items: StoryEpisode[],
  requestedScope?: string,
): Promise<void> {
  if (!items.length) return;
  const scope = await scopeOrCurrent(requestedScope);
  const db = getDb();
  for (const item of items) {
    const row = episodeRow(scope, item);
    // Graph/list endpoints intentionally omit the encrypted body.  Do not
    // overwrite a previously cached manuscript with NULL merely because the
    // response is metadata-only.
    const set: Record<string, unknown> = { ...row };
    delete set.authScope;
    delete set.id;
    if (!Object.prototype.hasOwnProperty.call(item, "body")) delete set.body;
    await db
      .insert(schema.storyEpisodes)
      .values(row)
      .onConflictDoUpdate({
        target: [schema.storyEpisodes.authScope, schema.storyEpisodes.id],
        set,
      });
  }
}

export async function applyRemoteStoryLinks(
  items: StoryLink[],
  requestedScope?: string,
): Promise<void> {
  if (!items.length) return;
  const scope = await scopeOrCurrent(requestedScope);
  const db = getDb();
  for (const item of items) {
    const row = linkRow(scope, item);
    await db
      .insert(schema.storyLinks)
      .values(row)
      .onConflictDoUpdate({
        target: [schema.storyLinks.authScope, schema.storyLinks.id],
        set: { ...row, authScope: undefined, id: undefined },
      });
  }
}

export async function applyRemoteStoryGraph(
  graph: StoryGraph,
  requestedScope?: string,
  workId?: string,
): Promise<void> {
  const scope = await scopeOrCurrent(requestedScope);
  const db = getDb();
  db.transaction((tx) => {
    applyStoryGraphTx(tx, scope, graph, workId);
  });
}

function applyStoryGraphTx(
  tx: any,
  scope: AuthScope,
  graph: StoryGraph,
  workId?: string,
): void {
  if (workId) {
    tx
      .delete(schema.storyLinks)
      .where(
        and(
          eq(schema.storyLinks.authScope, scope),
          eq(schema.storyLinks.workId, workId),
        ),
      )
      .run();
    const episodeIds = graph.episodes.map((item) => item.id);
    const episodeScope = [
      eq(schema.storyEpisodes.authScope, scope),
      eq(schema.storyEpisodes.workId, workId),
    ];
    if (episodeIds.length) {
      tx
        .update(schema.storyEpisodes)
        .set({ archivedAt: nowIso() })
        .where(and(...episodeScope, notInArray(schema.storyEpisodes.id, episodeIds)))
        .run();
    } else {
      tx
        .update(schema.storyEpisodes)
        .set({ archivedAt: nowIso() })
        .where(and(...episodeScope))
        .run();
    }
  }
  for (const item of graph.episodes ?? []) {
    upsertStoryEpisodeTx(tx, episodeRow(scope, item));
  }
  for (const item of graph.links ?? []) {
    upsertStoryLinkTx(tx, linkRow(scope, item));
  }
  if (workId) {
    tx
      .update(schema.storyWorks)
      .set({ startEpisodeId: graph.start_episode_id ?? null })
      .where(
        and(
          eq(schema.storyWorks.authScope, scope),
          eq(schema.storyWorks.id, workId),
        ),
      )
      .run();
  }
}

/**
 * Apply the overview response as one authoritative SQLite snapshot.  Work,
 * episodes, links, start point, and current route are committed together;
 * transaction failure therefore leaves the previous offline snapshot intact.
 */
export async function applyRemoteStoryOverview(
  overview: StoryOverview,
  requestedScope?: string,
): Promise<void> {
  const scope = await scopeOrCurrent(requestedScope);
  const db = getDb();
  db.transaction((tx) => {
    const work: StoryWork = {
      ...overview.work,
      start_episode_id: overview.graph.start_episode_id ?? null,
      ui_state: {
        ...(overview.work.ui_state ?? {}),
        current_route: overview.current_route ?? [],
      },
    };
    upsertStoryWorkTx(tx, workRow(scope, work));
    applyStoryGraphTx(tx, scope, overview.graph, overview.work.id);
  });
}

export async function applyRemoteStoryCharacters(
  items: StoryCharacter[],
  requestedScope?: string,
): Promise<void> {
  if (!items.length) return;
  const scope = await scopeOrCurrent(requestedScope);
  const db = getDb();
  for (const item of items) {
    const row = characterRow(scope, item);
    await db
      .insert(schema.storyCharacters)
      .values(row)
      .onConflictDoUpdate({
        target: [schema.storyCharacters.authScope, schema.storyCharacters.id],
        set: { ...row, authScope: undefined, id: undefined },
      });
  }
}

/** Replace the global character pool; omitted active rows become archived. */
export async function replaceRemoteStoryCharacters(
  items: StoryCharacter[],
  requestedScope?: string,
): Promise<void> {
  const scope = await scopeOrCurrent(requestedScope);
  const db = getDb();
  const ids = items.map((item) => item.id);
  db.transaction((tx) => {
    if (ids.length) {
      tx
        .update(schema.storyCharacters)
        .set({ archivedAt: nowIso() })
        .where(
          and(
            eq(schema.storyCharacters.authScope, scope),
            notInArray(schema.storyCharacters.id, ids),
          ),
        )
        .run();
    } else {
      tx
        .update(schema.storyCharacters)
        .set({ archivedAt: nowIso() })
        .where(eq(schema.storyCharacters.authScope, scope))
        .run();
    }
    for (const item of items) upsertStoryCharacterTx(tx, characterRow(scope, item));
  });
}

export async function applyRemoteStoryWorkCharacters(
  items: StoryCharacter[],
  requestedScope?: string,
): Promise<void> {
  if (!items.length) return;
  const scope = await scopeOrCurrent(requestedScope);
  await applyRemoteStoryCharacters(items, scope);
  const db = getDb();
  for (const item of items) {
    if (!item.work_id || !item.character_id) continue;
    await db
      .insert(schema.storyWorkCharacters)
      .values({
        authScope: scope,
        workId: item.work_id,
        characterId: item.character_id,
        roleNote: item.role_note ?? null,
        position: item.position ?? 0,
      })
      .onConflictDoUpdate({
        target: [
          schema.storyWorkCharacters.authScope,
          schema.storyWorkCharacters.workId,
          schema.storyWorkCharacters.characterId,
        ],
        set: {
          roleNote: item.role_note ?? null,
          position: item.position ?? 0,
        },
      });
  }
}

export async function replaceLocalStoryWorkCharacters(
  workId: string,
  items: StoryCharacter[],
  requestedScope?: string,
): Promise<void> {
  const scope = await scopeOrCurrent(requestedScope);
  const db = getDb();
  await db
    .delete(schema.storyWorkCharacters)
    .where(
      and(
        eq(schema.storyWorkCharacters.authScope, scope),
        eq(schema.storyWorkCharacters.workId, workId),
      ),
    );
  await applyRemoteStoryWorkCharacters(items, scope);
}

/** Replace a work character association set, including the empty response. */
export async function replaceRemoteStoryWorkCharacters(
  workId: string,
  items: StoryCharacter[],
  requestedScope?: string,
): Promise<void> {
  const scope = await scopeOrCurrent(requestedScope);
  const db = getDb();
  db.transaction((tx) => {
    tx
      .delete(schema.storyWorkCharacters)
      .where(
        and(
          eq(schema.storyWorkCharacters.authScope, scope),
          eq(schema.storyWorkCharacters.workId, workId),
        ),
      )
      .run();
    for (const item of items) {
      if (!item.character_id) continue;
      upsertStoryCharacterTx(tx, characterRow(scope, item));
      upsertStoryWorkCharacterTx(tx, storyWorkCharacterRow(scope, item));
    }
  });
}

export async function applyRemoteStoryRulebooks(
  items: StoryRulebook[],
  requestedScope?: string,
): Promise<void> {
  if (!items.length) return;
  const scope = await scopeOrCurrent(requestedScope);
  const db = getDb();
  for (const item of items) {
    const row = rulebookRow(scope, item);
    await db
      .insert(schema.storyRulebooks)
      .values(row)
      .onConflictDoUpdate({
        target: [schema.storyRulebooks.authScope, schema.storyRulebooks.id],
        set: { ...row, authScope: undefined, id: undefined },
      });
  }
}

/** Replace the global rulebook pool; omitted active rows become archived. */
export async function replaceRemoteStoryRulebooks(
  items: StoryRulebook[],
  requestedScope?: string,
): Promise<void> {
  const scope = await scopeOrCurrent(requestedScope);
  const db = getDb();
  const ids = items.map((item) => item.id);
  db.transaction((tx) => {
    if (ids.length) {
      tx
        .update(schema.storyRulebooks)
        .set({ archivedAt: nowIso() })
        .where(
          and(
            eq(schema.storyRulebooks.authScope, scope),
            notInArray(schema.storyRulebooks.id, ids),
          ),
        )
        .run();
    } else {
      tx
        .update(schema.storyRulebooks)
        .set({ archivedAt: nowIso() })
        .where(eq(schema.storyRulebooks.authScope, scope))
        .run();
    }
    for (const item of items) upsertStoryRulebookTx(tx, rulebookRow(scope, item));
  });
}

export async function applyRemoteStoryWorkRulebooks(
  items: StoryRulebook[],
  requestedScope?: string,
): Promise<void> {
  if (!items.length) return;
  const scope = await scopeOrCurrent(requestedScope);
  await applyRemoteStoryRulebooks(items, scope);
  const db = getDb();
  for (const item of items) {
    if (!item.work_id || !item.rulebook_id) continue;
    await db
      .insert(schema.storyWorkRulebooks)
      .values({
        authScope: scope,
        workId: item.work_id,
        rulebookId: item.rulebook_id,
        enabled: item.enabled ?? true,
        position: item.position ?? 0,
      })
      .onConflictDoUpdate({
        target: [
          schema.storyWorkRulebooks.authScope,
          schema.storyWorkRulebooks.workId,
          schema.storyWorkRulebooks.rulebookId,
        ],
        set: { enabled: item.enabled ?? true, position: item.position ?? 0 },
      });
  }
}

/** Replace a work rulebook association set, including the empty response. */
export async function replaceRemoteStoryWorkRulebooksForWork(
  workId: string,
  items: StoryRulebook[],
  requestedScope?: string,
): Promise<void> {
  const scope = await scopeOrCurrent(requestedScope);
  const db = getDb();
  db.transaction((tx) => {
    tx
      .delete(schema.storyWorkRulebooks)
      .where(
        and(
          eq(schema.storyWorkRulebooks.authScope, scope),
          eq(schema.storyWorkRulebooks.workId, workId),
        ),
      )
      .run();
    for (const item of items) {
      if (!item.rulebook_id) continue;
      upsertStoryRulebookTx(tx, rulebookRow(scope, item));
      upsertStoryWorkRulebookTx(tx, storyWorkRulebookRow(scope, item));
    }
  });
}

export async function applyRemoteStoryNotes(
  items: StoryNote[],
  requestedScope?: string,
): Promise<void> {
  if (!items.length) return;
  const scope = await scopeOrCurrent(requestedScope);
  const db = getDb();
  for (const item of items) {
    const row = {
      authScope: scope,
      id: item.id,
      workId: item.work_id,
      title: item.title,
      content: item.content ?? null,
      aiMode: item.ai_mode || "keyword",
      keywords: item.keywords ?? [],
      position: item.position ?? 0,
      createdAt: item.created_at ?? null,
      updatedAt: item.updated_at ?? null,
    };
    await db
      .insert(schema.storyNotes)
      .values(row)
      .onConflictDoUpdate({
        target: [schema.storyNotes.authScope, schema.storyNotes.id],
        set: { ...row, authScope: undefined, id: undefined },
      });
  }
}

/** Replace notes for a work; notes omitted from the full response are removed. */
export async function replaceRemoteStoryNotes(
  workId: string,
  items: StoryNote[],
  requestedScope?: string,
): Promise<void> {
  const scope = await scopeOrCurrent(requestedScope);
  const db = getDb();
  const ids = items.map((item) => item.id);
  db.transaction((tx) => {
    if (ids.length) {
      tx
        .delete(schema.storyNotes)
        .where(
          and(
            eq(schema.storyNotes.authScope, scope),
            eq(schema.storyNotes.workId, workId),
            notInArray(schema.storyNotes.id, ids),
          ),
        )
        .run();
    } else {
      tx
        .delete(schema.storyNotes)
        .where(
          and(
            eq(schema.storyNotes.authScope, scope),
            eq(schema.storyNotes.workId, workId),
          ),
        )
        .run();
    }
    for (const item of items) {
      upsertStoryNoteTx(tx, {
        authScope: scope,
        id: item.id,
        workId,
        title: item.title,
        content: item.content ?? null,
        aiMode: item.ai_mode || "keyword",
        keywords: item.keywords ?? [],
        position: item.position ?? 0,
        createdAt: item.created_at ?? null,
        updatedAt: item.updated_at ?? null,
      });
    }
  });
}

export async function applyRemoteStoryRevisions(
  items: StoryEpisodeRevision[],
  requestedScope?: string,
): Promise<void> {
  if (!items.length) return;
  const scope = await scopeOrCurrent(requestedScope);
  const db = getDb();
  for (const item of items) {
    const row = {
      authScope: scope,
      id: item.id,
      episodeId: item.episode_id,
      revNo: item.rev_no,
      title: item.title ?? null,
      plot: item.plot ?? null,
      body: item.body ?? null,
      message: item.message ?? null,
      origin: item.origin,
      bodySha256: item.body_sha256,
      charCount: item.char_count ?? 0,
      createdBy: item.created_by,
      createdAt: item.created_at ?? null,
    };
    await db
      .insert(schema.storyEpisodeRevisions)
      .values(row)
      .onConflictDoUpdate({
        target: [schema.storyEpisodeRevisions.authScope, schema.storyEpisodeRevisions.id],
        set: { ...row, authScope: undefined, id: undefined },
      });
  }
}

export async function applyRemoteStoryJob(
  item: StoryJob,
  requestedScope?: string,
): Promise<void> {
  const scope = await scopeOrCurrent(requestedScope);
  const db = getDb();
  const row = {
    authScope: scope,
    id: item.id,
    workId: item.work_id,
    kind: item.kind,
    payload: item.payload ?? {},
    status: item.status,
    progress: item.progress ?? {},
    result: item.result ?? null,
    error: item.error ?? null,
    createdAt: item.created_at ?? null,
    startedAt: item.started_at ?? null,
    finishedAt: item.finished_at ?? null,
  };
  await db
    .insert(schema.storyGenerationJobs)
    .values(row)
    .onConflictDoUpdate({
      target: [schema.storyGenerationJobs.authScope, schema.storyGenerationJobs.id],
      set: { ...row, authScope: undefined, id: undefined },
    });
}

export async function applyRemoteStoryWritingSession(
  item: StoryWritingSession,
  requestedScope?: string,
): Promise<void> {
  const scope = await scopeOrCurrent(requestedScope);
  const db = getDb();
  const row = {
    authScope: scope,
    id: item.id,
    workId: item.work_id,
    episodeId: item.episode_id ?? null,
    conversationSessionId: item.conversation_session_id ?? null,
    createdAt: item.created_at ?? null,
    updatedAt: item.updated_at ?? null,
  };
  await db
    .insert(schema.storyWritingSessions)
    .values(row)
    .onConflictDoUpdate({
      target: [schema.storyWritingSessions.authScope, schema.storyWritingSessions.id],
      set: { ...row, authScope: undefined, id: undefined },
    });
}

export async function applyRemoteStoryBody(
  episode: StoryEpisode,
  requestedScope?: string,
): Promise<void> {
  await applyRemoteStoryEpisodes([episode], requestedScope);
}

export async function markStoryWorkArchived(
  workId: string,
  archivedAt: string | null = nowIso(),
  requestedScope?: string,
): Promise<void> {
  const scope = await scopeOrCurrent(requestedScope);
  await getDb()
    .update(schema.storyWorks)
    .set({ archivedAt, updatedAt: archivedAt ?? nowIso() })
    .where(
      and(eq(schema.storyWorks.authScope, scope), eq(schema.storyWorks.id, workId)),
    );
}

export async function markStoryEpisodeArchived(
  episodeId: string,
  archivedAt: string | null = nowIso(),
  requestedScope?: string,
): Promise<void> {
  const scope = await scopeOrCurrent(requestedScope);
  await getDb()
    .update(schema.storyEpisodes)
    .set({ archivedAt, updatedAt: archivedAt ?? nowIso() })
    .where(
      and(
        eq(schema.storyEpisodes.authScope, scope),
        eq(schema.storyEpisodes.id, episodeId),
      ),
    );
}

export async function markStoryCharacterArchived(
  characterId: string,
  archivedAt: string | null = nowIso(),
  requestedScope?: string,
): Promise<void> {
  const scope = await scopeOrCurrent(requestedScope);
  await getDb()
    .update(schema.storyCharacters)
    .set({ archivedAt, updatedAt: archivedAt ?? nowIso() })
    .where(
      and(
        eq(schema.storyCharacters.authScope, scope),
        eq(schema.storyCharacters.id, characterId),
      ),
    );
}

export async function markStoryRulebookArchived(
  rulebookId: string,
  archivedAt: string | null = nowIso(),
  requestedScope?: string,
): Promise<void> {
  const scope = await scopeOrCurrent(requestedScope);
  await getDb()
    .update(schema.storyRulebooks)
    .set({ archivedAt, updatedAt: archivedAt ?? nowIso() })
    .where(
      and(
        eq(schema.storyRulebooks.authScope, scope),
        eq(schema.storyRulebooks.id, rulebookId),
      ),
    );
}

export async function removeStoryNote(
  noteId: string,
  requestedScope?: string,
): Promise<void> {
  const scope = await scopeOrCurrent(requestedScope);
  await getDb()
    .delete(schema.storyNotes)
    .where(and(eq(schema.storyNotes.authScope, scope), eq(schema.storyNotes.id, noteId)));
}

// ---------- Offline reads ----------

export async function listStoryWorks(
  requestedScope?: string,
  options: { includeArchived?: boolean } = {},
): Promise<StoryWork[]> {
  const scope = await scopeOrCurrent(requestedScope);
  const db = getDb();
  const rows = await db
    .select()
    .from(schema.storyWorks)
    .where(eq(schema.storyWorks.authScope, scope))
    .orderBy(desc(schema.storyWorks.updatedAt));
  return rows
    .filter((row) => options.includeArchived || row.archivedAt == null)
    .map(workFromRow);
}

export async function getStoryWork(
  workId: string,
  requestedScope?: string,
): Promise<StoryWork | null> {
  const scope = await scopeOrCurrent(requestedScope);
  const row = await getDb()
    .select()
    .from(schema.storyWorks)
    .where(and(eq(schema.storyWorks.authScope, scope), eq(schema.storyWorks.id, workId)))
    .then((rows) => rows[0]);
  return row ? workFromRow(row) : null;
}

export async function listStoryEpisodes(
  workId: string,
  requestedScope?: string,
  options: { includeArchived?: boolean } = {},
): Promise<StoryEpisode[]> {
  const scope = await scopeOrCurrent(requestedScope);
  const rows = await getDb()
    .select()
    .from(schema.storyEpisodes)
    .where(and(eq(schema.storyEpisodes.authScope, scope), eq(schema.storyEpisodes.workId, workId)))
    .orderBy(asc(schema.storyEpisodes.sortHint), asc(schema.storyEpisodes.createdAt));
  return rows
    .filter((row) => options.includeArchived || row.archivedAt == null)
    .map(episodeFromRow);
}

export async function getStoryEpisode(
  episodeId: string,
  requestedScope?: string,
): Promise<StoryEpisode | null> {
  const scope = await scopeOrCurrent(requestedScope);
  const row = await getDb()
    .select()
    .from(schema.storyEpisodes)
    .where(and(eq(schema.storyEpisodes.authScope, scope), eq(schema.storyEpisodes.id, episodeId)))
    .then((rows) => rows[0]);
  return row ? episodeFromRow(row) : null;
}

/**
 * Recover the old AsyncStorage writing-session draft into an auth-scoped
 * canonical recovery row.
 *
 * The legacy key is removed only after the SQLite transaction commits.  Work
 * ownership is proven from the token-derived user id and the canonical local
 * Story cache; an absent/mismatched work is therefore fail-closed.  A legacy
 * targetSceneId has no canonical Scene entity, so it is copied only when the
 * same id is an active episode of the verified work; the raw payload always
 * retains the original unknown value.
 */
export async function migrateLegacyStoryWritingDraft(
  workId: string,
): Promise<LegacyStoryWritingDraftMigrationResult> {
  const legacyKey = legacyWritingDraftKey(workId);
  let raw: string | null;
  try {
    raw = await AsyncStorage.getItem(legacyKey);
  } catch (error) {
    return {
      status: "failed",
      workId,
      legacyKey,
      reason: error instanceof Error ? error.message : "legacy read failed",
    };
  }
  if (raw == null) return { status: "not_found", workId, legacyKey };

  const scope = await resolveStoryAuthScope();
  let work: StoryWork | null;
  try {
    work = await getStoryWork(workId, scope);
  } catch (error) {
    return {
      status: "failed",
      workId,
      legacyKey,
      reason: error instanceof Error ? error.message : "canonical work lookup failed",
    };
  }
  const ownerId = ownerIdForAuthScope(scope);
  if (!work || work.archived_at != null || !ownerId || work.user_id !== ownerId) {
    return {
      status: "wrong_auth",
      workId,
      legacyKey,
      reason: "canonical work ownership could not be proven",
    };
  }

  const payload = parseLegacyWritingDraft(raw);
  if (!payload) {
    return { status: "invalid_payload", workId, legacyKey };
  }

  let targetEpisodeId: string | null = null;
  if (payload.targetEpisodeId != null && payload.targetEpisodeId !== "") {
    if (typeof payload.targetEpisodeId !== "string") {
      return { status: "invalid_target_episode", workId, legacyKey };
    }
    let episode: StoryEpisode | null;
    try {
      episode = await getStoryEpisode(payload.targetEpisodeId, scope);
    } catch (error) {
      return {
        status: "failed",
        workId,
        legacyKey,
        reason: error instanceof Error ? error.message : "canonical episode lookup failed",
      };
    }
    if (!episode || episode.work_id !== workId || episode.archived_at != null) {
      return { status: "invalid_target_episode", workId, legacyKey };
    }
    targetEpisodeId = payload.targetEpisodeId;
  }

  let targetSceneId: string | null = null;
  if (typeof payload.targetSceneId === "string" && payload.targetSceneId) {
    let sameIdEpisode: StoryEpisode | null;
    try {
      sameIdEpisode = await getStoryEpisode(payload.targetSceneId, scope);
    } catch (error) {
      return {
        status: "failed",
        workId,
        legacyKey,
        reason: error instanceof Error ? error.message : "canonical scene lookup failed",
      };
    }
    if (
      sameIdEpisode
      && sameIdEpisode.work_id === workId
      && sameIdEpisode.archived_at == null
    ) {
      targetSceneId = payload.targetSceneId;
    }
  }

  const timestamp = nowIso();
  const row = {
    authScope: scope,
    legacyKey,
    workId,
    targetEpisodeId,
    targetSceneId,
    prompt: typeof payload.prompt === "string" ? payload.prompt : null,
    rawPayload: payload,
    status: "recovered",
    createdAt: timestamp,
    updatedAt: timestamp,
  };
  try {
    const db = getDb();
    db.transaction((tx) => {
      tx
        .insert(schema.storyLegacyWritingDrafts)
        .values(row)
        .onConflictDoUpdate({
          target: [
            schema.storyLegacyWritingDrafts.authScope,
            schema.storyLegacyWritingDrafts.legacyKey,
          ],
          set: {
            workId,
            targetEpisodeId,
            targetSceneId,
            prompt: row.prompt,
            rawPayload: payload,
            status: row.status,
            updatedAt: row.updatedAt,
          },
        })
        .run();
    });
    await AsyncStorage.removeItem(legacyKey);
  } catch (error) {
    return {
      status: "failed",
      workId,
      legacyKey,
      targetEpisodeId,
      targetSceneId,
      reason: error instanceof Error ? error.message : "legacy migration failed",
    };
  }
  return {
    status: "migrated",
    workId,
    legacyKey,
    targetEpisodeId,
    targetSceneId,
  };
}

export async function getStoryGraph(
  workId: string,
  requestedScope?: string,
): Promise<StoryGraph | null> {
  const scope = await scopeOrCurrent(requestedScope);
  const [work, episodes, links] = await Promise.all([
    getStoryWork(workId, scope),
    listStoryEpisodes(workId, scope),
    getDb()
      .select()
      .from(schema.storyLinks)
      .where(and(eq(schema.storyLinks.authScope, scope), eq(schema.storyLinks.workId, workId)))
      .orderBy(asc(schema.storyLinks.position))
      .then((rows) => rows.map(linkFromRow)),
  ]);
  if (!work) return null;
  return {
    episodes,
    links,
    start_episode_id: work.start_episode_id,
  };
}

export async function getStoryOverview(
  workId: string,
  requestedScope?: string,
): Promise<StoryOverview | null> {
  const work = await getStoryWork(workId, requestedScope);
  const graph = await getStoryGraph(workId, requestedScope);
  if (!work || !graph) return null;
  const currentRoute = Array.isArray(work.ui_state.current_route)
    ? work.ui_state.current_route.filter((item): item is string => typeof item === "string")
    : [];
  return { work, graph, current_route: currentRoute };
}

export async function listStoryCharacters(
  requestedScope?: string,
): Promise<StoryCharacter[]> {
  const scope = await scopeOrCurrent(requestedScope);
  const rows = await getDb()
    .select()
    .from(schema.storyCharacters)
    .where(eq(schema.storyCharacters.authScope, scope))
    .orderBy(asc(schema.storyCharacters.name));
  return rows.filter((row) => row.archivedAt == null).map(characterFromRow);
}

export async function getStoryCharacter(
  characterId: string,
  requestedScope?: string,
): Promise<StoryCharacter | null> {
  const scope = await scopeOrCurrent(requestedScope);
  const row = await getDb()
    .select()
    .from(schema.storyCharacters)
    .where(and(eq(schema.storyCharacters.authScope, scope), eq(schema.storyCharacters.id, characterId)))
    .then((rows) => rows[0]);
  return row && row.archivedAt == null ? characterFromRow(row) : null;
}

export async function listStoryWorkCharacters(
  workId: string,
  requestedScope?: string,
): Promise<StoryCharacter[]> {
  const scope = await scopeOrCurrent(requestedScope);
  const db = getDb();
  const joins = await db
    .select()
    .from(schema.storyWorkCharacters)
    .where(and(eq(schema.storyWorkCharacters.authScope, scope), eq(schema.storyWorkCharacters.workId, workId)))
    .orderBy(asc(schema.storyWorkCharacters.position));
  const characters = await db
    .select()
    .from(schema.storyCharacters)
    .where(eq(schema.storyCharacters.authScope, scope));
  const byId = new Map(characters.map((row) => [row.id, row]));
  return joins.flatMap((join) => {
    const character = byId.get(join.characterId);
    return character
      ? [{ ...characterFromRow(character), ...workCharacterFromRow(join) }]
      : [];
  });
}

export async function listStoryRulebooks(
  requestedScope?: string,
): Promise<StoryRulebook[]> {
  const scope = await scopeOrCurrent(requestedScope);
  const rows = await getDb()
    .select()
    .from(schema.storyRulebooks)
    .where(eq(schema.storyRulebooks.authScope, scope))
    .orderBy(asc(schema.storyRulebooks.name));
  return rows.filter((row) => row.archivedAt == null).map(rulebookFromRow);
}

export async function getStoryRulebook(
  rulebookId: string,
  requestedScope?: string,
): Promise<StoryRulebook | null> {
  const scope = await scopeOrCurrent(requestedScope);
  const row = await getDb()
    .select()
    .from(schema.storyRulebooks)
    .where(and(eq(schema.storyRulebooks.authScope, scope), eq(schema.storyRulebooks.id, rulebookId)))
    .then((rows) => rows[0]);
  return row && row.archivedAt == null ? rulebookFromRow(row) : null;
}

export async function listStoryWorkRulebooks(
  workId: string,
  requestedScope?: string,
): Promise<StoryRulebook[]> {
  const scope = await scopeOrCurrent(requestedScope);
  const db = getDb();
  const joins = await db
    .select()
    .from(schema.storyWorkRulebooks)
    .where(and(eq(schema.storyWorkRulebooks.authScope, scope), eq(schema.storyWorkRulebooks.workId, workId)))
    .orderBy(asc(schema.storyWorkRulebooks.position));
  const books = await db
    .select()
    .from(schema.storyRulebooks)
    .where(eq(schema.storyRulebooks.authScope, scope));
  const byId = new Map(books.map((row) => [row.id, row]));
  return joins.flatMap((join) => {
    const book = byId.get(join.rulebookId);
    return book ? [{ ...rulebookFromRow(book), ...workRulebookFromRow(join) }] : [];
  });
}

/** Cache the association-only response returned by PUT /works/{id}/rulebooks. */
export async function applyRemoteStoryWorkRulebookEntries(
  items: StoryWorkRulebook[],
  workId?: string,
  requestedScope?: string,
): Promise<void> {
  const scope = await scopeOrCurrent(requestedScope);
  const db = getDb();
  const workIds = workId
    ? [workId]
    : [...new Set(items.map((item) => item.work_id).filter(Boolean))];
  if (!workIds.length) return;
  for (const workId of workIds) {
    await db
      .delete(schema.storyWorkRulebooks)
      .where(
        and(
          eq(schema.storyWorkRulebooks.authScope, scope),
          eq(schema.storyWorkRulebooks.workId, workId),
        ),
      );
  }
  for (const item of items) {
    if (!item.work_id || !item.rulebook_id) continue;
    await db
      .insert(schema.storyWorkRulebooks)
      .values({
        authScope: scope,
        workId: item.work_id,
        rulebookId: item.rulebook_id,
        enabled: item.enabled ?? true,
        position: item.position ?? 0,
      })
      .onConflictDoUpdate({
        target: [
          schema.storyWorkRulebooks.authScope,
          schema.storyWorkRulebooks.workId,
          schema.storyWorkRulebooks.rulebookId,
        ],
        set: { enabled: item.enabled ?? true, position: item.position ?? 0 },
      });
  }
}

/** Replace an association list even when the server returns an empty array. */
export async function replaceRemoteStoryWorkRulebooks(
  workId: string,
  items: StoryWorkRulebook[],
  requestedScope?: string,
): Promise<void> {
  await applyRemoteStoryWorkRulebookEntries(items, workId, requestedScope);
}

export async function listStoryNotes(
  workId: string,
  requestedScope?: string,
): Promise<StoryNote[]> {
  const scope = await scopeOrCurrent(requestedScope);
  const rows = await getDb()
    .select()
    .from(schema.storyNotes)
    .where(and(eq(schema.storyNotes.authScope, scope), eq(schema.storyNotes.workId, workId)))
    .orderBy(asc(schema.storyNotes.position), asc(schema.storyNotes.createdAt));
  return rows.map(noteFromRow);
}

export async function getStoryNote(
  noteId: string,
  requestedScope?: string,
): Promise<StoryNote | null> {
  const scope = await scopeOrCurrent(requestedScope);
  const row = await getDb()
    .select()
    .from(schema.storyNotes)
    .where(and(eq(schema.storyNotes.authScope, scope), eq(schema.storyNotes.id, noteId)))
    .then((rows) => rows[0]);
  return row ? noteFromRow(row) : null;
}

export async function listStoryRevisions(
  episodeId: string,
  requestedScope?: string,
): Promise<StoryEpisodeRevision[]> {
  const scope = await scopeOrCurrent(requestedScope);
  const rows = await getDb()
    .select()
    .from(schema.storyEpisodeRevisions)
    .where(and(eq(schema.storyEpisodeRevisions.authScope, scope), eq(schema.storyEpisodeRevisions.episodeId, episodeId)))
    .orderBy(desc(schema.storyEpisodeRevisions.revNo));
  return rows.map(revisionFromRow);
}

export async function getStoryRevision(
  episodeId: string,
  revNo: number,
  requestedScope?: string,
): Promise<StoryEpisodeRevision | null> {
  const scope = await scopeOrCurrent(requestedScope);
  const row = await getDb()
    .select()
    .from(schema.storyEpisodeRevisions)
    .where(
      and(
        eq(schema.storyEpisodeRevisions.authScope, scope),
        eq(schema.storyEpisodeRevisions.episodeId, episodeId),
        eq(schema.storyEpisodeRevisions.revNo, revNo),
      ),
    )
    .then((rows) => rows[0]);
  return row ? revisionFromRow(row) : null;
}

export async function listStoryJobs(
  workId: string,
  requestedScope?: string,
): Promise<StoryJob[]> {
  const scope = await scopeOrCurrent(requestedScope);
  const rows = await getDb()
    .select()
    .from(schema.storyGenerationJobs)
    .where(and(eq(schema.storyGenerationJobs.authScope, scope), eq(schema.storyGenerationJobs.workId, workId)))
    .orderBy(desc(schema.storyGenerationJobs.createdAt));
  return rows.map(jobFromRow);
}

export async function getStoryJob(
  jobId: string,
  requestedScope?: string,
): Promise<StoryJob | null> {
  const scope = await scopeOrCurrent(requestedScope);
  const row = await getDb()
    .select()
    .from(schema.storyGenerationJobs)
    .where(and(eq(schema.storyGenerationJobs.authScope, scope), eq(schema.storyGenerationJobs.id, jobId)))
    .then((rows) => rows[0]);
  return row ? jobFromRow(row) : null;
}

export async function getStoryWritingSessionByConversation(
  conversationSessionId: string,
  requestedScope?: string,
): Promise<StoryWritingSession | null> {
  const scope = await scopeOrCurrent(requestedScope);
  const row = await getDb()
    .select()
    .from(schema.storyWritingSessions)
    .where(and(eq(schema.storyWritingSessions.authScope, scope), eq(schema.storyWritingSessions.conversationSessionId, conversationSessionId)))
    .orderBy(desc(schema.storyWritingSessions.updatedAt))
    .then((rows) => rows[0]);
  return row ? writingSessionFromRow(row) : null;
}

export async function getStoryWritingSession(
  sessionId: string,
  requestedScope?: string,
): Promise<StoryWritingSession | null> {
  const scope = await scopeOrCurrent(requestedScope);
  const row = await getDb()
    .select()
    .from(schema.storyWritingSessions)
    .where(and(eq(schema.storyWritingSessions.authScope, scope), eq(schema.storyWritingSessions.id, sessionId)))
    .then((rows) => rows[0]);
  return row ? writingSessionFromRow(row) : null;
}

// ---------- Durable manuscript drafts ----------

export interface SaveStoryDraftInput {
  episodeId: string;
  body: string;
  expectedEtag?: string | null;
  serverSnapshot?: StoryEpisode | null;
  conflict?: boolean;
}

export async function saveStoryLocalDraft(
  input: SaveStoryDraftInput,
  requestedScope?: string,
): Promise<StoryLocalDraft> {
  const scope = await scopeOrCurrent(requestedScope);
  const db = getDb();
  const timestamp = nowIso();
  const previous = await getStoryLocalDraft(input.episodeId, scope);
  // A subsequent local edit must not discard the server snapshot captured by
  // an earlier 409.  It remains available until the canonical PUT succeeds.
  const serverSnapshot = input.serverSnapshot === undefined
    ? previous?.server_snapshot ?? null
    : input.serverSnapshot;
  const row = {
    authScope: scope,
    episodeId: input.episodeId,
    body: input.body,
    expectedEtag: input.expectedEtag ?? null,
    serverSnapshot,
    conflictStatus: input.conflict ? "conflict" : "draft",
    createdAt: previous?.created_at ?? timestamp,
    updatedAt: timestamp,
  } as const;
  await db
    .insert(schema.storyLocalDrafts)
    .values(row)
    .onConflictDoUpdate({
      target: [schema.storyLocalDrafts.authScope, schema.storyLocalDrafts.episodeId],
      set: {
        body: row.body,
        expectedEtag: row.expectedEtag,
        serverSnapshot: row.serverSnapshot,
        conflictStatus: row.conflictStatus,
        updatedAt: row.updatedAt,
      },
    });
  const result = await getDb()
    .select()
    .from(schema.storyLocalDrafts)
    .where(and(eq(schema.storyLocalDrafts.authScope, scope), eq(schema.storyLocalDrafts.episodeId, input.episodeId)))
    .then((rows) => rows[0]);
  if (!result) {
    // Test doubles and a broken SQLite transaction should not silently drop a
    // manuscript; return the exact durable value the caller asked to retain.
    return {
      auth_scope: scope,
      episode_id: input.episodeId,
      body: input.body,
      expected_etag: input.expectedEtag ?? null,
      server_snapshot: serverSnapshot,
      conflict_status: input.conflict ? "conflict" : "draft",
      created_at: previous?.created_at ?? timestamp,
      updated_at: timestamp,
    };
  }
  return draftFromRow(result);
}

export async function getStoryLocalDraft(
  episodeId: string,
  requestedScope?: string,
): Promise<StoryLocalDraft | null> {
  const scope = await scopeOrCurrent(requestedScope);
  const row = await getDb()
    .select()
    .from(schema.storyLocalDrafts)
    .where(and(eq(schema.storyLocalDrafts.authScope, scope), eq(schema.storyLocalDrafts.episodeId, episodeId)))
    .then((rows) => rows[0]);
  return row ? draftFromRow(row) : null;
}

export async function listStoryLocalDrafts(
  requestedScope?: string,
): Promise<StoryLocalDraft[]> {
  const scope = await scopeOrCurrent(requestedScope);
  const rows = await getDb()
    .select()
    .from(schema.storyLocalDrafts)
    .where(eq(schema.storyLocalDrafts.authScope, scope))
    .orderBy(desc(schema.storyLocalDrafts.updatedAt));
  return rows.map(draftFromRow);
}

export async function clearStoryLocalDraft(
  episodeId: string,
  requestedScope?: string,
): Promise<void> {
  const scope = await scopeOrCurrent(requestedScope);
  await getDb()
    .delete(schema.storyLocalDrafts)
    .where(and(eq(schema.storyLocalDrafts.authScope, scope), eq(schema.storyLocalDrafts.episodeId, episodeId)));
}

/** Update the cached episode body after a successful canonical PUT. */
export async function applyStoryBodyToCache(
  episodeId: string,
  body: string,
  bodyEtag: string | null | undefined,
  charCount: number,
  currentRevNo: number,
  requestedScope?: string,
): Promise<void> {
  const scope = await scopeOrCurrent(requestedScope);
  await getDb()
    .update(schema.storyEpisodes)
    .set({ body, bodyEtag: bodyEtag ?? null, charCount, currentRevNo, updatedAt: nowIso() })
    .where(and(eq(schema.storyEpisodes.authScope, scope), eq(schema.storyEpisodes.id, episodeId)));
}

export const storyRepo = {
  resolveAuthScope: resolveStoryAuthScope,
  applyRemoteWorks: applyRemoteStoryWorks,
  replaceRemoteWorks: replaceRemoteStoryWorks,
  applyRemoteEpisodes: applyRemoteStoryEpisodes,
  applyRemoteLinks: applyRemoteStoryLinks,
  applyRemoteGraph: applyRemoteStoryGraph,
  applyRemoteCharacters: applyRemoteStoryCharacters,
  replaceRemoteCharacters: replaceRemoteStoryCharacters,
  applyRemoteWorkCharacters: applyRemoteStoryWorkCharacters,
  replaceWorkCharacters: replaceLocalStoryWorkCharacters,
  replaceRemoteWorkCharacters: replaceRemoteStoryWorkCharacters,
  applyRemoteRulebooks: applyRemoteStoryRulebooks,
  replaceRemoteRulebooks: replaceRemoteStoryRulebooks,
  applyRemoteWorkRulebooks: applyRemoteStoryWorkRulebooks,
  applyRemoteWorkRulebookEntries: applyRemoteStoryWorkRulebookEntries,
  replaceWorkRulebooks: replaceRemoteStoryWorkRulebooks,
  replaceRemoteWorkRulebooks: replaceRemoteStoryWorkRulebooksForWork,
  applyRemoteNotes: applyRemoteStoryNotes,
  replaceRemoteNotes: replaceRemoteStoryNotes,
  applyRemoteRevisions: applyRemoteStoryRevisions,
  applyRemoteJob: applyRemoteStoryJob,
  applyRemoteWritingSession: applyRemoteStoryWritingSession,
  applyRemoteBody: applyRemoteStoryBody,
  applyRemoteOverview: applyRemoteStoryOverview,
  markWorkArchived: markStoryWorkArchived,
  markEpisodeArchived: markStoryEpisodeArchived,
  markCharacterArchived: markStoryCharacterArchived,
  markRulebookArchived: markStoryRulebookArchived,
  removeNote: removeStoryNote,
  listWorks: listStoryWorks,
  getWork: getStoryWork,
  listEpisodes: listStoryEpisodes,
  getEpisode: getStoryEpisode,
  getGraph: getStoryGraph,
  getOverview: getStoryOverview,
  listCharacters: listStoryCharacters,
  getCharacter: getStoryCharacter,
  listWorkCharacters: listStoryWorkCharacters,
  listRulebooks: listStoryRulebooks,
  getRulebook: getStoryRulebook,
  listWorkRulebooks: listStoryWorkRulebooks,
  listNotes: listStoryNotes,
  getNote: getStoryNote,
  listRevisions: listStoryRevisions,
  getRevision: getStoryRevision,
  getJob: getStoryJob,
  listJobs: listStoryJobs,
  getWritingSessionByConversation: getStoryWritingSessionByConversation,
  getWritingSession: getStoryWritingSession,
  saveDraft: saveStoryLocalDraft,
  getDraft: getStoryLocalDraft,
  listDrafts: listStoryLocalDrafts,
  clearDraft: clearStoryLocalDraft,
  applyBodyToCache: applyStoryBodyToCache,
  migrateLegacyWritingDraft: migrateLegacyStoryWritingDraft,
};

export default storyRepo;
export const storyRepository = storyRepo;
