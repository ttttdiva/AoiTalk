import React, { useCallback, useState } from "react";
import { StyleSheet, View } from "react-native";
import { useFocusEffect, useRouter } from "expo-router";
import { goBackOrReplace } from "../../../lib/navigation";
import { Button, List, Surface, Switch, Text } from "react-native-paper";
import { ScreenHeader } from "../../../components/screen-header";
import { ScreenShell } from "../../../components/screen-primitives";
import { useAuth } from "../../../contexts/AuthContext";
import { settingsApi } from "../../../lib/settings-api";
import type { AppSettings } from "../../../types/api";

type ToggleRow = {
  title: string;
  description: string;
  keyPath: string;
  value: boolean;
};

function getSettingsErrorMessage(
  error: unknown,
  fallback: string,
): string {
  return error instanceof Error && error.message
    ? error.message
    : fallback;
}

export default function SettingsMcpScreen() {
  const router = useRouter();
  const { isAuthenticated } = useAuth();
  const [settings, setSettings] = useState<AppSettings | null>(null);
  const [settingsError, setSettingsError] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!isAuthenticated) {
      setSettings(null);
      setSettingsError(null);
      return;
    }
    try {
      const data = await settingsApi.get();
      setSettings(data.settings);
      setSettingsError(null);
    } catch (error) {
      setSettings(null);
      setSettingsError(
        getSettingsErrorMessage(
          error,
          "設定を取得できませんでした。通信状態を確認して再読み込みしてください。",
        ),
      );
    }
  }, [isAuthenticated]);

  const updateSetting = useCallback(
    async (keyPath: string, value: boolean | string) => {
      try {
        setSettingsError(null);
        await settingsApi.update(keyPath, value, true);
        await load();
      } catch (error) {
        setSettingsError(
          getSettingsErrorMessage(
            error,
            "設定を保存できませんでした。通信状態を確認して再試行してください。",
          ),
        );
      }
    },
    [load],
  );

  useFocusEffect(
    useCallback(() => {
      void load();
    }, [load]),
  );

  const toggles: ToggleRow[] = settings
    ? [
        {
          title: "External LLM Auto Approval",
          description: "Allow external LLM tool requests without manual approval.",
          keyPath: "external_llm.auto_approve",
          value: settings.external_llm.auto_approve,
        },
        {
          title: "MCP",
          description: "Enable MCP-backed app integrations.",
          keyPath: "mcp_enabled",
          value: settings.agents.mcp.enabled,
        },
        {
          title: "Filesystem Agent",
          description: "Allow filesystem-backed agent features.",
          keyPath: "agents.filesystem.enabled",
          value: settings.agents.filesystem.enabled,
        },
        {
          title: "Project Management Agent",
          description: "Enable project-management automation.",
          keyPath: "agents.project_management.enabled",
          value: settings.agents.project_management.enabled,
        },
        {
          title: "Reasoning",
          description: "Enable multi-step planning and reasoning behavior.",
          keyPath: "reasoning.enabled",
          value: settings.reasoning.enabled,
        },
        {
          title: "Spotify Agent",
          description: "Enable the Spotify assistant agent.",
          keyPath: "agents.spotify.enabled",
          value: settings.agents.spotify.enabled,
        },
        {
          title: "Spotify Integration",
          description: "Enable Spotify features globally.",
          keyPath: "spotify.enabled",
          value: settings.spotify.enabled,
        },
        {
          title: "RAG",
          description: "Enable retrieval-augmented features.",
          keyPath: "search.knowledge_enabled",
          value: settings.search?.knowledge_enabled ?? false,
        },
      ]
    : [];

  return (
    <ScreenShell
      scroll
      style={styles.container}
      contentContainerStyle={styles.content}
      header={
        <ScreenHeader
          title="MCP & Agents"
          subtitle="Server-side integration toggles."
          onBack={() => goBackOrReplace(router, "/(tabs)/settings")}
        />
      }
    >
        {!isAuthenticated ? (
          <Surface style={styles.card} elevation={0}>
            <Text style={styles.description}>
              MCP とエージェント設定はサーバーログイン中のみ利用できます。
            </Text>
          </Surface>
        ) : null}
        {isAuthenticated && settingsError ? (
          <Surface style={styles.card} elevation={0}>
            <Text style={styles.description}>{settingsError}</Text>
            <Button
              mode="outlined"
              textColor="#cdd6f4"
              onPress={() => {
                void load();
              }}
              style={styles.retryButton}
            >
              再読み込み
            </Button>
          </Surface>
        ) : null}
        {toggles.map((toggle) => (
          <Surface key={toggle.keyPath} style={styles.card} elevation={0}>
            <List.Item
              title={toggle.title}
              description={toggle.description}
              titleStyle={styles.title}
              descriptionStyle={styles.description}
              right={() => (
                <Switch
                  value={toggle.value}
                  onValueChange={(value) => {
                    void updateSetting(toggle.keyPath, value);
                  }}
                />
              )}
            />
          </Surface>
        ))}
        {settings ? (
          <Surface style={styles.card} elevation={0}>
            <List.Item
              title="Reasoning Display Mode"
              description="Controls how much reasoning progress is shown."
              titleStyle={styles.title}
              descriptionStyle={styles.description}
            />
            <View style={styles.buttonGrid}>
              {(["silent", "progress", "detailed", "debug"] as const).map(
                (mode) => (
                  <Button
                    key={mode}
                    mode={
                      settings.reasoning.display_mode === mode
                        ? "contained"
                        : "outlined"
                    }
                    buttonColor={
                      settings.reasoning.display_mode === mode
                        ? "#7c3aed"
                        : undefined
                    }
                    textColor="#cdd6f4"
                    compact
                    onPress={() => {
                      void updateSetting("reasoning.display_mode", mode);
                    }}
                  >
                    {mode}
                  </Button>
                ),
              )}
            </View>
          </Surface>
        ) : null}
    </ScreenShell>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: "#11111b" },
  content: { padding: 16, gap: 12, paddingBottom: 32 },
  card: { backgroundColor: "#1e1e2e", borderRadius: 12 },
  buttonGrid: {
    flexDirection: "row",
    flexWrap: "wrap",
    gap: 8,
    paddingHorizontal: 16,
    paddingBottom: 16,
  },
  retryButton: { marginTop: 12, alignSelf: "flex-start" },
  title: { color: "#cdd6f4" },
  description: { color: "#a6adc8" },
});
