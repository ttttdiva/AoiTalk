import React, { useCallback, useMemo, useState } from "react";
import { Pressable, View } from "react-native";
import { Chip, Menu, Surface, Text } from "react-native-paper";
import type { ConversationSession, Project } from "../../../types/api";
import { buildConnectionStatusPresentation } from "../capabilities";
import type { ConversationDiagnostics } from "../models";
import { CharacterSelector } from "../character-selector";
import { chatScreenStyles as styles } from "./chat-screen.styles";

export type ChatHeaderStatusProps = {
  diagnostics: ConversationDiagnostics;
  session: ConversationSession | null;
  projects: Project[];
  currentProjectId: string | null;
  pendingCount: number;
  onRefreshProjects: () => Promise<void>;
  onChangeProject: (projectId: string | null) => Promise<void>;
  onChangeCharacter: (slug: string) => Promise<void>;
  onOpenPendingQueue: () => void;
};

export const ChatHeaderStatus = React.memo(function ChatHeaderStatus({
  diagnostics,
  session,
  projects,
  currentProjectId,
  pendingCount,
  onRefreshProjects,
  onChangeProject,
  onChangeCharacter,
  onOpenPendingQueue,
}: ChatHeaderStatusProps) {
  const [projectMenuVisible, setProjectMenuVisible] = useState(false);
  const [projectSaving, setProjectSaving] = useState(false);
  const [projectError, setProjectError] = useState<string | null>(null);
  const connectionStatus = buildConnectionStatusPresentation(diagnostics);
  const currentProject = useMemo(
    () => projects.find((project) => project.id === currentProjectId) ?? null,
    [currentProjectId, projects],
  );
  const changeProject = useCallback(
    async (projectId: string | null) => {
      setProjectSaving(true);
      setProjectError(null);
      try {
        await onChangeProject(projectId);
        setProjectMenuVisible(false);
      } catch (error) {
        setProjectError(
          error instanceof Error
            ? error.message
            : "プロジェクトの変更に失敗しました。",
        );
      } finally {
        setProjectSaving(false);
      }
    },
    [onChangeProject],
  );

  return (
    <>
      <Surface style={styles.connectionBar} elevation={0}>
        <View
          accessibilityRole="image"
          accessibilityLabel={`${connectionStatus.label}: ${connectionStatus.detail}`}
          style={[
            styles.connectionDot,
            { backgroundColor: connectionStatus.color },
          ]}
        />
        {connectionStatus.healthy ? null : (
          <Text style={styles.connectionLabel} numberOfLines={1}>
            {connectionStatus.label}
          </Text>
        )}
        <Menu
          visible={projectMenuVisible}
          onDismiss={() => setProjectMenuVisible(false)}
          anchor={
            <Chip
              compact
              icon="folder-outline"
              style={styles.projectChip}
              textStyle={styles.projectChipText}
              onPress={() => {
                setProjectMenuVisible(true);
                void onRefreshProjects();
              }}
              accessibilityLabel={`チャットのプロジェクト: ${currentProject?.name || "全体"}`}
            >
              {currentProject?.name || "全体"}
            </Chip>
          }
          contentStyle={styles.menuContent}
        >
          <Menu.Item
            title="全体"
            leadingIcon={!currentProjectId ? "check" : undefined}
            disabled={projectSaving}
            onPress={() => void changeProject(null)}
          />
          {projects.map((project) => (
            <Menu.Item
              key={project.id}
              title={project.name}
              leadingIcon={project.id === currentProjectId ? "check" : undefined}
              disabled={projectSaving}
              onPress={() => void changeProject(project.id)}
            />
          ))}
        </Menu>
        <CharacterSelector
          session={session}
          runState={diagnostics.runState}
          onChange={onChangeCharacter}
        />
        {pendingCount > 0 ? (
          <Pressable
            accessibilityRole="button"
            accessibilityLabel={`未送信メッセージ ${pendingCount}件`}
            style={styles.pendingBadge}
            onPress={onOpenPendingQueue}
          >
            <Text style={styles.pendingBadgeText}>未送信 {pendingCount}</Text>
          </Pressable>
        ) : null}
      </Surface>
      {projectError ? <Text style={styles.projectError}>{projectError}</Text> : null}
    </>
  );
});
