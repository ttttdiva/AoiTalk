import { eq } from "drizzle-orm";
import { getDb, schema } from "../db/client";
import {
  listPendingOutbox,
  markOutboxConflict,
  markOutboxError,
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
import { flushPendingConversations } from "../repositories/conversations";
import type {
  ConversationMessage,
  ConversationSession,
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
import { decodeTokenPayload, getToken } from "../lib/auth";
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
];

let running = false;

async function getSyncStateKey(): Promise<string | null> {
  const token = await getToken();
  if (!token) return null;
  const user = decodeTokenPayload(token, { ignoreExpiration: true });
  return user?.user_id ? `__global__:${user.user_id}` : "__global__:unknown";
}

async function getLastPulledAt(): Promise<string | null> {
  const tableName = await getSyncStateKey();
  if (!tableName) return null;
  const db = getDb();
  const scenarioRows = await db.select({ id: schema.scenarios.id }).from(schema.scenarios);
  if (scenarioRows.length === 0) return null;

  const rows = await db
    .select()
    .from(schema.syncState)
    .where(eq(schema.syncState.tableName, tableName));
  return rows[0]?.lastPulledAt ?? null;
}

async function setLastPulledAt(value: string): Promise<void> {
  const tableName = await getSyncStateKey();
  if (!tableName) return;
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

async function applyPull(): Promise<void> {
  const since = await getLastPulledAt();
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

  await setLastPulledAt(response.server_time);
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
  for (const result of response.results) {
    const operation = operationsById.get(result.op_id);
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
      await removeOutboxOp(result.op_id);
      continue;
    }

    if (result.status === "conflict") {
      if (result.entity && operation?.table === "projects") {
        await applyRemoteProjects([result.entity as unknown as Project]);
      }
      if (result.entity && operation?.table === "tasks") {
        await applyRemoteTasks([result.entity as unknown as Task]);
      }
      if (result.entity && operation?.table === "time_entries") {
        await applyRemoteTimeEntries([result.entity as unknown as TimeEntry]);
      }
      await markOutboxConflict(result.op_id, result.reason ?? result.status);
      continue;
    }

    if (result.status === "error") {
      await markOutboxError(result.op_id, result.reason ?? result.status);
    }
  }
}

export async function runSync(): Promise<void> {
  if (running) return;
  if (!useNetworkStore.getState().online) return;
  if (!(await getToken())) return;
  running = true;
  try {
    await pushOutbox();
    await flushPendingConversations();
    await applyPull();
    useNetworkStore.getState().setServerReachable(true);
  } catch {
    useNetworkStore.getState().setServerReachable(false);
  } finally {
    running = false;
  }
}
