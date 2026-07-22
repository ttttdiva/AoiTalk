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
  type UserSettings,
} from "@/lib/user-settings";

type UserSettingsContextType = {
  settings: UserSettings;
  editorLinkDefaultDisplayMode: EditorLinkDefaultDisplayMode;
  audioPlayerSettings: AudioPlayerSettings;
  refresh: () => void;
  patch: (patch: UserSettings) => Promise<UserSettings>;
};

const UserSettingsContext = createContext<UserSettingsContextType>({
  settings: {},
  editorLinkDefaultDisplayMode: "embed",
  audioPlayerSettings: getAudioPlayerSettings({}),
  refresh: () => {},
  patch: async () => ({}),
});

export const useUserSettings = () => useContext(UserSettingsContext);

export function UserSettingsProvider({
  children,
}: {
  children: React.ReactNode;
}) {
  const [settings, setSettings] = useState<UserSettings>({});
  const requestVersionRef = useRef(0);

  const refresh = useCallback(() => {
    const requestVersion = ++requestVersionRef.current;
    getUserSettings()
      .then((next) => {
        if (requestVersion === requestVersionRef.current) setSettings(next);
      })
      .catch(() => {});
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const patch = useCallback(async (settingsPatch: UserSettings) => {
    const requestVersion = ++requestVersionRef.current;
    const next = await patchUserSettings(settingsPatch);
    if (requestVersion === requestVersionRef.current) setSettings(next);
    return next;
  }, []);

  const value = useMemo<UserSettingsContextType>(
    () => ({
      settings,
      editorLinkDefaultDisplayMode: getEditorLinkDefaultDisplayMode(settings),
      audioPlayerSettings: getAudioPlayerSettings(settings),
      refresh,
      patch,
    }),
    [settings, refresh, patch],
  );

  return (
    <UserSettingsContext.Provider value={value}>
      {children}
    </UserSettingsContext.Provider>
  );
}
