"use client";

import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { useOptionalRuntimeContext } from "@/contexts/runtime-context";
import { DEFAULT_PREFERENCES, normalizePreferences, type PetPreferences, type StoredPet } from "@/lib/pets/codex-pet";
import { importPetFiles } from "@/lib/pets/pet-archive";
import { loadPet, savePet } from "@/lib/pets/pet-storage";
import { loadPetPreferences, savePetPreferences } from "@/lib/pets/pet-preferences";
import { deleteServerPet, fetchPetImage, fetchServerPet, registerServerPet } from "@/lib/pets/pet-api";
import { PET_REFRESH_MS, PetRequestError, type ServerPet } from "@/lib/pets/pet-server-contract";

type PetContextValue = {
  available: boolean; ready: boolean; connected: boolean; busy: boolean;
  pet: StoredPet | null; registered: ServerPet | null; legacyPet: StoredPet | null;
  error: string | null; warning: string | null; notice: string | null;
  importFiles: (files: readonly File[]) => Promise<boolean>;
  migrateLegacy: () => Promise<boolean>;
  updatePreferences: (patch: Partial<PetPreferences>) => Promise<boolean>;
  remove: () => Promise<boolean>;
  reload: () => void;
};
const PetContext = createContext<PetContextValue | null>(null);
export const usePet = () => useContext(PetContext);
const message = (cause: unknown) => cause instanceof Error ? cause.message : "ペットの処理に失敗しました。";

/** The server owns registration/assets. Local data is presentation or explicit legacy import only. */
export function PetStateProvider({ children, userId, available }: { children: ReactNode; userId: string | null; available: boolean }) {
  const [pet, setPet] = useState<StoredPet | null>(null);
  const [registered, setRegistered] = useState<ServerPet | null>(null);
  const [legacyPet, setLegacyPet] = useState<StoredPet | null>(null);
  const [ready, setReady] = useState(false);
  const [connected, setConnected] = useState(false);
  const [busy, setBusy] = useState(false);
  const [operationError, setOperationError] = useState<string | null>(null);
  const [syncError, setSyncError] = useState<string | null>(null);
  const [warning, setWarning] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const registrationRef = useRef<ServerPet | null>(null);
  const imageRef = useRef<{ revision: string; image: Blob } | null>(null);
  const preferencesRef = useRef<PetPreferences>({ ...DEFAULT_PREFERENCES });
  const preferenceEdits = useRef(0);
  const connectedRef = useRef(false);
  const busyRef = useRef(false);
  const epochRef = useRef(0);
  const requestRef = useRef(0);
  const sessionRef = useRef<AbortController | null>(null);
  const cancelReadRef = useRef<() => void>(() => {});
  const refreshRef = useRef<() => void>(() => {});
  const channelRef = useRef<BroadcastChannel | null>(null);
  const enabled = available && !!userId;

  const present = useCallback(() => {
    const current = registrationRef.current;
    const cached = imageRef.current;
    setPet(current && cached?.revision === current.revision
      ? { version: 1, manifest: current.manifest, image: cached.image, preferences: preferencesRef.current } : null);
  }, []);

  useEffect(() => {
    const epoch = ++epochRef.current;
    const session = new AbortController();
    sessionRef.current = session;
    let read: AbortController | null = null;
    let retry: ReturnType<typeof setTimeout> | undefined;
    let channel: BroadcastChannel | null = null;
    const current = () => epoch === epochRef.current && !session.signal.aborted;
    const cancelRead = () => { ++requestRef.current; read?.abort(); };
    cancelReadRef.current = cancelRead;
    if (!enabled || !userId) return () => { session.abort(); ++epochRef.current; };
    connectedRef.current = false;
    busyRef.current = false;
    // A new auth/capability scope must first consult the server, never the local asset.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setReady(false);
    let localPreferences = false;
    try {
      const saved = loadPetPreferences(userId);
      if (saved) { preferencesRef.current = saved; localPreferences = true; }
    } catch {
      // Report unavailable external storage without blocking the server read.
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setWarning("このブラウザでは表示設定を保存できません。サーバーのペット登録には影響しません。");
    }
    const editAtLoad = preferenceEdits.current;
    // Read the old store only to offer an explicit, create-only migration.
    void loadPet(userId).then((legacy) => {
      if (!current()) return;
      setLegacyPet(legacy);
      if (legacy && !localPreferences && preferenceEdits.current === editAtLoad) {
        preferencesRef.current = legacy.preferences;
        present();
      }
    }).catch(() => { /* IndexedDB being unavailable must never block server pets. */ });

    const refresh = async () => {
      if (!current() || busyRef.current) return;
      cancelRead();
      const serial = requestRef.current;
      read = new AbortController();
      const signal = read.signal; // Scope cleanup also aborts this read.
      const valid = () => current() && serial === requestRef.current && !signal.aborted;
      let metadataRead = false;
      try {
        const next = await fetchServerPet(signal);
        if (!valid()) return;
        metadataRead = true;
        connectedRef.current = true; setConnected(true);
        registrationRef.current = next; setRegistered(next);
        if (!next) {
          imageRef.current = null; setPet(null); setSyncError(null);
          return;
        }
        if (imageRef.current?.revision !== next.revision) {
          // A replaced/deleted asset must not keep masquerading as the current pet.
          imageRef.current = null; setPet(null);
          const image = await fetchPetImage(next.revision, signal);
          if (!valid()) return;
          imageRef.current = { revision: next.revision, image };
        }
        present(); setSyncError(null);
      } catch (cause) {
        if (!valid()) return;
        if (!metadataRead) { connectedRef.current = false; setConnected(false); }
        if (cause instanceof PetRequestError && [401, 403].includes(cause.status)) {
          registrationRef.current = null; imageRef.current = null;
          setRegistered(null); setPet(null);
          connectedRef.current = false; setConnected(false);
        }
        setSyncError(message(cause));
        if (cause instanceof PetRequestError && [404, 409].includes(cause.status) && metadataRead) {
          clearTimeout(retry);
          retry = setTimeout(() => { void refresh(); }, 500);
        }
      } finally { if (valid()) setReady(true); }
    };
    const refreshNow = () => { void refresh(); };
    const whenVisible = () => { if (document.visibilityState === "visible") refreshNow(); };
    refreshRef.current = refreshNow;
    try {
      channel = new BroadcastChannel("aoitalk-server-pet-v1");
      channel.onmessage = refreshNow; channelRef.current = channel;
    } catch { /* Polling/focus also cover browsers without BroadcastChannel. */ }
    const timer = setInterval(whenVisible, PET_REFRESH_MS);
    window.addEventListener("focus", refreshNow);
    window.addEventListener("online", refreshNow);
    document.addEventListener("visibilitychange", whenVisible);
    refreshNow();
    return () => {
      ++epochRef.current; session.abort(); cancelRead();
      clearInterval(timer); clearTimeout(retry); channel?.close(); channelRef.current = null;
      window.removeEventListener("focus", refreshNow);
      window.removeEventListener("online", refreshNow);
      document.removeEventListener("visibilitychange", whenVisible);
      refreshRef.current = () => {};
    };
  }, [enabled, userId, present]);

  const persistPreferences = useCallback((patch: Partial<PetPreferences>) => {
    preferencesRef.current = normalizePreferences({ ...preferencesRef.current, ...patch });
    ++preferenceEdits.current;
    present();
    try {
      if (userId) savePetPreferences(userId, preferencesRef.current);
      setWarning(null);
    } catch { setWarning("表示設定は今回の画面だけに適用しました。サーバーのペット登録は保持されています。"); }
  }, [present, userId]);

  const mutate = useCallback(async (
    action: (signal: AbortSignal) => Promise<ServerPet | null>, success: string, enable = false,
  ): Promise<boolean> => {
    if (!enabled || !userId || !ready || !connectedRef.current || busyRef.current || !sessionRef.current) return false;
    const epoch = epochRef.current;
    const signal = sessionRef.current.signal;
    const current = () => epoch === epochRef.current && !signal.aborted;
    busyRef.current = true; setBusy(true); setOperationError(null); setNotice(null);
    cancelReadRef.current();
    try {
      const next = await action(signal);
      if (!current()) return false;
      registrationRef.current = next; imageRef.current = null;
      setRegistered(next); setPet(null); setNotice(success);
      if (enable) persistPreferences({ enabled: true });
      try { channelRef.current?.postMessage("changed"); } catch { /* Periodic refresh remains active. */ }
      return true;
    } catch (cause) {
      if (current()) setOperationError(message(cause));
      return false;
    } finally {
      if (current()) { busyRef.current = false; setBusy(false); refreshRef.current(); }
    }
  }, [enabled, userId, ready, persistPreferences]);

  const importFiles = useCallback((files: readonly File[]) => {
    const createOnly = registrationRef.current === null;
    return mutate(async (signal) => {
      const imported = await importPetFiles(files);
      signal.throwIfAborted();
      return registerServerPet(imported.manifest, imported.image, createOnly, signal);
    }, "このAoiTalkサーバーにペットを登録しました。他のブラウザ・端末でも再登録せず利用できます。", true);
  }, [mutate]);
  const migrateLegacy = useCallback(async () => {
    if (!legacyPet || registrationRef.current) return false;
    const ok = await mutate((signal) => registerServerPet(legacyPet.manifest, legacyPet.image, true, signal),
      "このブラウザのペットをAoiTalkサーバーへ移行しました。", true);
    if (ok) {
      setLegacyPet(null);
      // A failed legacy cleanup must never undo successful server registration.
      if (userId) await savePet(userId, null).catch(() => {});
    }
    return ok;
  }, [legacyPet, mutate, userId]);
  const updatePreferences = useCallback(async (patch: Partial<PetPreferences>) => {
    if (!enabled || !userId || !registrationRef.current || busyRef.current) return false;
    persistPreferences(patch);
    return true;
  }, [enabled, userId, persistPreferences]);
  const remove = useCallback(() => mutate(async (signal) => {
    await deleteServerPet(signal); return null;
  }, "このサーバーからペットの登録を削除しました。他のブラウザ・端末にも反映されます。"), [mutate]);
  const reload = useCallback(() => refreshRef.current(), []);
  return <PetContext.Provider value={{
    available: enabled, ready, connected, busy, pet: enabled ? pet : null,
    registered: enabled ? registered : null, legacyPet: enabled ? legacyPet : null,
    error: operationError ?? syncError, warning, notice, importFiles, migrateLegacy, updatePreferences, remove, reload,
  }}>{children}</PetContext.Provider>;
}

export function PetProvider({ children, userId }: { children: ReactNode; userId: string | null }) {
  const runtime = useOptionalRuntimeContext();
  const available = runtime?.runtimeFeatures?.application_features?.entertainment === true;
  // Capability/auth changes remount local view state and abort the previous scope.
  return <PetStateProvider key={`${userId ?? "anonymous"}:${available}`} userId={userId} available={available}>{children}</PetStateProvider>;
}
