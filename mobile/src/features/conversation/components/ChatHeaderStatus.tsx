import React from "react";
import { ScrollView, StyleSheet, useWindowDimensions, View } from "react-native";
import { IconButton } from "react-native-paper";
import { ScopeSwitcher } from "../../../components/scope-switcher";
import type { ConversationSession, Project } from "../../../types/api";
import type { ConversationAgent } from "../../../types/api";
import type { ConversationDiagnostics } from "../models";
import { conversationPerformanceDiagnostics } from "../performance-diagnostics";
import { CharacterSelector } from "../character-selector";
import { AgentSelector } from "../agent-selector";
import { AoiDotsTrigger } from "../../aoidots/context";
import { useChatWaveTouchGuard } from "./chat-wave-touch-context";

export type ChatHeaderStatusProps = {
  diagnostics: ConversationDiagnostics;
  session: ConversationSession | null;
  projects: Project[];
  currentProjectId: string | null;
  pendingCount: number;
  onChangeProject: (projectId: string | null) => Promise<void>;
  onChangeCharacter: (slug: string) => Promise<void>;
  agents?: ConversationAgent[];
  agentCatalogLoading?: boolean;
  onChangeAgent?: (agentId: string | null) => Promise<void>;
  onRefreshAgents?: () => Promise<void>;
  onAdoptServerAgent?: () => Promise<void>;
  onNewSameAgentChat?: () => void;
  onOpenPendingQueue: () => void;
  onBack?: () => void;
  onRenameTitle?: () => void;
};

export function isProtectedAoiDotsRootSession(session: ConversationSession | null): boolean {
  if (session?.private_agent_root === true) return true;
  if (session?.purpose === "aoidots" && session.readonly_binding === true) return true;
  // Nested context is not authoritative. Backend flat projections are the
  // only marker used to lock ordinary Chat mutation controls.
  return false;
}

/** Compact native navigation controls that keep long names readable. */
export const ChatHeaderStatus = React.memo(function ChatHeaderStatus({
  diagnostics, session, projects, currentProjectId, pendingCount,
  onChangeProject, onChangeCharacter, agents = [], agentCatalogLoading = false,
  onChangeAgent, onRefreshAgents, onNewSameAgentChat, onBack, onRenameTitle,
  onAdoptServerAgent,
}: ChatHeaderStatusProps) {
  conversationPerformanceDiagnostics.recordRender("ChatHeaderStatus");
  const { height: windowHeight } = useWindowDimensions();
  const headerMaxHeight = Number.isFinite(windowHeight) && windowHeight > 0
    ? Math.max(1, Math.floor(windowHeight * 0.5))
    : 360;
  const rootLocked = isProtectedAoiDotsRootSession(session);
  const markWaveTouchIgnored = useChatWaveTouchGuard();
  return (
    <View testID="chat-header" style={[styles.header, { maxHeight: headerMaxHeight }]} onTouchStart={markWaveTouchIgnored}>
      <ScrollView
        testID="chat-header-scroll"
        style={styles.contentScroll}
        horizontal={false}
        nestedScrollEnabled
        keyboardShouldPersistTaps="handled"
        showsVerticalScrollIndicator
      >
      <View style={styles.row}>
        <View style={styles.navigation}>
          <IconButton icon="arrow-left" size={22} iconColor="#cdd6f4"
            style={styles.icon} onPress={onBack} accessibilityLabel="チャット一覧へ戻る" />
          <View style={styles.actions}>
            {onNewSameAgentChat && session?.agent_id && !rootLocked ? <IconButton icon="plus-box-outline" size={19}
              iconColor="#89b4fa" style={styles.action} onPress={onNewSameAgentChat}
              accessibilityLabel="同じAgentで新しいChatを開始" /> : null}
            {onRenameTitle ? <IconButton icon="pencil-outline" size={19}
              iconColor="#a6adc8" style={styles.action} onPress={onRenameTitle}
              accessibilityLabel="セッションタイトルを変更" /> : null}
            <AoiDotsTrigger compact />
          </View>
        </View>
        <View style={styles.selectors}>
          <View style={styles.selector}>
            <ScopeSwitcher variant="inline" projectNameOnly projects={projects}
              projectId={currentProjectId} onSelectProject={onChangeProject}
              allLabel="プロジェクト未選択"
              accessibilityLabel="プロジェクトを選択"
              disabled={rootLocked || diagnostics.runState !== "idle" || pendingCount > 0} />
          </View>
          <View style={styles.selector}>
            <CharacterSelector session={session} runState={diagnostics.runState}
              onChange={onChangeCharacter}
              locked={rootLocked} />
          </View>
          <View style={styles.selector}>
            <AgentSelector session={session} agents={agents} loading={agentCatalogLoading}
              runState={diagnostics.runState} pendingCount={pendingCount}
              onChange={onChangeAgent ?? (async () => undefined)}
              bindingLocked={rootLocked}
              onRefresh={onRefreshAgents ?? (async () => undefined)}
              onAdoptServerAgent={onAdoptServerAgent} />
          </View>
        </View>
      </View>
      </ScrollView>
    </View>
  );
});

const styles = StyleSheet.create({
  header: { width: "100%", minWidth: 0, backgroundColor: "#1e1e2e" },
  contentScroll: { flexGrow: 0, flexShrink: 1 },
  row: { width: "100%", minWidth: 0, alignItems: "stretch", gap: 4 },
  navigation: { width: "100%", minWidth: 0, minHeight: 52, flexDirection: "row", alignItems: "center", justifyContent: "space-between", paddingRight: 4 },
  icon: { width: 44, height: 44, minWidth: 44, minHeight: 44, margin: 0 },
  selectors: {
    width: "100%",
    minWidth: 0,
    maxWidth: "100%",
    flexDirection: "row",
    flexWrap: "wrap",
    alignItems: "flex-start",
    gap: 4,
  },
  selector: { flexGrow: 1, flexShrink: 1, flexBasis: 90, minWidth: 0, maxWidth: "100%", minHeight: 44 },
  actions: { flexDirection: "row", alignItems: "center", flexShrink: 0, minWidth: 0, minHeight: 44 },
  action: { width: 44, height: 44, minWidth: 44, minHeight: 44, margin: 0 },
});
