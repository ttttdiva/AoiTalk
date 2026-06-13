/**
 * Root Layout — AuthProvider + PaperProvider
 */

import { useEffect, useState } from "react";
import { View } from "react-native";
import { Slot } from "expo-router";
import { PaperProvider, MD3DarkTheme } from "react-native-paper";
import { StatusBar } from "expo-status-bar";
import * as ScreenOrientation from "expo-screen-orientation";
import { PersistQueryClientProvider } from "@tanstack/react-query-persist-client";
import { SafeAreaProvider } from "react-native-safe-area-context";
import { AuthProvider } from "../contexts/AuthContext";
import { AppSidebar } from "../components/app-sidebar";
import { TaskCompletionUndoStack } from "../components/task-completion-undo-stack";
import { ProjectProvider } from "../contexts/ProjectContext";
import { ensureSchema } from "../db/migrate";
import { queryClient, asyncStoragePersister } from "../query/client";
import { useNetworkStore } from "../stores/network";
import { runSync } from "../sync/engine";
import { checkForUpdate, showUpdateAlert } from "../lib/update-service";
import { configureNetworkEndpointRouting } from "../lib/connection-routing";
import {
  initializeLocalNotifications,
  installLocalNotificationResponseHandler,
  rescheduleLocalTaskNotificationsFromCache,
} from "../lib/local-notifications";

const theme = {
  ...MD3DarkTheme,
  colors: {
    ...MD3DarkTheme.colors,
    primary: "#7c3aed",
    primaryContainer: "#4c1d95",
    secondary: "#06b6d4",
    surface: "#1e1e2e",
    background: "#11111b",
    surfaceVariant: "#313244",
    onSurface: "#cdd6f4",
    onSurfaceVariant: "#a6adc8",
    onBackground: "#cdd6f4",
    outline: "#585b70",
    error: "#f38ba8",
  },
};

export default function RootLayout() {
  const [ready, setReady] = useState(false);

  useEffect(() => {
    void ScreenOrientation.lockAsync(
      ScreenOrientation.OrientationLock.PORTRAIT_UP,
    ).catch(() => {});
    configureNetworkEndpointRouting();
    ensureSchema();
    useNetworkStore.getState().start();
    void initializeLocalNotifications().then((enabled) => {
      if (enabled) void rescheduleLocalTaskNotificationsFromCache();
    });
    const removeNotificationResponseHandler =
      installLocalNotificationResponseHandler();
    const unsubscribe = useNetworkStore.subscribe((state, prevState) => {
      if (state.online && !prevState.online) {
        void runSync();
      }
    });
    const syncInterval = setInterval(() => {
      if (useNetworkStore.getState().online) {
        void runSync();
      }
    }, 60_000);
    void runSync();
    setReady(true);
    return () => {
      removeNotificationResponseHandler();
      clearInterval(syncInterval);
      unsubscribe();
      useNetworkStore.getState().stop();
    };
  }, []);

  useEffect(() => {
    void checkForUpdate().then((result) => {
      if (result.available) {
        showUpdateAlert(result);
      }
    });
  }, []);

  return (
    <SafeAreaProvider>
      <PaperProvider theme={theme}>
        <PersistQueryClientProvider
          client={queryClient}
          persistOptions={{ persister: asyncStoragePersister }}
        >
          <AuthProvider>
            <ProjectProvider>
              <StatusBar style="light" />
              {ready ? (
                <AppSidebar>
                  <Slot />
                </AppSidebar>
              ) : (
                <View
                  style={{ flex: 1, backgroundColor: theme.colors.background }}
                />
              )}
              <TaskCompletionUndoStack />
            </ProjectProvider>
          </AuthProvider>
        </PersistQueryClientProvider>
      </PaperProvider>
    </SafeAreaProvider>
  );
}
