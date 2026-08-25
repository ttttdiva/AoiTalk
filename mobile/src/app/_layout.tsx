/**
 * Root Layout — AuthProvider + PaperProvider
 */

import { useEffect, useState } from "react";
import { AppState, Pressable, Text, View } from "react-native";
import { Slot } from "expo-router";
import { PaperProvider, MD3DarkTheme } from "react-native-paper";
import { StatusBar } from "expo-status-bar";
import * as ScreenOrientation from "expo-screen-orientation";
import { PersistQueryClientProvider } from "@tanstack/react-query-persist-client";
import { SafeAreaProvider } from "react-native-safe-area-context";
import { KeyboardProvider } from "react-native-keyboard-controller";
import { AuthProvider } from "../contexts/AuthContext";
import { AppSidebar } from "../components/app-sidebar";
import { KeyboardAwareLayout } from "../components/keyboard-aware-layout";
import { TaskCompletionUndoStack } from "../components/task-completion-undo-stack";
import { ProjectProvider } from "../contexts/ProjectContext";
import { ensureSchema, ensureSchemaAsync } from "../db/migrate";
import { queryClient, asyncStoragePersister } from "../query/client";
import { useNetworkStore } from "../stores/network";
import { useProjectStore } from "../stores/project";
import { scheduleSyncAfterInteractions } from "../lib/background-sync";
import { setSyncExecutionActive } from "../sync/engine";
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

function SchemaBootstrapScreen({
  status,
  onRetry,
}: {
  status: "pending" | "error";
  onRetry: () => void;
}) {
  const failed = status === "error";
  return (
    <View
      style={{
        flex: 1,
        alignItems: "center",
        justifyContent: "center",
        gap: 16,
        paddingHorizontal: 28,
        backgroundColor: theme.colors.background,
      }}
    >
      <Text style={{ color: theme.colors.onBackground, fontSize: 17, textAlign: "center" }}>
        {failed
          ? "ローカルデータの準備に失敗しました"
          : "ローカルデータを準備しています…"}
      </Text>
      <Text style={{ color: theme.colors.onSurfaceVariant, textAlign: "center" }}>
        {failed
          ? "データを削除せずに再試行できます。"
          : "完了するまで同期や画面遷移は開始しません。"}
      </Text>
      {failed ? (
        <Pressable
          accessibilityRole="button"
          accessibilityLabel="ローカルデータの準備を再試行"
          onPress={onRetry}
          style={{
            borderRadius: 8,
            backgroundColor: theme.colors.primary,
            paddingHorizontal: 20,
            paddingVertical: 10,
          }}
        >
          <Text style={{ color: "#ffffff", fontWeight: "600" }}>再試行</Text>
        </Pressable>
      ) : null}
    </View>
  );
}

export default function RootLayout() {
  const [schemaState, setSchemaState] = useState<"pending" | "ready" | "error">(
    "pending",
  );
  const [schemaAttempt, setSchemaAttempt] = useState(0);

  useEffect(() => {
    void ScreenOrientation.lockAsync(
      ScreenOrientation.OrientationLock.PORTRAIT_UP,
    ).catch(() => {});
    configureNetworkEndpointRouting();
    // Keep the first shell render responsive and surface bootstrap failures as
    // a retryable state instead of throwing from startup.
    setSyncExecutionActive(false);
  }, []);

  useEffect(() => {
    let cancelled = false;
    const timer = setTimeout(() => {
      // Keep the bootstrap off the JS thread.  The sync fallback only exists
      // for older test doubles/embedders that have not exposed the async API.
      void (async () => {
        try {
          if (ensureSchemaAsync) {
            await ensureSchemaAsync();
          } else {
            ensureSchema();
          }
          if (!cancelled) setSchemaState("ready");
        } catch (error) {
          if (cancelled) return;
          console.warn(
            "[startup] local schema bootstrap failed",
            error instanceof Error ? error.message : "UnknownError",
          );
          setSchemaState("error");
        }
      })();
    }, 0);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [schemaAttempt]);

  useEffect(() => {
    if (schemaState !== "ready") return;

    useNetworkStore.getState().start();
    void initializeLocalNotifications()
      .then((enabled) => {
        if (enabled) void rescheduleLocalTaskNotificationsFromCache();
      })
      .catch(() => undefined);
    const removeNotificationResponseHandler =
      installLocalNotificationResponseHandler();
    let currentAppState = AppState.currentState;
    setSyncExecutionActive(currentAppState === "active");
    let cancelScheduledSync: (() => void) | null = null;
    const scheduleRootSync = () => {
      if (
        currentAppState !== "active" ||
        !useNetworkStore.getState().online
      ) {
        return;
      }
      cancelScheduledSync?.();
      cancelScheduledSync = scheduleSyncAfterInteractions(() =>
        useProjectStore.getState().refreshProjects({ localOnly: true }),
      );
    };
    const unsubscribe = useNetworkStore.subscribe((state, prevState) => {
      if (state.online && !prevState.online) {
        scheduleRootSync();
      }
    });
    const appStateSubscription = AppState.addEventListener(
      "change",
      (nextAppState) => {
        const wasActive = currentAppState === "active";
        currentAppState = nextAppState;
        setSyncExecutionActive(nextAppState === "active");
        if (nextAppState !== "active") {
          cancelScheduledSync?.();
          cancelScheduledSync = null;
          return;
        }
        if (!wasActive) scheduleRootSync();
      },
    );
    const syncInterval = setInterval(() => {
      scheduleRootSync();
    }, 60_000);
    scheduleRootSync();
    return () => {
      removeNotificationResponseHandler();
      cancelScheduledSync?.();
      setSyncExecutionActive(false);
      appStateSubscription.remove();
      clearInterval(syncInterval);
      unsubscribe();
      useNetworkStore.getState().stop();
    };
  }, [schemaState]);

  useEffect(() => {
    if (schemaState !== "ready") return;
    void checkForUpdate().then((result) => {
      if (result.available) {
        showUpdateAlert(result);
      }
    });
  }, [schemaState]);

  const retrySchema = () => {
    setSchemaState("pending");
    setSchemaAttempt((attempt) => attempt + 1);
  };

  return (
    <SafeAreaProvider>
      <KeyboardProvider>
        <PaperProvider theme={theme}>
          <KeyboardAwareLayout>
            <PersistQueryClientProvider
              client={queryClient}
              persistOptions={{ persister: asyncStoragePersister }}
            >
              <AuthProvider>
                <StatusBar style="light" />
                {schemaState === "ready" ? (
                  <ProjectProvider>
                    <AppSidebar>
                      <Slot />
                    </AppSidebar>
                    <TaskCompletionUndoStack />
                  </ProjectProvider>
                ) : (
                  <SchemaBootstrapScreen
                    status={schemaState}
                    onRetry={retrySchema}
                  />
                )}
              </AuthProvider>
            </PersistQueryClientProvider>
          </KeyboardAwareLayout>
        </PaperProvider>
      </KeyboardProvider>
    </SafeAreaProvider>
  );
}
