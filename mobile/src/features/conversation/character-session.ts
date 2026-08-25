import type { MutableRefObject } from "react";
import type {
  CharacterProfileSnapshot,
  ConversationMessage,
  ConversationSession,
} from "../../types/api";
import { characterApi } from "../../lib/character-api";
import { getApiUrl, getToken, getTokenAuthScope } from "../../lib/auth";
import { normalizeApiUrl } from "../../lib/api-url";
import {
  generateMobileLlmReply,
  type MobileLlmReply,
  type MobileLlmSettings,
} from "../../lib/mobile-llm";
import { getCharacterUpdateEligibility } from "../../repositories/conversations";
import type { RunState } from "./models";

export type CharacterChangeAvailability =
  | { allowed: true }
  | { allowed: false; reason: string };

export function getCharacterChangeAvailability(
  session: ConversationSession | null,
  runState: RunState,
  pendingMessages = 0,
): CharacterChangeAvailability {
  if (!session) {
    return {
      allowed: false,
      reason: "会話セッションの読み込み完了後に変更できます。",
    };
  }
  const eligibility = getCharacterUpdateEligibility(session);
  if (!eligibility.allowed) return eligibility;
  if (pendingMessages > 0) {
    return {
      allowed: false,
      reason: "未送信メッセージの同期が完了してからキャラクターを変更してください。",
    };
  }
  if (runState !== "idle") {
    return {
      allowed: false,
      reason: "応答の生成・送信が完了してからキャラクターを変更してください。",
    };
  }
  return { allowed: true };
}

export type ConversationExclusiveOperation =
  | "send"
  | "character-update"
  | "project-update"
  | "background-flush";

export function tryStartConversationOperation(
  operationRef: MutableRefObject<ConversationExclusiveOperation | null>,
  operation: ConversationExclusiveOperation,
): boolean {
  if (operationRef.current) return false;
  operationRef.current = operation;
  return true;
}

export function finishConversationOperation(
  operationRef: MutableRefObject<ConversationExclusiveOperation | null>,
  operation: ConversationExclusiveOperation,
): void {
  if (operationRef.current === operation) operationRef.current = null;
}

export async function runExclusiveConversationOperation<T>(
  operationRef: MutableRefObject<ConversationExclusiveOperation | null>,
  operation: ConversationExclusiveOperation,
  task: () => Promise<T>,
): Promise<{ started: false } | { started: true; value: T }> {
  if (!tryStartConversationOperation(operationRef, operation)) {
    return { started: false };
  }
  try {
    return { started: true, value: await task() };
  } finally {
    finishConversationOperation(operationRef, operation);
  }
}

type CharacterLookup = (
  slug: string,
) => Promise<CharacterProfileSnapshot | null>;

export class CharacterProfileUnavailableError extends Error {
  readonly slug: string;

  constructor(slug: string) {
    super(`キャラクター「${slug}」の情報を取得できないため、Direct応答を開始できません。`);
    this.name = "CharacterProfileUnavailableError";
    this.slug = slug;
  }
}

export type CharacterSnapshotResolverOptions = {
  /** 会話ごとの snapshot を別会話へ混ぜないための識別子。 */
  sessionId?: string | null;
  /** 同一ユーザー/認証状態だけで snapshot を再利用するための scope。 */
  authScope?: string | null;
  /** API URL / server identity。未指定時は現在の API URL を解決する。 */
  serverIdentity?: string | null;
  /** true の場合、初回取得不能時に null 生成へ縮退しない。 */
  strict?: boolean;
};

const characterSnapshotCache = new Map<string, CharacterProfileSnapshot>();

function isCompleteCharacterSnapshot(
  value: CharacterProfileSnapshot | null | undefined,
  slug: string,
): value is CharacterProfileSnapshot {
  if (!value) return false;
  return (
    typeof value.name === "string" &&
    value.name.trim().length > 0 &&
    typeof value.slug === "string" &&
    value.slug.trim() === slug
  );
}

async function resolveCharacterSnapshotScope(
  characterSlug: string,
  options: CharacterSnapshotResolverOptions,
): Promise<string> {
  const server = normalizeApiUrl(
    options.serverIdentity || (await getApiUrl()) || "",
  ) || "server-unknown";
  const authScope =
    String(options.authScope ?? "").trim() ||
    getTokenAuthScope(await getToken());
  const sessionId = String(options.sessionId ?? "").trim() || "session-unknown";
  return [server, authScope || "anonymous", sessionId, characterSlug].join("::");
}

export function createCharacterProfileSnapshotResolver(
  characterSlug: string | null | undefined,
  lookup: CharacterLookup = (slug) => characterApi.getBySlug(slug),
  options: CharacterSnapshotResolverOptions = {},
): () => Promise<CharacterProfileSnapshot | null> {
  const slugAtSendStart = String(characterSlug ?? "").trim();
  const strict = options.strict ?? true;
  let snapshotPromise: Promise<CharacterProfileSnapshot | null> | null = null;

  return () => {
    if (!snapshotPromise) {
      snapshotPromise = (async () => {
        if (!slugAtSendStart) return null;
        const cacheable = Boolean(String(options.sessionId ?? "").trim());
        let cacheKey = [
          String(options.serverIdentity ?? "server-unknown"),
          String(options.authScope ?? "anonymous"),
          String(options.sessionId ?? "session-unknown"),
          slugAtSendStart,
        ].join("::");
        try {
          cacheKey = await resolveCharacterSnapshotScope(
            slugAtSendStart,
            options,
          );
          const resolved = await lookup(slugAtSendStart);
          if (!isCompleteCharacterSnapshot(resolved, slugAtSendStart)) {
            throw new CharacterProfileUnavailableError(slugAtSendStart);
          }
          if (cacheable) characterSnapshotCache.set(cacheKey, resolved);
          return resolved;
        } catch (error) {
          const cached = cacheable ? characterSnapshotCache.get(cacheKey) : null;
          if (cached) return cached;
          if (strict) {
            if (error instanceof CharacterProfileUnavailableError) throw error;
            throw new CharacterProfileUnavailableError(slugAtSendStart);
          }
          return null;
        }
      })();
    }
    return snapshotPromise;
  };
}

export function buildDirectReplyPersistedMetadata(
  metadata: Record<string, unknown> = {},
): Record<string, unknown> {
  return {
    pending: false,
    message_state: "persisted",
    direct_cloud: true,
    ...metadata,
  };
}

export function buildRetryableServerDispatchMetadata(
  deliveryError: string,
  fallbackError?: string,
): Record<string, unknown> {
  return {
    pending: true,
    message_state: "queued",
    delivery_route: "server",
    retryable: true,
    delivery_error: deliveryError,
    ...(fallbackError ? { fallback_error: fallbackError } : {}),
  };
}

type DirectReplyGenerator = (
  settings: MobileLlmSettings,
  messages: ConversationMessage[],
  nextText: string,
  characterProfile?: CharacterProfileSnapshot | null,
) => Promise<MobileLlmReply>;

export async function generateCharacterAwareDirectReply(
  settings: MobileLlmSettings,
  messages: ConversationMessage[],
  nextText: string,
  resolveCharacterSnapshot: () => Promise<CharacterProfileSnapshot | null>,
  generate: DirectReplyGenerator = generateMobileLlmReply,
): Promise<MobileLlmReply> {
  const profile = await resolveCharacterSnapshot();
  return generate(settings, messages, nextText, profile);
}
