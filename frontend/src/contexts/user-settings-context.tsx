"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  getEditorLinkDefaultDisplayMode,
  getAudioPlayerSettings,
  getUserSettings,
  patchUserSettings,
  type AudioPlayerSettings,
  type EditorLinkDefaultDisplayMode,
  type UserSettingsRequestOptions,
  type UserSettings,
} from "@/lib/user-settings";

export type UserSettingsLoadStatus = "idle" | "loading" | "ready" | "error";

type UserSettingsContextType = {
  settings: UserSettings;
  /** Stable authenticated owner for settings-scoped client migrations. */
  userId?: string | null;
  /** `undefined` means this hook is being used outside the app provider. */
  settingsReady?: boolean;
  /** The latest GET failure, if settings could not be hydrated. */
  settingsError?: unknown;
  settingsLoadStatus?: UserSettingsLoadStatus;
  editorLinkDefaultDisplayMode: EditorLinkDefaultDisplayMode;
  audioPlayerSettings: AudioPlayerSettings;
  refresh: () => void;
  patch: (
    patch: UserSettings,
    options?: UserSettingsRequestOptions,
  ) => Promise<UserSettings>;
};

const UserSettingsContext = createContext<UserSettingsContextType>({
  settings: {},
  userId: undefined,
  settingsReady: undefined,
  settingsError: undefined,
  settingsLoadStatus: "idle",
  editorLinkDefaultDisplayMode: "embed",
  audioPlayerSettings: getAudioPlayerSettings({}),
  refresh: () => {},
  patch: async () => ({}),
});

export const useUserSettings = () => useContext(UserSettingsContext);

export function UserSettingsProvider({
  children,
  userId = null,
}: {
  children: React.ReactNode;
  userId?: string | null;
}) {
  const [settings, setSettings] = useState<UserSettings>({});
  const [settingsReady, setSettingsReady] = useState(false);
  const [settingsError, setSettingsError] = useState<unknown>(undefined);
  const [settingsLoadStatus, setSettingsLoadStatus] =
    useState<UserSettingsLoadStatus>("idle");
  const requestVersionRef = useRef(0);
  const mountedRef = useRef(false);
  const activeUserIdRef = useRef<string | null | undefined>(userId);
  const initializedRef = useRef(false);
  // 同時に走る refresh() を1リクエストへ束ねる（StrictMode の二重実行や
  // 複数箇所からの初期化で、同じ設定を何度も取りに行かないようにする）。
  const inFlightRefreshRef = useRef<Promise<void> | null>(null);

  const refresh = useCallback(() => {
    if (inFlightRefreshRef.current) return;
    const requestVersion = ++requestVersionRef.current;
    setSettingsLoadStatus("loading");
    setSettingsError(undefined);
    const refreshPromise = getUserSettings()
      .then((next) => {
        if (
          mountedRef.current &&
          requestVersion === requestVersionRef.current
        ) {
          setSettings(next);
          setSettingsError(undefined);
          setSettingsLoadStatus("ready");
        }
      })
      .catch((error) => {
        if (
          mountedRef.current &&
          requestVersion === requestVersionRef.current
        ) {
          setSettingsError(error);
          setSettingsLoadStatus("error");
        }
      })
      .finally(() => {
        // A PATCH/user switch can advance requestVersion while this GET is
        // still in flight.  Clear only our own promise in that case; leaving
        // the stale promise installed would permanently disable later
        // refreshes because the guard at the top returns early.
        if (inFlightRefreshRef.current === refreshPromise) {
          inFlightRefreshRef.current = null;
        }
        if (
          mountedRef.current &&
          requestVersion === requestVersionRef.current
        ) {
          setSettingsReady(true);
        }
      });
    inFlightRefreshRef.current = refreshPromise;
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      requestVersionRef.current += 1;
      inFlightRefreshRef.current = null;
      initializedRef.current = false;
    };
  }, []);

  useEffect(() => {
    // A provider can remain mounted across sign-out/sign-in. Clear the old
    // account before issuing the new account's GET so a stale snapshot can
    // never be rendered or patched under the next user.
    const userChanged =
      !initializedRef.current || activeUserIdRef.current !== userId;
    if (!userChanged) return;
    activeUserIdRef.current = userId;
    initializedRef.current = true;
    requestVersionRef.current += 1;
    inFlightRefreshRef.current = null;
    window.queueMicrotask(() => {
      if (!mountedRef.current || activeUserIdRef.current !== userId) return;
      setSettings({});
      setSettingsError(undefined);
      setSettingsReady(false);
      setSettingsLoadStatus("idle");
      refresh();
    });
  }, [refresh, userId]);

  useEffect(() => {
    // A tab can be opened while offline. Refreshing on recovery lets scoped
    // consumers hydrate authoritative settings without requiring a reload.
    const handleOnline = () => refresh();
    window.addEventListener("online", handleOnline);
    return () => window.removeEventListener("online", handleOnline);
  }, [refresh]);

  const patch = useCallback(
    async (
      settingsPatch: UserSettings,
      _options?: UserSettingsRequestOptions,
    ) => {
      const ownerUserId = activeUserIdRef.current;
      const requestVersion = ++requestVersionRef.current;
      const wasSettingsReady = settingsReady;
      let next: UserSettings;
      try {
        next =
          _options === undefined
            ? await patchUserSettings(settingsPatch)
            : await patchUserSettings(settingsPatch, _options);
      } catch (error) {
        if (
          mountedRef.current &&
          ownerUserId === activeUserIdRef.current &&
          requestVersion === requestVersionRef.current
        ) {
          if (!wasSettingsReady) setSettingsError(error);
          setSettingsReady(true);
          setSettingsLoadStatus("error");
        }
        throw error;
      }
      if (
        mountedRef.current &&
        ownerUserId === activeUserIdRef.current &&
        requestVersion === requestVersionRef.current
      ) {
        setSettings(next);
        setSettingsError(undefined);
        setSettingsReady(true);
        setSettingsLoadStatus("ready");
      }
      return next;
    },
    [settingsReady],
  );

  const value = useMemo<UserSettingsContextType>(
    () => ({
      settings,
      userId,
      settingsReady,
      settingsError,
      settingsLoadStatus,
      editorLinkDefaultDisplayMode: getEditorLinkDefaultDisplayMode(settings),
      audioPlayerSettings: getAudioPlayerSettings(settings),
      refresh,
      patch,
    }),
    [
      settings,
      userId,
      settingsReady,
      settingsError,
      settingsLoadStatus,
      refresh,
      patch,
    ],
  );

  return (
    <UserSettingsContext.Provider value={value}>
      {children}
    </UserSettingsContext.Provider>
  );
}
