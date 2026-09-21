import React from "react";
import { StyleSheet, View } from "react-native";
import { IconButton } from "react-native-paper";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { ScopeSwitcher } from "../../../components/scope-switcher";
import type { ConversationSession, Project } from "../../../types/api";
import type { ConversationDiagnostics } from "../models";
import { conversationPerformanceDiagnostics } from "../performance-diagnostics";
import { CharacterSelector } from "../character-selector";

export type ChatHeaderStatusProps = {
  diagnostics: ConversationDiagnostics;
  session: ConversationSession | null;
  projects: Project[];
  currentProjectId: string | null;
  pendingCount: number;
  onChangeProject: (projectId: string | null) => Promise<void>;
  onChangeCharacter: (slug: string) => Promise<void>;
  onOpenPendingQueue: () => void;
  onBack?: () => void;
  onRenameTitle?: () => void;
};

/** One native navigation row. Names are truncated, never wrapped into a second toolbar. */
export const ChatHeaderStatus = React.memo(function ChatHeaderStatus({
  diagnostics, session, projects, currentProjectId, pendingCount,
  onChangeProject, onChangeCharacter, onBack, onRenameTitle,
}: ChatHeaderStatusProps) {
  conversationPerformanceDiagnostics.recordRender("ChatHeaderStatus");
  const insets = useSafeAreaInsets();
  return (
    <View testID="chat-header" style={[styles.header, { paddingTop: insets.top }]}>
      <View style={styles.row}>
        <IconButton icon="arrow-left" size={22} iconColor="#cdd6f4"
          style={styles.icon} onPress={onBack} accessibilityLabel="チャット一覧へ戻る" />
        <View style={styles.project}>
          <ScopeSwitcher variant="inline" projectNameOnly projects={projects}
            projectId={currentProjectId} onSelectProject={onChangeProject}
            allLabel="プロジェクト未選択"
            accessibilityLabel="プロジェクトを選択"
            disabled={diagnostics.runState !== "idle" || pendingCount > 0} />
        </View>
        <View style={styles.character}>
          <CharacterSelector session={session} runState={diagnostics.runState}
            onChange={onChangeCharacter} />
        </View>
        {onRenameTitle ? <IconButton icon="pencil-outline" size={19}
          iconColor="#a6adc8" style={styles.rename} onPress={onRenameTitle}
          accessibilityLabel="セッションタイトルを変更" /> : null}
      </View>
    </View>
  );
});

const styles = StyleSheet.create({
  header: { backgroundColor: "#1e1e2e" },
  row: { height: 52, flexDirection: "row", alignItems: "center", paddingRight: 4, gap: 4 },
  icon: { width: 44, height: 44, margin: 0 },
  rename: { width: 44, height: 44, margin: 0 },
  project: { flex: 1, minWidth: 0 },
  character: { flex: 1, minWidth: 0 },
});
