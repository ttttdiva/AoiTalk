import { eq } from "drizzle-orm";
import { getDb, schema } from "../db/client";
import {
  hasPendingOutbox,
  listPendingOutbox,
  markOutboxConflict,
  markOutboxError,
  rebaseOutboxOp,
  recordOutboxServerSnapshot,
  removeOutboxOpIfSnapshot,
  removeOutboxOp,
} from "../repositories/outbox";
import { applyRemoteTasks, applyTaskTombstones } from "../repositories/tasks";
import {
  applyProjectTombstones,
  applyRemoteProjects,
} from "../repositories/projects";
import {
  applyOccurrenceTombstones,
  applyRemoteOccurrences,
} from "../repositories/occurrences";
import {
  applyConversationMessageTombstones,
  applyConversationSessionTombstones,
  applyRemoteConversationMessages,
  applyRemoteConversationSessions,
  reconcileConversationSessionsWithServer,
} from "../repositories/conversations";
import {
  applyRemoteTimeEntries,
  applyTimeEntryTombstones,
} from "../repositories/timeEntries";
import {
  applyRecordFieldTombstones,
  applyRecordRowTombstones,
  applyRecordTableTombstones,
  applyRemoteRecordFields,
  applyRemoteRecordRows,
  applyRemoteRecordTables,
} from "../repositories/records";
import {
  applyRemoteScenarioCharacters,
  applyRemoteScenarioEpisodes,
  applyRemoteScenarios,
  applyRemoteScenarioScenes,
  applyScenarioTombstones,
  reconcileScenarioCharactersWithServer,
  reconcileScenarioEpisodesWithServer,
  reconcileScenariosWithServer,
  reconcileScenarioScenesWithServer,
} from "../repositories/scenarios";
import {
  applyDocsNodeTombstones,
  applyRemoteDocsEdges,
  applyRemoteDocsFieldValues,
  applyRemoteDocsFields,
  applyRemoteDocsNodeSupertags,
  applyRemoteDocsNodes,
  applyRemoteDocsPlacements,
  applyRemoteDocsSupertagFields,
  applyRemoteDocsSupertags,
  deleteLocalDocsFieldValue,
  deleteLocalDocsNodeSupertag,
  reconcileDocsNodesWithServer,
} from "../repositories/docs";
import { flushPendingConversations } from "../repositories/conversations";
import type {
  ConversationMessage,
  ConversationSession,
  DocsEdge,
  DocsField,
  DocsFieldValue,
  DocsNode,
  DocsNodePlacement,
  DocsNodeSupertag,
  DocsSupertag,
  DocsSupertagField,
  Project,
  RecordField,
  RecordRow,
  RecordTable,
  Task,
  TaskOccurrence,
  TimeEntry,
  Scenario,
  ScenarioCharacter,
  ScenarioEpisode,
  ScenarioScene,
} from "../types/api";
import { useNetworkStore } from "../stores/network";
import { getCachedToken, getToken, getTokenAuthScope } from "../lib/auth";
import {
  pullSync,
  pushSync,
  type SyncPushOperation,
  type SyncTable,
} from "./api";

const TABLES: SyncTable[] = [
  "projects",
  "tasks",
  "task_occurrences",
  "time_entries",
  "conversation_sessions",
  "conversation_messages",
  "record_tables",
  "record_fields",
  "record_rows",
  "scenarios",
  "scenario_characters",
  "scenario_scenes",
  "scenario_episodes",
  "knowledge_nodes",
  "knowledge_supertags",
  "knowledge_node_supertags",
  "knowledge_supertag_fields",
  "knowledge_fields",
  "knowledge_field_values",
  "knowledge_node_placements",
  "knowledge_edges",
];

type PendingOutbox = typeof schema.outbox.$inferSelect;

const completedSync = Promise.resolve();
const runningByAuthScope = new Map<string, Promise<void>>();
let unresolvedAuthFlight: Promise<void> | null = null;
let exclusiveTail: Promise<void> = completedSync;
let syncRequestCount = 0;
let syncExecutionCount = 0;

function enqueueExclusive(operation: () => Promise<void>): Promise<void> {
  const flight = exclusiveTail.catch(() => undefined).then(operation);
  exclusiveTail = flight.catch(() => undefined);
  return flight;
}

function getSyncStateKey(authScope: string): string {
  return `__global__:${authScope.slice("auth:".length)}`;
}

async function getLastPulledAt(authScope: string): Promise<string | null> {
  const tableName = getSyncStateKey(authScope);
  const db = getDb();
  const rows = await db
    .select()
    .from(schema.syncState)
    .where(eq(schema.syncState.tableName, tableName));
  return rows[0]?.lastPulledAt ?? null;
}

async function setLastPulledAt(authScope: string, value: string): Promise<void> {
  const tableName = getSyncStateKey(authScope);
  const db = getDb();
  await db
    .insert(schema.syncState)
    .values({
      tableName,
      lastPulledAt: value,
      lastPushedAt: null,
      cursor: null,
    })
    .onConflictDoUpdate({
      target: schema.syncState.tableName,
      set: { lastPulledAt: value },
    });
}

async function applyPull(authScope: string): Promise<void> {
  const since = await getLastPulledAt(authScope);
  const response = await pullSync({ since, tables: TABLES });
  const projects = response.tables.projects;
  if (projects) {
    await applyRemoteProjects(projects.changes as unknown as Project[]);
    await applyProjectTombstones(projects.tombstones);
  }

  const tasks = response.tables.tasks;
  if (tasks) {
    await applyRemoteTasks(tasks.changes as unknown as Task[]);
    await applyTaskTombstones(tasks.tombstones);
  }

  const occurrences = response.tables.task_occurrences;
  if (occurrences) {
    await applyRemoteOccurrences(
      occurrences.changes as unknown as TaskOccurrence[],
    );
    await applyOccurrenceTombstones(occurrences.tombstones);
  }

  const timeEntries = response.tables.time_entries;
  if (timeEntries) {
    await applyRemoteTimeEntries(timeEntries.changes as unknown as TimeEntry[]);
    await applyTimeEntryTombstones(timeEntries.tombstones);
  }

  const sessions = response.tables.conversation_sessions;
  if (sessions) {
    await applyRemoteConversationSessions(
      sessions.changes as unknown as ConversationSession[],
    );
    await applyConversationSessionTombstones(sessions.tombstones);
    await reconcileConversationSessionsWithServer(sessions.authoritative_ids);
  }

  const messages = response.tables.conversation_messages;
  if (messages) {
    await applyRemoteConversationMessages(
      messages.changes as unknown as ConversationMessage[],
    );
    await applyConversationMessageTombstones(messages.tombstones);
  }

  const recordTables = response.tables.record_tables;
  if (recordTables) {
    await applyRemoteRecordTables(
      recordTables.changes as unknown as RecordTable[],
    );
    await applyRecordTableTombstones(recordTables.tombstones);
  }

  const recordFields = response.tables.record_fields;
  if (recordFields) {
    await applyRemoteRecordFields(
      recordFields.changes as unknown as RecordField[],
    );
    await applyRecordFieldTombstones(recordFields.tombstones);
  }

  const recordRows = response.tables.record_rows;
  if (recordRows) {
    await applyRemoteRecordRows(recordRows.changes as unknown as RecordRow[]);
    await applyRecordRowTombstones(recordRows.tombstones);
  }

  const scenarios = response.tables.scenarios;
  if (scenarios) {
    await applyRemoteScenarios(scenarios.changes as unknown as Scenario[]);
    await applyScenarioTombstones(scenarios.tombstones);
    await reconcileScenariosWithServer(scenarios.authoritative_ids);
  }

  const scenarioCharacters = response.tables.scenario_characters;
  if (scenarioCharacters) {
    await applyRemoteScenarioCharacters(
      scenarioCharacters.changes as unknown as ScenarioCharacter[],
    );
    await reconcileScenarioCharactersWithServer(
      scenarioCharacters.authoritative_ids,
    );
  }

  const scenarioScenes = response.tables.scenario_scenes;
  if (scenarioScenes) {
    await applyRemoteScenarioScenes(
      scenarioScenes.changes as unknown as ScenarioScene[],
    );
    await reconcileScenarioScenesWithServer(scenarioScenes.authoritative_ids);
  }

  const scenarioEpisodes = response.tables.scenario_episodes;
  if (scenarioEpisodes) {
    await applyRemoteScenarioEpisodes(
      scenarioEpisodes.changes as unknown as ScenarioEpisode[],
    );
    await reconcileScenarioEpisodesWithServer(
      scenarioEpisodes.authoritative_ids,
    );
  }

  // ---------- Docs ----------
  const docsNodes = response.tables.knowledge_nodes;
  if (docsNodes) {
    await applyRemoteDocsNodes(docsNodes.changes as unknown as DocsNode[]);
    await applyDocsNodeTombstones(docsNodes.tombstones);
    await reconcileDocsNodesWithServer(
      docsNodes.authoritative_ids,
      docsNodes.authoritative_scope_id,
    );
  }

  const docsSupertags = response.tables.knowledge_supertags;
  if (docsSupertags) {
    await applyRemoteDocsSupertags(
      docsSupertags.changes as unknown as DocsSupertag[],
    );
  }

  const docsFields = response.tables.knowledge_fields;
  if (docsFields) {
    await applyRemoteDocsFields(docsFields.changes as unknown as DocsField[]);
  }

  const docsFieldValues = response.tables.knowledge_field_values;
  if (docsFieldValues) {
    await applyRemoteDocsFieldValues(
      docsFieldValues.changes as unknown as DocsFieldValue[],
      docsFieldValues.authoritative_ids,
    );
  }

  // 関連表は削除を取りこぼさないよう毎回権威セット全量 + authoritative_ids で reconcile。
  const docsNodeSupertags = response.tables.knowledge_node_supertags;
  if (docsNodeSupertags) {
    await applyRemoteDocsNodeSupertags(
      docsNodeSupertags.changes as unknown as DocsNodeSupertag[],
      docsNodeSupertags.authoritative_ids,
    );
  }

  const docsSupertagFields = response.tables.knowledge_supertag_fields;
  if (docsSupertagFields) {
    await applyRemoteDocsSupertagFields(
      docsSupertagFields.changes as unknown as DocsSupertagField[],
      docsSupertagFields.authoritative_ids,
    );
  }

  const docsPlacements = response.tables.knowledge_node_placements;
  if (docsPlacements) {
    await applyRemoteDocsPlacements(
      docsPlacements.changes as unknown as DocsNodePlacement[],
      docsPlacements.authoritative_ids,
    );
  }

  const docsEdges = response.tables.knowledge_edges;
  if (docsEdges) {
    await applyRemoteDocsEdges(
      docsEdges.changes as unknown as DocsEdge[],
      docsEdges.authoritative_ids,
    );
  }

  await setLastPulledAt(authScope, response.server_time);
}

/**
 * Docs push 応答の entity（サーバ権威）を対応する applyRemote* で反映する。
 * 409 conflict の応答も、競合解決が適用済みと判断した場合に反映する。
 */
async function applyDocsPushEntity(
  table: string | undefined,
  entity: Record<string, unknown> | undefined,
): Promise<void> {
  if (!table || !entity) return;
  // クリア/タグ削除時、サーバは { ..., "deleted": true } を返す。無条件 upsert すると
  // 削除済み行を再 INSERT してしまうため、deleted の場合はローカル行を物理削除する。
  const deleted = entity.deleted === true;
  switch (table) {
    case "knowledge_nodes":
      if (await hasPendingOutbox(table, String(entity.id))) {
        await recordOutboxServerSnapshot(table, String(entity.id), entity);
        return;
      }
      await applyRemoteDocsNodes([entity as unknown as DocsNode], { force: true });
      break;
    case "knowledge_supertags":
      if (await hasPendingOutbox(table, String(entity.id))) {
        await recordOutboxServerSnapshot(table, String(entity.id), entity);
        return;
      }
      await applyRemoteDocsSupertags([entity as unknown as DocsSupertag], { force: true });
      break;
    case "knowledge_node_supertags":
      if (await hasPendingOutbox(
        table,
        `${String(entity.node_id)}:${String(entity.supertag_id)}`,
      )) {
        await recordOutboxServerSnapshot(
          table,
          `${String(entity.node_id)}:${String(entity.supertag_id)}`,
          entity,
        );
        return;
      }
      if (deleted) {
        await deleteLocalDocsNodeSupertag(
          String(entity.node_id),
          String(entity.supertag_id),
        );
      } else {
        await applyRemoteDocsNodeSupertags([
          entity as unknown as DocsNodeSupertag,
        ], undefined, { force: true });
      }
      break;
    case "knowledge_field_values":
      if (await hasPendingOutbox(
        table,
        `${String(entity.node_id)}:${String(entity.field_id)}`,
      )) {
        await recordOutboxServerSnapshot(
          table,
          `${String(entity.node_id)}:${String(entity.field_id)}`,
          entity,
        );
        return;
      }
      if (deleted) {
        await deleteLocalDocsFieldValue(
          String(entity.node_id),
          String(entity.field_id),
        );
      } else {
        await applyRemoteDocsFieldValues(
          [entity as unknown as DocsFieldValue],
          undefined,
          { force: true },
        );
      }
      break;
    default:
      break;
  }
}

function jsonEqual(a: unknown, b: unknown): boolean {
  return JSON.stringify(a ?? null) === JSON.stringify(b ?? null);
}

async function getCurrentOutbox(
  operationId: string,
): Promise<PendingOutbox | null> {
  const db = getDb();
  const rows = await db
    .select()
    .from(schema.outbox)
    .where(eq(schema.outbox.opId, operationId));
  return rows[0] ?? null;
}

function isOutboxSnapshotCurrent(
  current: PendingOutbox | null,
  snapshot: PendingOutbox,
): boolean {
  return Boolean(
    current &&
      current.tableName === snapshot.tableName &&
      current.action === snapshot.action &&
      current.entityId === snapshot.entityId &&
      current.payload === snapshot.payload &&
      current.baseUpdatedAt === snapshot.baseUpdatedAt,
  );
}

async function rebaseChangedDocsOutbox(
  operation: PendingOutbox,
  serverEntity: Record<string, unknown> | undefined,
  serverUpdatedAt: string | null | undefined,
): Promise<void> {
  if (!serverEntity || !operation.tableName.startsWith("knowledge_")) return;
  await rebaseOutboxOp(
    operation.opId,
    serverUpdatedAt ?? (serverEntity.updated_at as string | null | undefined) ?? null,
    serverEntity,
  );
}

type DocsConflictResolution = "rebased" | "applied" | "unresolved";

/**
 * サーバー409を端末時計で解決しない。ノードの異なるフィールドだけは
 * サーバー版を新しい基準として再ベースし、同一フィールド・削除状態は
 * outboxに両方を残して明示的競合にする。
 */
async function resolveDocsConflict(
  operation: PendingOutbox,
  serverEntity: Record<string, unknown> | undefined,
  serverUpdatedAt: string | null | undefined,
): Promise<DocsConflictResolution> {
  if (!serverEntity) return "unresolved";
  const base = (operation.basePayload ?? {}) as Record<string, unknown>;
  const payload = JSON.parse(operation.payload || "{}") as Record<string, unknown>;
  const serverVersion = serverUpdatedAt ?? (serverEntity.updated_at as string | null | undefined) ?? null;

  if (operation.tableName === "knowledge_nodes" && operation.action === "create") {
    if (
      serverEntity.deleted === true ||
      !["title", "description", "node_type", "project_id", "day_date", "sort_order"]
        .every((key) => jsonEqual(serverEntity[key], payload[key]))
    ) {
      return "unresolved";
    }
    return "applied";
  }

  if (
    operation.tableName === "knowledge_nodes" &&
    operation.action === "update"
  ) {
    const changed = Object.keys(payload).filter((key) => !jsonEqual(payload[key], base[key]));
    if (changed.length && changed.every((key) => jsonEqual(serverEntity[key], payload[key]))) {
      return "applied";
    }
    const overlap = changed.some((key) => !jsonEqual(serverEntity[key], base[key]));
    if (overlap) return "unresolved";
    await rebaseOutboxOp(operation.opId, serverVersion, serverEntity);
    return "rebased";
  }

  if (operation.tableName === "knowledge_nodes" && operation.action === "delete") {
    if (serverEntity.archived_at) return "applied";
    const nodeFields = [
      "title",
      "description",
      "body_json",
      "parent_id",
      "project_id",
      "day_date",
      "sort_order",
      "archived_at",
    ];
    if (nodeFields.some((key) => !jsonEqual(serverEntity[key], base[key]))) {
      return "unresolved";
    }
    await rebaseOutboxOp(operation.opId, serverVersion, serverEntity);
    return "rebased";
  }

  if (operation.tableName === "knowledge_node_supertags") {
    const deleted = serverEntity.deleted === true;
    if ((operation.action === "create" && !deleted) || (operation.action === "delete" && deleted)) {
      return "applied";
    }
    return "unresolved";
  }

  if (operation.tableName === "knowledge_supertags" && operation.action === "create") {
    if (
      serverEntity.deleted === true ||
      !["name", "base_type", "color", "icon"]
        .every((key) => jsonEqual(serverEntity[key], payload[key]))
    ) {
      return "unresolved";
    }
    return "applied";
  }

  if (operation.tableName === "knowledge_field_values") {
    const value = payload.value;
    if (value === null || value === undefined || value === "") {
      return serverEntity.deleted === true ? "applied" : "unresolved";
    }
    const serverMatches =
      (typeof value === "number" && jsonEqual(serverEntity.value_number, value)) ||
      (typeof value === "boolean" && jsonEqual(serverEntity.value_json, { value })) ||
      (typeof value === "string" &&
        (jsonEqual(serverEntity.value_text, value) ||
          jsonEqual(serverEntity.value_datetime, value) ||
          jsonEqual(serverEntity.target_node_id, value))) ||
      jsonEqual(serverEntity.value_json, value);
    // 既に同じ値がサーバーへ適用済みなら、通信再送による409を解消できる。
    // 異なる値の場合は同一フィールドの競合なので、両方を残す。
    return serverMatches ? "applied" : "unresolved";
  }
  return "unresolved";
}

async function pushOutbox(): Promise<void> {
  const pending = await listPendingOutbox();
  if (!pending.length) return;

  const operations: SyncPushOperation[] = pending.map((op) => ({
    op_id: op.opId,
    table: op.tableName,
    action: op.action as SyncPushOperation["action"],
    entity_id: op.entityId,
    payload: JSON.parse(op.payload || "{}") as Record<string, unknown>,
    base_updated_at: op.baseUpdatedAt ?? null,
  }));

  const response = await pushSync(operations);
  const operationsById = new Map(
    operations.map((operation) => [operation.op_id, operation]),
  );
  const pendingById = new Map(pending.map((operation) => [operation.opId, operation]));
  for (const result of response.results) {
    const operation = operationsById.get(result.op_id);
    const pendingOperation = pendingById.get(result.op_id);
    // push 中に同一エンティティが再編集されると、enqueueOutbox は同じ
    // operation_id の行を更新する。古い応答でその行を削除したり、サーバー値を
    // ローカルへ適用したりせず、応答時点のサーバー版を新しいoutboxの基準にする。
    if (pendingOperation) {
      const currentOperation = await getCurrentOutbox(result.op_id);
      if (!isOutboxSnapshotCurrent(currentOperation, pendingOperation)) {
        if (result.status === "ok") {
          // 送信した古い版はサーバーに適用済みなので、最新outboxだけを
          // 応答版へ再baseする。ローカル値そのものは決して上書きしない。
          await rebaseChangedDocsOutbox(
            currentOperation ?? pendingOperation,
            result.entity,
            result.server_updated_at,
          );
          continue;
        }
        if (
          currentOperation &&
          result.status === "conflict" &&
          operation?.table?.startsWith("knowledge_")
        ) {
          const resolution = await resolveDocsConflict(
            currentOperation,
            result.entity,
            result.server_updated_at,
          );
          if (resolution === "applied") {
            const removed = await removeOutboxOpIfSnapshot(result.op_id, {
              table: currentOperation.tableName,
              action: currentOperation.action,
              entityId: currentOperation.entityId,
              payload: currentOperation.payload,
              baseUpdatedAt: currentOperation.baseUpdatedAt,
            });
            if (!removed) continue;
            await applyDocsPushEntity(operation.table, result.entity);
            continue;
          }
          if (resolution === "rebased") continue;
          await recordOutboxServerSnapshot(
            currentOperation.tableName,
            currentOperation.entityId,
            result.entity ?? { deleted: true },
          );
          await markOutboxConflict(
            result.op_id,
            result.reason ?? result.status,
            result.entity,
          );
        }
        // ネットワーク/サーバーエラーは、後から積まれた最新操作へ
        // 古い応答のエラー状態を引き継がせない。
        continue;
      }
    }
    if (result.status === "ok") {
      if (result.entity && operation?.table === "projects") {
        await applyRemoteProjects([result.entity as unknown as Project]);
      }
      if (result.entity && operation?.table === "tasks") {
        await applyRemoteTasks([result.entity as unknown as Task]);
      }
      if (result.entity && operation?.table === "time_entries") {
        await applyRemoteTimeEntries([result.entity as unknown as TimeEntry]);
      }
      if (pendingOperation && operation) {
        const removed = await removeOutboxOpIfSnapshot(result.op_id, {
          table: pendingOperation.tableName,
          action: pendingOperation.action,
          entityId: pendingOperation.entityId,
          payload: pendingOperation.payload,
          baseUpdatedAt: pendingOperation.baseUpdatedAt,
        });
        if (!removed) {
          const currentOperation = await getCurrentOutbox(result.op_id);
          await rebaseChangedDocsOutbox(
            currentOperation ?? pendingOperation,
            result.entity,
            result.server_updated_at,
          );
          continue;
        }
      } else {
        await removeOutboxOp(result.op_id);
      }
      await applyDocsPushEntity(operation?.table, result.entity);
      continue;
    }

    if (result.status === "conflict") {
      if (pendingOperation && operation?.table?.startsWith("knowledge_")) {
        const resolution = await resolveDocsConflict(
          pendingOperation,
          result.entity,
          result.server_updated_at,
        );
        if (resolution === "applied") {
          const removed = await removeOutboxOpIfSnapshot(result.op_id, {
            table: pendingOperation.tableName,
            action: pendingOperation.action,
            entityId: pendingOperation.entityId,
            payload: pendingOperation.payload,
            baseUpdatedAt: pendingOperation.baseUpdatedAt,
          });
          if (!removed) continue;
          await applyDocsPushEntity(operation.table, result.entity);
          continue;
        }
        if (resolution === "rebased") continue;
        await recordOutboxServerSnapshot(
          operation.table,
          operation.entity_id,
          result.entity ?? { deleted: true },
        );
        await markOutboxConflict(
          result.op_id,
          result.reason ?? result.status,
          result.entity,
        );
        continue;
      }
      if (result.entity && operation?.table === "projects") {
        await applyRemoteProjects([result.entity as unknown as Project]);
      }
      if (result.entity && operation?.table === "tasks") {
        await applyRemoteTasks([result.entity as unknown as Task]);
      }
      if (result.entity && operation?.table === "time_entries") {
        await applyRemoteTimeEntries([result.entity as unknown as TimeEntry]);
      }
      await applyDocsPushEntity(operation?.table, result.entity);
      await markOutboxConflict(result.op_id, result.reason ?? result.status);
      continue;
    }

    if (result.status === "error") {
      await markOutboxError(result.op_id, result.reason ?? result.status);
    }
  }
}

async function performSync(authScope: string): Promise<void> {
  syncExecutionCount += 1;
  try {
    await pushOutbox();
    await flushPendingConversations();
    await applyPull(authScope);
    useNetworkStore.getState().setServerReachable(true);
  } catch (error) {
    console.warn(
      "[sync] run failed",
      error instanceof Error ? error.name : "UnknownError",
    );
    useNetworkStore.getState().setServerReachable(false);
  }
}

function runSyncForToken(token: string | null): Promise<void> {
  if (!token || !useNetworkStore.getState().online) return completedSync;

  const authScope = getTokenAuthScope(token);
  const running = runningByAuthScope.get(authScope);
  if (running) return running;

  const flight = enqueueExclusive(async () => {
    // auth transition中に予約された旧scopeの同期は、DBへ触れる前に破棄する。
    if (getTokenAuthScope(getCachedToken()) !== authScope) return;
    await performSync(authScope);
  }).finally(() => {
    if (runningByAuthScope.get(authScope) === flight) {
      runningByAuthScope.delete(authScope);
    }
  });
  runningByAuthScope.set(authScope, flight);
  return flight;
}

/**
 * 同じ認証スコープの同期要求を完全なsingle-flightとしてまとめる。
 * 異なるscopeも共有SQLite/outboxを守るため共通queue上で直列実行する。
 */
export function runSync(): Promise<void> {
  syncRequestCount += 1;
  if (!useNetworkStore.getState().online) return completedSync;

  const token = getCachedToken();
  if (token !== undefined) return runSyncForToken(token);
  if (unresolvedAuthFlight) return unresolvedAuthFlight;

  const tokenLookup = getToken();
  const flight = tokenLookup
    .then((resolvedToken) => runSyncForToken(resolvedToken))
    .finally(() => {
      if (unresolvedAuthFlight === flight) unresolvedAuthFlight = null;
    });
  unresolvedAuthFlight = flight;
  return flight;
}

/**
 * 進行中の同期と排他的に認証scopeを切り替える。
 * callback中に届いたrunSyncは、token/cache更新完了後まで開始されない。
 */
export function runAuthScopeTransition<T>(
  callback: () => Promise<T>,
): Promise<T> {
  const transition = exclusiveTail.catch(() => undefined).then(callback);
  exclusiveTail = transition.then(
    () => undefined,
    () => undefined,
  );
  return transition;
}

export function getSyncDiagnostics(): {
  requestCount: number;
  executionCount: number;
  runningScopes: number;
} {
  return {
    requestCount: syncRequestCount,
    executionCount: syncExecutionCount,
    runningScopes: runningByAuthScope.size + (unresolvedAuthFlight ? 1 : 0),
  };
}

export function resetSyncDiagnostics(): void {
  syncRequestCount = 0;
  syncExecutionCount = 0;
}
