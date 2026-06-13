import React, { useCallback, useState } from "react";
import { ScrollView, StyleSheet, View } from "react-native";
import { useFocusEffect, useRouter } from "expo-router";
import { goBackOrReplace } from "../../../lib/navigation";
import { Button, IconButton, List, Surface, Switch, Text } from "react-native-paper";
import { useAuth } from "../../../contexts/AuthContext";
import { settingsApi } from "../../../lib/settings-api";
import type { AppSettings } from "../../../types/api";

type ToggleRow = {
  title: string;
  description: string;
  keyPath: string;
  value: boolean;
};

export default function SettingsMcpScreen() {
  const router = useRouter();
  const { isAuthenticated } = useAuth();
  const [settings, setSettings] = useState<AppSettings | null>(null);

  const load = useCallback(async () => {
    if (!isAuthenticated) {
      setSettings(null);
      return;
    }
    const data = await settingsApi.get();
    setSettings(data.settings);
  }, [isAuthenticated]);

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
          keyPath: "rag.enabled",
          value: settings.rag.enabled,
        },
      ]
    : [];

  return (
    <View style={styles.container}>
      <Surface style={styles.header} elevation={1}>
        <View style={styles.headerRow}>
          <IconButton
            icon="arrow-left"
            iconColor="#cdd6f4"
            onPress={() => goBackOrReplace(router, '/(tabs)/settings')}
          />
          <View style={{ flex: 1 }}>
            <Text variant="titleLarge" style={styles.headerTitle}>
              MCP & Agents
            </Text>
            <Text style={styles.headerSubtext}>
              Server-side integration toggles.
            </Text>
          </View>
        </View>
      </Surface>

      <ScrollView contentContainerStyle={styles.content}>
        {!isAuthenticated ? (
          <Surface style={styles.card} elevation={0}>
            <Text style={styles.description}>
              MCP とエージェント設定はサーバーログイン中のみ利用できます。
            </Text>
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
                  onValueChange={async (value) => {
                    await settingsApi.update(toggle.keyPath, value, true);
                    await load();
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
                    onPress={async () => {
                      await settingsApi.update(
                        "reasoning.display_mode",
                        mode,
                        true,
                      );
                      await load();
                    }}
                  >
                    {mode}
                  </Button>
                ),
              )}
            </View>
          </Surface>
        ) : null}
      </ScrollView>
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: "#11111b" },
  header: {
    paddingTop: 52,
    paddingHorizontal: 8,
    paddingBottom: 16,
    backgroundColor: "#1e1e2e",
  },
  headerRow: { flexDirection: "row", alignItems: "center" },
  headerTitle: { color: "#cdd6f4", fontWeight: "bold" },
  headerSubtext: { color: "#a6adc8", marginTop: 2 },
  content: { padding: 16, gap: 12, paddingBottom: 32 },
  card: { backgroundColor: "#1e1e2e", borderRadius: 12 },
  buttonGrid: {
    flexDirection: "row",
    flexWrap: "wrap",
    gap: 8,
    paddingHorizontal: 16,
    paddingBottom: 16,
  },
  title: { color: "#cdd6f4" },
  description: { color: "#a6adc8" },
});
