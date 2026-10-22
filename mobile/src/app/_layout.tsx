/**
 * Root Layout — AuthProvider + PaperProvider
 */

import { useEffect, useState } from "react";
import { AppState, Linking, Pressable, Text, View } from "react-native";
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
import { getToken, getTokenAuthScope } from "../lib/auth";
import { useSyncStatusStore } from "../stores/sync-status";
import { useProjectStore } from "../stores/project";
import { scheduleSyncAfterInteractions } from "../lib/background-sync";
import { setSyncExecutionActive } from "../sync/engine";
import { checkForUpdate, showUpdateAlert } from "../lib/update-service";
import { configureNetworkEndpointRouting } from "../lib/connection-routing";
import { completePortalCallback } from "../lib/portal-auth";
import { isGatewayCallbackUrl } from "../constants/config";
import {
  initializeLocalNotifications,
  installLocalNotificationResponseHandler,
  rescheduleLocalTaskNotificationsFromCache,
} from "../lib/local-notifications";
import { AoiDotsProvider } from "../features/aoidots/AoiDotsOverlay";
import { ReadOnlyDiagnosticsBadge } from "../components/read-only-diagnostics-badge";

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

  // Gateway OAuth returns to the app scheme while the app may be cold, warm,
  // or resumed from the browser. Complete the SDK exchange at the root so the
  // callback is handled exactly once and never rendered as an Expo Router
  // screen (the native-intent shim below also redirects it to Login).
  useEffect(() => {
    const handled = new Set<string>();
    const handleGatewayCallback = async (url: string | null) => {
      if (!url || !isGatewayCallbackUrl(url) || handled.has(url)) return;
      handled.add(url);
      try {
        await completePortalCallback(url);
      } catch {
        // The SDK persists retryable exchange state. Keep callback credentials
        // out of UI errors and logs; Login/Connection settings can retry.
      }
    };

    const subscription = Linking.addEventListener("url", ({ url }) => {
      void handleGatewayCallback(url);
    });
    void Linking.getInitialURL().then(handleGatewayCallback).catch(() => undefined);
    return () => subscription.remove();
  }, []);

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
    // Root refreshes talk to the AoiTalk API.  `online` is Internet-only and
    // must not suppress a server hosted on the current LAN.  The `online`
    // fallback keeps older test/embedding state objects compatible.
    const hasServerNetworkPath = (state: {
      connected?: boolean;
      online?: boolean;
    }): boolean => state.connected ?? state.online ?? false;
    const scheduleRootSync = () => {
      if (
        currentAppState !== "active" ||
        !hasServerNetworkPath(useNetworkStore.getState())
      ) {
        return;
      }
      // Spaces are not part of the delta sync. Give a missing Space cache a
      // head start before the potentially long general/Docs sync occupies the
      // auth-scope queue. A complete cache stays entirely local/no-op here.
      const projectState = useProjectStore.getState();
      const knownSpaceIds = new Set(projectState.spaces.map((space) => space.id));
      const needsSpaceCatchUp =
        projectState.spaces.length === 0 ||
        projectState.projects.some(
          (project) =>
            Boolean(project.space_id) &&
            !knownSpaceIds.has(project.space_id!),
        );
      if (needsSpaceCatchUp) {
        void projectState.refreshProjects({ spacesOnly: true });
      }

      cancelScheduledSync?.();
      cancelScheduledSync = scheduleSyncAfterInteractions(() =>
        useProjectStore.getState().refreshProjects({ spacesOnly: true }),
      );
    };
    const unsubscribe = useNetworkStore.subscribe((state, prevState) => {
      if (hasServerNetworkPath(state) && !hasServerNetworkPath(prevState)) {
        scheduleRootSync();
      }
    });
    // General data is committed while Docs still owns the auth-scope queue.
    // Never queue a canonical/remote refresh from this notification.
    let rootActive = true;
    let appGeneration = 0;
    const unsubscribeGeneral = useSyncStatusStore.subscribe((state, previous) => {
      if (
        state.generalRevision === previous.generalRevision ||
        !state.generalAuthScope || currentAppState !== "active"
      ) return;
      const { generalRevision, generalAuthScope } = state;
      const generation = appGeneration;
      void getToken().then((token) => {
        const latest = useSyncStatusStore.getState();
        if (!rootActive || generation !== appGeneration || currentAppState !== "active" ||
            latest.generalRevision !== generalRevision ||
            latest.generalAuthScope !== generalAuthScope ||
            getTokenAuthScope(token) !== generalAuthScope) return;
        return useProjectStore.getState().refreshProjects({ localOnly: true });
      }).catch(() => undefined);
    });
    const appStateSubscription = AppState.addEventListener(
      "change",
      (nextAppState) => {
        ++appGeneration;
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
      rootActive = false;
      unsubscribeGeneral();
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
                    <AoiDotsProvider>
                      <AppSidebar>
                        <Slot />
                        <ReadOnlyDiagnosticsBadge />
                      </AppSidebar>
                      <TaskCompletionUndoStack />
                    </AoiDotsProvider>
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
