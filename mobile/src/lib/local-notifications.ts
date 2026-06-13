import AsyncStorage from "@react-native-async-storage/async-storage";
import * as Notifications from "expo-notifications";
import { Platform } from "react-native";
import { and, eq, isNull } from "drizzle-orm";
import { router } from "expo-router";
import { getDb, schema } from "../db/client";

const CHANNEL_ID = "aoitalk-task-reminders";
const STORAGE_KEY = "aoitalk.localNotificationSchedule.v1";
const DEFAULT_OFFSETS_STORAGE_KEY = "aoitalk.localNotificationDefaultOffsets.v1";
const DEFAULT_REMINDER_OFFSETS = [5];
const MAX_SCHEDULED_NOTIFICATIONS = 60;
const SCHEDULE_HORIZON_DAYS = 60;

const CLOSED_STATUSES = new Set(["closed", "done", "cancelled", "canceled"]);
let rescheduleChain: Promise<void> = Promise.resolve();

type StoredSchedule = Record<string, string[]>;

type NotificationCandidate = {
  key: string;
  taskId: string;
  occurrenceId?: string | null;
  title: string;
  body: string;
  triggerAt: Date;
  kind: "reminder";
};

Notifications.setNotificationHandler({
  handleNotification: async () => ({
    shouldShowBanner: true,
    shouldShowList: true,
    shouldPlaySound: true,
    shouldSetBadge: false,
  }),
});

function parseDate(value?: string | null): Date | null {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

function sanitizeOffsets(value: unknown, fallback: number[]): number[] {
  if (!Array.isArray(value)) return fallback;
  const offsets = value
    .map((offset) => Number(offset))
    .filter((offset) => Number.isFinite(offset) && offset >= 0)
    .map((offset) => Math.floor(offset));
  return offsets.length > 0 ? offsets : fallback;
}

function normalizeOffsets(value: unknown, fallback: number[]): number[] {
  const parsed =
    typeof value === "string"
      ? (() => {
          try {
            return JSON.parse(value) as unknown;
          } catch {
            return null;
          }
        })()
      : value;

  return sanitizeOffsets(parsed, fallback);
}

function isClosedStatus(status?: string | null): boolean {
  return CLOSED_STATUSES.has(String(status ?? "").toLowerCase());
}

function isMidnight(date: Date | null): boolean {
  return (
    !!date &&
    date.getHours() === 0 &&
    date.getMinutes() === 0 &&
    date.getSeconds() === 0 &&
    date.getMilliseconds() === 0
  );
}

function isDateOnlySchedule(
  allDay: boolean | null | undefined,
  startAt: Date | null,
  endAt: Date | null,
): boolean {
  if (allDay) return true;
  if (startAt && endAt) return isMidnight(startAt) && isMidnight(endAt);
  return isMidnight(startAt ?? endAt);
}

function formatDateTime(date: Date): string {
  return date.toLocaleString("ja-JP", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

async function readStoredSchedule(): Promise<StoredSchedule> {
  try {
    const raw = await AsyncStorage.getItem(STORAGE_KEY);
    const parsed = raw ? JSON.parse(raw) : {};
    return parsed && typeof parsed === "object" && !Array.isArray(parsed)
      ? (parsed as StoredSchedule)
      : {};
  } catch {
    return {};
  }
}

async function readDefaultReminderOffsets(): Promise<number[]> {
  try {
    const raw = await AsyncStorage.getItem(DEFAULT_OFFSETS_STORAGE_KEY);
    return sanitizeOffsets(
      raw ? JSON.parse(raw) : null,
      DEFAULT_REMINDER_OFFSETS,
    );
  } catch {
    return DEFAULT_REMINDER_OFFSETS;
  }
}

async function cancelStoredSchedule(): Promise<void> {
  const stored = await readStoredSchedule();
  const identifiers = Object.values(stored).flat();
  await Promise.allSettled(
    identifiers.map((id) => Notifications.cancelScheduledNotificationAsync(id)),
  );
  await AsyncStorage.removeItem(STORAGE_KEY);
}

async function canScheduleNotifications(): Promise<boolean> {
  try {
    const current = await Notifications.getPermissionsAsync();
    if (current.granted) return true;
    const requested = await Notifications.requestPermissionsAsync();
    return requested.granted;
  } catch {
    return false;
  }
}

export async function initializeLocalNotifications(): Promise<boolean> {
  try {
    if (Platform.OS === "android") {
      await Notifications.setNotificationChannelAsync(CHANNEL_ID, {
        name: "AoiTalk task reminders",
        importance: Notifications.AndroidImportance.HIGH,
        vibrationPattern: [0, 250, 250, 250],
        lightColor: "#7c3aed",
      });
    }
    return await canScheduleNotifications();
  } catch {
    return false;
  }
}

export async function getLocalNotificationPermissionLabel(): Promise<string> {
  try {
    const permission = await Notifications.getPermissionsAsync();
    if (permission.granted) return "許可済み";
    if (permission.canAskAgain) return "未許可";
    return "ブロック中";
  } catch {
    return "確認不可";
  }
}

export async function setLocalTaskNotificationDefaultOffset(
  minutes: number,
  options: { reschedule?: boolean } = {},
): Promise<void> {
  const safeMinutes =
    Number.isFinite(minutes) && minutes >= 0 ? Math.floor(minutes) : 5;
  await AsyncStorage.setItem(
    DEFAULT_OFFSETS_STORAGE_KEY,
    JSON.stringify([safeMinutes]),
  );
  if (options.reschedule !== false) {
    await rescheduleLocalTaskNotificationsFromCache();
  }
}

export async function sendLocalNotificationTest(): Promise<boolean> {
  if (!(await initializeLocalNotifications())) return false;
  await Notifications.scheduleNotificationAsync({
    content: {
      title: "AoiTalk notification test",
      body: "スマホアプリの端末通知は有効です。",
      sound: true,
      data: { kind: "notification_test" },
    },
    trigger: {
      type: Notifications.SchedulableTriggerInputTypes.DATE,
      date: new Date(Date.now() + 1000),
      channelId: CHANNEL_ID,
    },
  });
  return true;
}

function addReminderCandidates(
  candidates: NotificationCandidate[],
  params: {
    keyPrefix: string;
    taskId: string;
    occurrenceId?: string | null;
    title: string;
    startAt: Date | null;
    endAt: Date | null;
    allDay?: boolean | null;
    offsets: number[];
    status?: string | null;
  },
) {
  if (isDateOnlySchedule(params.allDay, params.startAt, params.endAt)) return;
  const anchor = params.startAt ?? params.endAt;
  if (!anchor) return;

  for (const offset of params.offsets) {
    const triggerAt = new Date(anchor.getTime() - offset * 60_000);
    candidates.push({
      key: `${params.keyPrefix}:reminder:${offset}`,
      taskId: params.taskId,
      occurrenceId: params.occurrenceId,
      title: `Upcoming: ${params.title}`,
      body:
        offset > 0
          ? `${params.title} starts in ${offset} minutes (${formatDateTime(anchor)})`
          : `${params.title} starts now`,
      triggerAt,
      kind: "reminder",
    });
  }
}

async function collectCandidates(): Promise<NotificationCandidate[]> {
  const db = getDb();
  const now = Date.now();
  const horizon = now + SCHEDULE_HORIZON_DAYS * 24 * 60 * 60_000;
  const defaultOffsets = await readDefaultReminderOffsets();
  const candidates: NotificationCandidate[] = [];

  const occurrenceRows = await db
    .select({
      occurrence: schema.taskOccurrences,
      taskId: schema.tasks.id,
      taskTitle: schema.tasks.title,
      taskStatus: schema.tasks.status,
      taskReminderOffsets: schema.tasks.reminderOffsets,
      taskNotificationsEnabled: schema.tasks.notificationsEnabled,
    })
    .from(schema.taskOccurrences)
    .innerJoin(schema.tasks, eq(schema.taskOccurrences.taskId, schema.tasks.id))
    .where(
      and(
        isNull(schema.taskOccurrences.deletedAt),
        isNull(schema.tasks.deletedAt),
      ),
    );

  const occurrenceTaskIds = new Set<string>();
  for (const row of occurrenceRows) {
    occurrenceTaskIds.add(row.taskId);
    if (row.taskNotificationsEnabled === false) continue;
    if (isClosedStatus(row.occurrence.status || row.taskStatus)) continue;

    addReminderCandidates(candidates, {
      keyPrefix: `occurrence:${row.occurrence.id}`,
      taskId: row.taskId,
      occurrenceId: row.occurrence.id,
      title: row.taskTitle || "Task",
      startAt: parseDate(row.occurrence.startAt),
      endAt: parseDate(row.occurrence.endAt),
      allDay: row.occurrence.allDay,
      offsets: normalizeOffsets(
        row.occurrence.reminderOffsets ?? row.taskReminderOffsets,
        defaultOffsets,
      ),
      status: row.occurrence.status || row.taskStatus,
    });
  }

  const tasks = await db
    .select()
    .from(schema.tasks)
    .where(isNull(schema.tasks.deletedAt));
  for (const task of tasks) {
    if (occurrenceTaskIds.has(task.id)) continue;
    if (task.notificationsEnabled === false) continue;
    if (isClosedStatus(task.status)) continue;

    addReminderCandidates(candidates, {
      keyPrefix: `task:${task.id}`,
      taskId: task.id,
      title: task.title || "Task",
      startAt: parseDate(task.startAt),
      endAt: parseDate(task.endAt),
      allDay: task.allDay,
      offsets: normalizeOffsets(task.reminderOffsets, defaultOffsets),
      status: task.status,
    });
  }

  return candidates
    .filter((candidate) => {
      const time = candidate.triggerAt.getTime();
      return time > now && time <= horizon;
    })
    .sort((a, b) => a.triggerAt.getTime() - b.triggerAt.getTime())
    .slice(0, MAX_SCHEDULED_NOTIFICATIONS);
}

async function runRescheduleLocalTaskNotificationsFromCache(): Promise<void> {
  await cancelStoredSchedule();
  if (!(await canScheduleNotifications())) return;

  const scheduled: StoredSchedule = {};
  const candidates = await collectCandidates();
  for (const candidate of candidates) {
    try {
      const identifier = await Notifications.scheduleNotificationAsync({
        content: {
          title: candidate.title,
          body: candidate.body,
          sound: true,
          data: {
            kind: candidate.kind,
            task_id: candidate.taskId,
            occurrence_id: candidate.occurrenceId ?? null,
          },
        },
        trigger: {
          type: Notifications.SchedulableTriggerInputTypes.DATE,
          date: candidate.triggerAt,
          channelId: CHANNEL_ID,
        },
      });
      scheduled[candidate.key] = [
        ...(scheduled[candidate.key] ?? []),
        identifier,
      ];
    } catch {
      // Ignore individual scheduling failures so one bad row does not disable
      // all other local reminders.
    }
  }

  await AsyncStorage.setItem(STORAGE_KEY, JSON.stringify(scheduled));
}

export function rescheduleLocalTaskNotificationsFromCache(): Promise<void> {
  rescheduleChain = rescheduleChain
    .catch(() => undefined)
    .then(runRescheduleLocalTaskNotificationsFromCache);
  return rescheduleChain;
}

export async function clearLocalTaskNotifications(): Promise<void> {
  await cancelStoredSchedule();
}

export function installLocalNotificationResponseHandler() {
  const subscription = Notifications.addNotificationResponseReceivedListener(
    (response) => {
      const taskId = response.notification.request.content.data?.task_id;
      if (typeof taskId === "string" && taskId) {
        router.push(`/(tabs)/tasks/${taskId}`);
      }
    },
  );
  return () => subscription.remove();
}
