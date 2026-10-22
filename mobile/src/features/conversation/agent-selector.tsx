import React, { useMemo, useState } from "react";
import { Pressable, ScrollView, StyleSheet, View } from "react-native";
import {
  ActivityIndicator,
  Button,
  Dialog,
  Portal,
  Surface,
  Text,
} from "react-native-paper";
import type { ConversationAgent, ConversationSession } from "../../types/api";
import type { RunState } from "./models";

type AgentSelectorProps = {
  session: ConversationSession | null;
  agents: ConversationAgent[];
  loading: boolean;
  runState: RunState;
  pendingCount: number;
  onChange: (agentId: string | null) => Promise<void>;
  onRefresh: () => Promise<void>;
  onAdoptServerAgent?: () => Promise<void>;
  bindingLocked?: boolean;
};

function currentLabel(session: ConversationSession | null): string {
  if (!session?.agent_id) return "Agentなし";
  return session.agent_display_name?.trim() || `Agent ${session.agent_id.slice(0, 8)}`;
}

export function AgentSelector({
  session,
  agents,
  loading,
  runState,
  pendingCount,
  onChange,
  onRefresh,
  onAdoptServerAgent,
  bindingLocked = false,
}: AgentSelectorProps) {
  const [visible, setVisible] = useState(false);
  const [saving, setSaving] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const unavailable = bindingLocked || !session || session.is_group_chat || runState !== "idle";
  const selectionLocked = pendingCount > 0;
  const knownCurrent = useMemo(
    () => agents.some((agent) => agent.id === session?.agent_id),
    [agents, session?.agent_id],
  );

  const select = async (agentId: string | null) => {
    if (saving || unavailable || selectionLocked || agentId === (session?.agent_id ?? null)) return;
    setSaving(agentId ?? "none");
    setError(null);
    try {
      await onChange(agentId);
      setVisible(false);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Agentを変更できませんでした。");
    } finally {
      setSaving(null);
    }
  };

  return (
    <>
      <Pressable
        accessibilityRole="button"
        accessibilityLabel={`Agentを選択。現在は${currentLabel(session)}`}
        accessibilityState={{ disabled: unavailable }}
        disabled={unavailable}
        onPress={() => {
          setError(null);
          setVisible(true);
        }}
        style={[styles.chip, unavailable ? styles.disabled : null]}
      >
        <Text style={styles.label}>{currentLabel(session)}</Text>
        <Text style={styles.chevron}>⌄</Text>
      </Pressable>
      <Portal>
        <Dialog
          visible={visible}
          onDismiss={() => {
            if (!saving) setVisible(false);
          }}
          style={styles.dialog}
        >
          <Dialog.Title style={styles.title}>Agent主体を選択</Dialog.Title>
          <Dialog.Content>
            {bindingLocked ? (
              <Text style={styles.help}>AoiDots root のAgent bindingは固定されています。</Text>
            ) : session?.agent_binding_pending ? (
              <Surface style={styles.reasonCard} elevation={0}>
                <Text style={styles.help}>Agent変更は保留中です。未送信メッセージは元の expected binding を保持しています。</Text>
                {onAdoptServerAgent ? (
                  <Button
                    mode="outlined"
                    textColor="#f9e2af"
                    disabled={Boolean(saving)}
                    onPress={() => {
                      setError(null);
                      void onAdoptServerAgent()
                        .then(() => setVisible(false))
                        .catch((reason) => {
                          setError(reason instanceof Error ? reason.message : "サーバーのAgentへ戻せませんでした。");
                        });
                    }}
                  >
                    サーバーのAgentへ戻す（保留変更を破棄）
                  </Button>
                ) : null}
              </Surface>
            ) : session?.is_group_chat ? (
              <Text style={styles.help}>グループチャットにはprimary Agentを設定できません。参加Agentは既存の設定を使用します。</Text>
            ) : pendingCount > 0 ? (
              <Text style={styles.help}>未送信メッセージは元のAgent bindingを保持して再試行されます。</Text>
            ) : null}
            {error ? <Text accessibilityRole="alert" style={styles.error}>{error}</Text> : null}
            {loading ? <ActivityIndicator color="#7c3aed" accessibilityLabel="Agent一覧を読み込み中" /> : null}
            {!loading ? (
              <ScrollView style={styles.list} contentContainerStyle={styles.listContent}>
              <Pressable
                accessibilityRole="radio"
                accessibilityState={{ selected: !session?.agent_id, disabled: Boolean(saving) || selectionLocked }}
                disabled={Boolean(saving) || selectionLocked}
                onPress={() => void select(null)}
              >
                <Surface style={[styles.card, !session?.agent_id ? styles.selected : null]} elevation={0}>
                  <Text style={styles.name}>Agentなし</Text>
                  <Text style={styles.meta}>{!session?.agent_id ? "✓ 使用中" : ""}</Text>
                </Surface>
              </Pressable>
              {agents.map((agent) => {
                const selected = agent.id === session?.agent_id;
                return (
                  <Pressable
                    key={agent.id}
                    accessibilityRole="radio"
                    accessibilityLabel={agent.display_name}
                    accessibilityState={{ selected, disabled: Boolean(saving) || selectionLocked }}
                    disabled={Boolean(saving) || selectionLocked}
                    onPress={() => void select(agent.id)}
                  >
                    <Surface style={[styles.card, selected ? styles.selected : null]} elevation={0}>
                      <View style={styles.textWrap}>
                        <Text style={styles.name}>{agent.display_name}</Text>
                        <Text style={styles.meta}>{agent.state || "状態未取得"}{agent.character_name ? ` · ${agent.character_name}` : ""}</Text>
                      </View>
                      <Text style={styles.meta}>{saving === agent.id ? "変更中…" : selected ? "✓ 使用中" : ""}</Text>
                    </Surface>
                  </Pressable>
                );
              })}
              {!knownCurrent && session?.agent_id ? (
                <Text style={styles.help}>現在のAgentはオフライン一覧にありません。既存のbinding表示だけ保持しています。</Text>
              ) : null}
              {!agents.length ? <Text style={styles.help}>選択できるAgent一覧がありません。接続後に更新してください。</Text> : null}
              </ScrollView>
            ) : null}
          </Dialog.Content>
          <Dialog.Actions>
            <Button icon="refresh" textColor="#89b4fa" onPress={() => void onRefresh()} disabled={Boolean(saving) || loading}>更新</Button>
            <Button textColor="#a6adc8" onPress={() => setVisible(false)} disabled={Boolean(saving)}>閉じる</Button>
          </Dialog.Actions>
        </Dialog>
      </Portal>
    </>
  );
}

const styles = StyleSheet.create({
  chip: { width: "100%", maxWidth: "100%", minHeight: 44, minWidth: 0, flexShrink: 1, flexDirection: "row", alignItems: "flex-start", borderRadius: 12, paddingHorizontal: 6, paddingVertical: 5 },
  disabled: { opacity: 0.62 },
  label: { flex: 1, minWidth: 0, color: "#cdd6f4", fontSize: 12, fontWeight: "600", paddingVertical: 4 },
  chevron: { color: "#a6adc8", fontSize: 12, marginLeft: 3, paddingTop: 4, flexShrink: 0 },
  dialog: { backgroundColor: "#1e1e2e" },
  title: { color: "#cdd6f4" },
  reasonCard: { backgroundColor: "#2d2537", borderColor: "#c084fc", borderWidth: 1, borderRadius: 8, padding: 10, marginBottom: 8 },
  help: { color: "#a6adc8", fontSize: 12, lineHeight: 18, marginBottom: 8 },
  error: { color: "#f38ba8", fontSize: 12, lineHeight: 18, marginBottom: 8 },
  list: { maxHeight: 420 },
  listContent: { gap: 8, paddingVertical: 4 },
  card: { minHeight: 56, flexDirection: "row", alignItems: "center", backgroundColor: "#181825", borderColor: "#313244", borderWidth: 1, borderRadius: 10, paddingHorizontal: 12, paddingVertical: 9 },
  selected: { borderColor: "#89b4fa", backgroundColor: "#20283a" },
  textWrap: { flex: 1 },
  name: { color: "#cdd6f4", fontSize: 14, fontWeight: "700" },
  meta: { color: "#a6adc8", fontSize: 11, marginTop: 2 },
});
