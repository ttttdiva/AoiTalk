import { useCallback, useEffect, useRef, useState, type Dispatch, type SetStateAction } from "react";
import { getConfiguredApiServerFingerprint } from "../../lib/api-client";
import {
  CHAT_COMPOSER_DRAFT_MAX_LENGTH,
  clearChatComposerDraftIfRevisionMatches,
  chatComposerDraftScopeKey,
  readChatComposerDraft,
  type ChatComposerDraftRecord,
  type ChatComposerDraftScope,
  type ChatComposerDraftSnapshot,
  writeChatComposerDraft,
} from "./chat-composer-draft";
import { MOBILE_CHAT_COMMANDS, type MobileChatCommand } from "./chat-commands";

const DRAFT_WRITE_DEBOUNCE_MS = 250;

type DraftScopeInput = {
  authUser: string | null | undefined;
  conversationKey: string | null | undefined;
};

type SubmissionOptions = {
  clearUi?: boolean;
};

function newRevision(): string {
  return `${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function normalizeScopePart(value: string | null | undefined): string | null {
  const normalized = value?.trim();
  return normalized || null;
}

function resolveBuiltInCommand(command: string | null): MobileChatCommand | null {
  if (!command) return null;
  return MOBILE_CHAT_COMMANDS.find((item) => item.command === command) ?? null;
}

function snapshotFrom(
  scope: ChatComposerDraftScope | null,
  text: string,
  activeCommand: MobileChatCommand | null,
  revision: string,
): ChatComposerDraftSnapshot {
  return {
    scope,
    scopeKey: scope ? chatComposerDraftScopeKey(scope) : null,
    text: text.slice(0, CHAT_COMPOSER_DRAFT_MAX_LENGTH),
    activeCommand: activeCommand?.command ?? null,
    revision,
  };
}

export function useChatComposerDraft({ authUser, conversationKey }: DraftScopeInput) {
  const [input, setInputState] = useState("");
  const [activeCommand, setActiveCommandState] = useState<MobileChatCommand | null>(null);
  const [hydrated, setHydrated] = useState(false);
  const inputRef = useRef(input);
  const activeCommandRef = useRef(activeCommand);
  const scopeRef = useRef<ChatComposerDraftScope | null>(null);
  const scopeKeyRef = useRef<string | null>(null);
  const recordRef = useRef<ChatComposerDraftRecord | null>(null);
  const revisionRef = useRef(newRevision());
  const dirtyRef = useRef(false);
  const hydratedRef = useRef(false);
  const generationRef = useRef(0);
  const writeTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pendingSubmissionRef = useRef<ChatComposerDraftSnapshot | null>(null);
  const preserveScopeInputRef = useRef(false);
  const previousScopeIdentityRef = useRef<string | null>(null);
  inputRef.current = input;
  activeCommandRef.current = activeCommand;

  const cancelWriteTimer = useCallback(() => {
    if (writeTimerRef.current) {
      clearTimeout(writeTimerRef.current);
      writeTimerRef.current = null;
    }
  }, []);

  const writeCurrent = useCallback((scope: ChatComposerDraftScope, generation: number) => {
    if (generation !== generationRef.current || scopeKeyRef.current !== chatComposerDraftScopeKey(scope)) return;
    const record: ChatComposerDraftRecord = {
      version: 1,
      text: inputRef.current.slice(0, CHAT_COMPOSER_DRAFT_MAX_LENGTH),
      activeCommand: activeCommandRef.current?.command ?? null,
      revision: revisionRef.current,
      updatedAt: Date.now(),
    };
    recordRef.current = record;
    void writeChatComposerDraft(scope, record).catch(() => undefined);
  }, []);

  const scheduleWrite = useCallback((scope: ChatComposerDraftScope, generation: number) => {
    cancelWriteTimer();
    writeTimerRef.current = setTimeout(() => {
      writeTimerRef.current = null;
      writeCurrent(scope, generation);
    }, DRAFT_WRITE_DEBOUNCE_MS);
  }, [cancelWriteTimer, writeCurrent]);

  const flushCurrent = useCallback((generation: number) => {
    cancelWriteTimer();
    const scope = scopeRef.current;
    if (!scope || generation !== generationRef.current || !dirtyRef.current) return;
    writeCurrent(scope, generation);
  }, [cancelWriteTimer, writeCurrent]);

  const setInput = useCallback<Dispatch<SetStateAction<string>>>((next) => {
    dirtyRef.current = true;
    revisionRef.current = newRevision();
    setInputState((current) => {
      const value = typeof next === "function" ? next(current) : next;
      return String(value).slice(0, CHAT_COMPOSER_DRAFT_MAX_LENGTH);
    });
  }, []);

  const setActiveCommand = useCallback<Dispatch<SetStateAction<MobileChatCommand | null>>>((next) => {
    dirtyRef.current = true;
    revisionRef.current = newRevision();
    setActiveCommandState(next);
  }, []);

  const beginSubmission = useCallback((options: SubmissionOptions = {}): ChatComposerDraftSnapshot | null => {
    if (!hydratedRef.current) return null;
    cancelWriteTimer();
    const snapshot = snapshotFrom(
      scopeRef.current,
      inputRef.current,
      activeCommandRef.current,
      revisionRef.current,
    );
    pendingSubmissionRef.current = snapshot;
    if (options.clearUi) {
      dirtyRef.current = false;
      setInputState("");
      setActiveCommandState(null);
    }
    return snapshot;
  }, [cancelWriteTimer]);

  const restoreSubmission = useCallback((snapshot: ChatComposerDraftSnapshot) => {
    if (snapshot.scopeKey !== scopeKeyRef.current || revisionRef.current !== snapshot.revision) return;
    dirtyRef.current = false;
    setInputState(snapshot.text);
    setActiveCommandState(resolveBuiltInCommand(snapshot.activeCommand));
  }, []);

  const acceptSubmission = useCallback((snapshot: ChatComposerDraftSnapshot) => {
    if (snapshot.scopeKey !== scopeKeyRef.current || revisionRef.current !== snapshot.revision) return;
    void clearChatComposerDraftIfRevisionMatches(snapshot).catch(() => false);
    pendingSubmissionRef.current = null;
    dirtyRef.current = false;
    recordRef.current = null;
    setInputState("");
    setActiveCommandState(null);
  }, []);

  const rejectSubmission = useCallback((snapshot: ChatComposerDraftSnapshot) => {
    if (snapshot.scopeKey !== scopeKeyRef.current || revisionRef.current !== snapshot.revision) return;
    if (pendingSubmissionRef.current?.revision === snapshot.revision) {
      pendingSubmissionRef.current = null;
    }
    if (snapshot.scope) {
      const record: ChatComposerDraftRecord = {
        version: 1,
        text: snapshot.text,
        activeCommand: snapshot.activeCommand,
        revision: snapshot.revision,
        updatedAt: Date.now(),
      };
      void writeChatComposerDraft(snapshot.scope, record).catch(() => undefined);
    }
    restoreSubmission(snapshot);
  }, [restoreSubmission]);

  const scopeIdentity = (() => {
    // An unknown auth user never falls back to a shared "anonymous" key;
    // otherwise one account's private draft could be shown to another.
    const user = normalizeScopePart(authUser);
    const conversation = normalizeScopePart(conversationKey);
    return user && conversation ? JSON.stringify([user, conversation]) : null;
  })();

  useEffect(() => {
    const previousIdentity = previousScopeIdentityRef.current;
    previousScopeIdentityRef.current = scopeIdentity;
    const generation = ++generationRef.current;
    const preserveInput = Boolean(
      scopeIdentity &&
      previousIdentity !== null &&
      previousIdentity !== scopeIdentity &&
      (dirtyRef.current || Boolean(pendingSubmissionRef.current?.text)),
    );
    const shouldReset = scopeIdentity === null || (
      previousIdentity !== null && previousIdentity !== scopeIdentity
    );
    cancelWriteTimer();
    scopeRef.current = null;
    scopeKeyRef.current = null;
    recordRef.current = null;
    pendingSubmissionRef.current = null;
    hydratedRef.current = false;
    preserveScopeInputRef.current = preserveInput;
    setHydrated(false);
    if (preserveInput) {
      // Keep the visible draft for the current in-flight interaction, but do
      // not write it into the new account/conversation scope unless the user
      // edits it after the switch.
      dirtyRef.current = false;
    } else if (shouldReset) {
      dirtyRef.current = false;
      setInputState("");
      setActiveCommandState(null);
    }
    if (!scopeIdentity) {
      hydratedRef.current = true;
      setHydrated(true);
      return () => {
        flushCurrent(generation);
      };
    }

    let active = true;
    void getConfiguredApiServerFingerprint()
      .then((serverProfile) => {
        if (!active || generation !== generationRef.current) return;
        const user = normalizeScopePart(authUser);
        const conversation = normalizeScopePart(conversationKey);
        const profile = normalizeScopePart(serverProfile);
        if (!user || !conversation || !profile) {
          hydratedRef.current = true;
          setHydrated(true);
          return;
        }
        const scope: ChatComposerDraftScope = {
          authUser: user,
          serverProfile: profile,
          conversationKey: conversation,
        };
        const key = chatComposerDraftScopeKey(scope);
        scopeRef.current = scope;
        scopeKeyRef.current = key;
        void readChatComposerDraft(scope).then((stored) => {
          if (!active || generation !== generationRef.current || scopeKeyRef.current !== key) return;
          recordRef.current = stored;
          hydratedRef.current = true;
          setHydrated(true);
          if (preserveScopeInputRef.current) {
            preserveScopeInputRef.current = false;
            if (dirtyRef.current) scheduleWrite(scope, generation);
            return;
          }
          if (!dirtyRef.current) {
            revisionRef.current = stored?.revision ?? newRevision();
            setInputState(stored?.text ?? "");
            setActiveCommandState(resolveBuiltInCommand(stored?.activeCommand ?? null));
            return;
          }
          // User input arrived while the async read was pending. Preserve it
          // and write it after hydration; never treat the initial empty value
          // as a request to delete stored text.
          scheduleWrite(scope, generation);
        }).catch(() => {
          if (!active || generation !== generationRef.current || scopeKeyRef.current !== key) return;
          hydratedRef.current = true;
          setHydrated(true);
        });
      })
      .catch(() => {
        if (active && generation === generationRef.current) {
          hydratedRef.current = true;
          setHydrated(true);
        }
      });

    return () => {
      active = false;
      flushCurrent(generation);
    };
  }, [authUser, cancelWriteTimer, conversationKey, flushCurrent, scheduleWrite, scopeIdentity]);

  useEffect(() => {
    const scope = scopeRef.current;
    if (!scope || !hydratedRef.current || !dirtyRef.current) return;
    scheduleWrite(scope, generationRef.current);
  }, [activeCommand, input, scheduleWrite]);

  return {
    input,
    activeCommand,
    hydrated,
    setInput,
    setActiveCommand,
    beginSubmission,
    acceptSubmission,
    rejectSubmission,
  };
}
