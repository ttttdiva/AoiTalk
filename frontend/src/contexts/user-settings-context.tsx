"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
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

  const refresh = useCallback(() => {
    getUserSettings()
      .then(setSettings)
      .catch(() => {});
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const patch = useCallback(async (settingsPatch: UserSettings) => {
    const next = await patchUserSettings(settingsPatch);
    setSettings(next);
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
