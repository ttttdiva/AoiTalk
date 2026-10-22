"use client";

import type { ChatAppContextSelection } from "@/components/chat/app-context-picker";
import type { ChatCommandCapability } from "@/lib/chat-commands";
import type { MentionItem } from "@/components/chat/mention-menu";
import { getPersistentStore } from "@/lib/persistent-cache";

/**
 * A browser-side record for a user turn whose server acknowledgement is not
 * known yet.  The client id is the only identity used when a retry is sent;
 * the server's (session_id, client_message_id) unique key makes that retry
 * idempotent.
 */
export type ChatSendDeliveryState =
  | "pending"
  | "accepted"
  | "failed"
  | "uncertain";

export type ChatSendAttachmentMetadata = {
  name: string;
  size: number;
  type: string;
  lastModified: number;
};

export type ChatSendOutboxRecord = {
  schema: 1;
  userId: string;
  clientMessageId: string;
  sessionId: string;
  content: string;
  mentions: MentionItem[];
  commandCapabilities: ChatCommandCapability[];
  generationProfile?: string;
  includeProjectContext?: boolean;
  projectId?: string | null;
  appContext?: ChatAppContextSelection | null;
  toolsRequired?: boolean;
  attachments: ChatSendAttachmentMetadata[];
  /** File objects are persisted in IndexedDB when structured clone supports them. */
  files?: File[];
  status: ChatSendDeliveryState;
  error?: string;
  createdAt: number;
  updatedAt: number;
};

const OUTBOX_KEY_PREFIX = "chat/send-outbox/";
const OUTBOX_STORAGE_PREFIX = "aoitalk:chat-send-outbox:v1:";
export const CHAT_SEND_RESTORE_EVENT = "aoitalk:chat-send-restore";

export type ChatSendRestoreEventDetail = {
  userId: string;
  sessionId: string;
  clientMessageId: string;
  content: string;
  mentions: MentionItem[];
  commandCapabilities: ChatCommandCapability[];
  attachments: ChatSendAttachmentMetadata[];
};

export function requestChatSendRestore(
  detail: ChatSendRestoreEventDetail,
): void {
  if (typeof window === "undefined") return;
  window.dispatchEvent(
    new CustomEvent<ChatSendRestoreEventDetail>(CHAT_SEND_RESTORE_EVENT, {
      detail,
    }),
  );
}

function normalizedUserId(userId: string | null | undefined): string | null {
  const value = typeof userId === "string" ? userId.trim() : "";
  return value || null;
}

function storageKey(userId: string, clientMessageId: string): string {
  return `${OUTBOX_STORAGE_PREFIX}${encodeURIComponent(userId)}:${encodeURIComponent(clientMessageId)}`;
}

function idbKey(userId: string, clientMessageId: string): string {
  return `${OUTBOX_KEY_PREFIX}${encodeURIComponent(userId)}:${encodeURIComponent(clientMessageId)}`;
}

function browserStorage(): Storage | null {
  if (typeof window === "undefined") return null;
  try {
    return window.localStorage ?? null;
  } catch {
    return null;
  }
}

function toMetadata(file: File): ChatSendAttachmentMetadata {
  return {
    name: file.name,
    size: Number.isFinite(file.size) ? file.size : 0,
    type: file.type || "",
    lastModified: Number.isFinite(file.lastModified) ? file.lastModified : 0,
  };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function parseRecord(value: unknown): ChatSendOutboxRecord | null {
  if (!isRecord(value)) return null;
  if (value.schema !== 1) return null;
  if (typeof value.userId !== "string" || !value.userId.trim()) return null;
  if (typeof value.clientMessageId !== "string" || !value.clientMessageId.trim()) return null;
  if (typeof value.sessionId !== "string") return null;
  if (typeof value.content !== "string") return null;
  if (!Array.isArray(value.mentions) || !Array.isArray(value.commandCapabilities)) return null;
  if (!Array.isArray(value.attachments)) return null;
  if (typeof value.createdAt !== "number" || typeof value.updatedAt !== "number") return null;
  if (!(value.status === "pending" || value.status === "accepted" || value.status === "failed" || value.status === "uncertain")) return null;

  const attachments = value.attachments.filter((item): item is ChatSendAttachmentMetadata => {
    if (!isRecord(item)) return false;
    return typeof item.name === "string" && typeof item.size === "number" &&
      typeof item.type === "string" && typeof item.lastModified === "number";
  });
  return {
    schema: 1,
    userId: value.userId,
    clientMessageId: value.clientMessageId,
    sessionId: value.sessionId,
    content: value.content,
    mentions: value.mentions as MentionItem[],
    commandCapabilities: value.commandCapabilities as ChatCommandCapability[],
    generationProfile: typeof value.generationProfile === "string" ? value.generationProfile : undefined,
    includeProjectContext: typeof value.includeProjectContext === "boolean" ? value.includeProjectContext : undefined,
    projectId: typeof value.projectId === "string" || value.projectId === null ? value.projectId : undefined,
    appContext: isRecord(value.appContext) ? value.appContext as unknown as ChatAppContextSelection : value.appContext === null ? null : undefined,
    toolsRequired: typeof value.toolsRequired === "boolean" ? value.toolsRequired : undefined,
    attachments,
    files: Array.isArray(value.files) ? value.files.filter((file): file is File => typeof File !== "undefined" && file instanceof File) : undefined,
    status: value.status,
    error: typeof value.error === "string" ? value.error : undefined,
    createdAt: value.createdAt,
    updatedAt: value.updatedAt,
  };
}

function withoutFiles(record: ChatSendOutboxRecord): Omit<ChatSendOutboxRecord, "files"> {
  const metadata = { ...record } as Omit<ChatSendOutboxRecord, "files"> & {
    files?: File[];
  };
  delete metadata.files;
  return metadata;
}

function readLocalRecords(userId: string): ChatSendOutboxRecord[] {
  const storage = browserStorage();
  if (!storage) return [];
  const prefix = `${OUTBOX_STORAGE_PREFIX}${encodeURIComponent(userId)}:`;
  const records: ChatSendOutboxRecord[] = [];
  try {
    for (let index = 0; index < storage.length; index += 1) {
      const key = storage.key(index);
      if (!key?.startsWith(prefix)) continue;
      const raw = storage.getItem(key);
      if (!raw) continue;
      const parsed = parseRecord(JSON.parse(raw));
      if (!parsed) {
        storage.removeItem(key);
        continue;
      }
      records.push(parsed);
    }
  } catch {
    return records;
  }
  return records;
}

function writeLocalRecord(record: ChatSendOutboxRecord): void {
  const storage = browserStorage();
  if (!storage) return;
  try {
    storage.setItem(storageKey(record.userId, record.clientMessageId), JSON.stringify(withoutFiles(record)));
  } catch {
    // A full or unavailable localStorage must not block the send path.
  }
}

function removeLocalRecord(userId: string, clientMessageId: string): void {
  try {
    browserStorage()?.removeItem(storageKey(userId, clientMessageId));
  } catch {
    // Best effort cleanup only.
  }
}

/** Build an outbox record without retaining data outside the user-scoped store. */
export function createChatSendOutboxRecord(input: {
  userId: string;
  clientMessageId: string;
  sessionId: string;
  content: string;
  files?: File[];
  mentions?: MentionItem[];
  commandCapabilities?: ChatCommandCapability[];
  generationProfile?: string;
  includeProjectContext?: boolean;
  projectId?: string | null;
  appContext?: ChatAppContextSelection | null;
  toolsRequired?: boolean;
}): ChatSendOutboxRecord {
  const now = Date.now();
  const files = input.files?.length ? [...input.files] : undefined;
  return {
    schema: 1,
    userId: input.userId,
    clientMessageId: input.clientMessageId,
    sessionId: input.sessionId,
    content: input.content,
    mentions: [...(input.mentions ?? [])],
    commandCapabilities: [...(input.commandCapabilities ?? [])],
    generationProfile: input.generationProfile,
    includeProjectContext: input.includeProjectContext,
    projectId: input.projectId,
    appContext: input.appContext ?? null,
    toolsRequired: input.toolsRequired,
    attachments: files?.map(toMetadata) ?? [],
    files,
    status: "pending",
    createdAt: now,
    updatedAt: now,
  };
}

export async function saveChatSendOutboxRecord(record: ChatSendOutboxRecord): Promise<void> {
  const next = { ...record, updatedAt: Date.now() };
  writeLocalRecord(next);
  try {
    await getPersistentStore().set(idbKey(next.userId, next.clientMessageId), next);
  } catch {
    // The local metadata still allows a safe text restore/reselect path.
  }
}

export async function updateChatSendOutboxRecord(
  userId: string,
  clientMessageId: string,
  patch: Partial<Pick<ChatSendOutboxRecord, "sessionId" | "status" | "error">>,
): Promise<ChatSendOutboxRecord | null> {
  const existing = await getChatSendOutboxRecord(userId, clientMessageId);
  if (!existing) return null;
  const next = { ...existing, ...patch, updatedAt: Date.now() };
  await saveChatSendOutboxRecord(next);
  return next;
}

export async function getChatSendOutboxRecord(
  userId: string | null | undefined,
  clientMessageId: string,
): Promise<ChatSendOutboxRecord | null> {
  const normalized = normalizedUserId(userId);
  if (!normalized || !clientMessageId) return null;
  const local = readLocalRecords(normalized).find((record) => record.clientMessageId === clientMessageId) ?? null;
  try {
    const persisted = parseRecord(await getPersistentStore().get(idbKey(normalized, clientMessageId)));
    if (persisted && (!local || persisted.updatedAt >= local.updatedAt)) return persisted;
  } catch {
    // Fall back to local metadata.
  }
  return local;
}

export async function listChatSendOutboxRecords(
  userId: string | null | undefined,
): Promise<ChatSendOutboxRecord[]> {
  const normalized = normalizedUserId(userId);
  if (!normalized) return [];
  const byId = new Map(readLocalRecords(normalized).map((record) => [record.clientMessageId, record]));
  const idbPrefix = `${OUTBOX_KEY_PREFIX}${encodeURIComponent(normalized)}:`;
  try {
    for (const [key, value] of await getPersistentStore().entries()) {
      if (!key.startsWith(idbPrefix)) continue;
      const record = parseRecord(value);
      if (record && record.userId === normalized) {
        if (record.status === "accepted") {
          void deleteChatSendOutboxRecord(normalized, record.clientMessageId);
          continue;
        }
        const current = byId.get(record.clientMessageId);
        if (!current || record.updatedAt >= current.updatedAt) byId.set(record.clientMessageId, record);
      }
    }
  } catch {
    // Local metadata remains enough for text restore and explicit reselect.
  }
  return [...byId.values()].filter((record) => record.status !== "accepted");
}

export async function deleteChatSendOutboxRecord(
  userId: string | null | undefined,
  clientMessageId: string,
): Promise<void> {
  const normalized = normalizedUserId(userId);
  if (!normalized || !clientMessageId) return;
  removeLocalRecord(normalized, clientMessageId);
  try {
    await getPersistentStore().delete(idbKey(normalized, clientMessageId));
  } catch {
    // Best effort cleanup only.
  }
}

/** Metadata for an attachment that could not be recovered as a File object. */
export function outboxAttachmentMetadata(record: ChatSendOutboxRecord): ChatSendAttachmentMetadata[] {
  return record.attachments.map((attachment) => ({ ...attachment }));
}
