import React, { useRef, useState } from "react";
import { Modal, Pressable, ScrollView, StyleSheet, Text, View } from "react-native";
import { useTheme } from "react-native-paper";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import type { OperationsWorkReference } from "../../lib/operations-api";

/** A short, already scoped presentation projection, not a persisted domain model. */
export type ConversationWorkMapEntry = {
  id: string;
  parentId: string | null;
  instructorLabel: string;
  assignedAgentLabel: string;
  shortInstruction: string;
  status: string | null;
  workReference?: OperationsWorkReference | null;
};
export type ConversationWorkMapScopeKey = string;
export type ConversationWorkMapRelationship = { id: string; sourceLabel: string; targetLabel: string };
export type ConversationWorkMapData =
  | { scopeKey: ConversationWorkMapScopeKey; conversationId: string; state: "loading" | "error" }
  | { scopeKey: ConversationWorkMapScopeKey; conversationId: string; state: "ready"; entries: readonly ConversationWorkMapEntry[]; hasMore?: boolean; relationships?: readonly ConversationWorkMapRelationship[] };
export type ConversationWorkMapSheetProps = {
  visible: boolean;
  onDismiss: () => void;
  currentScopeKey: ConversationWorkMapScopeKey;
  currentConversationId: string;
  data: ConversationWorkMapData;
  onOpenWork?: (reference: OperationsWorkReference) => void;
  onRefresh?: () => void;
};

const PAGE_SIZE = 10;
const MAX_ENTRIES = 100;
const MAX_RELATIONSHIPS = 100;
const MAX_DEPTH = 4;

// Each entry is visited once, including orphans and cycles. Indentation is capped.
function orderedEntries(entries: readonly ConversationWorkMapEntry[]) {
  const limited = entries.slice(0, MAX_ENTRIES);
  const byId = new Map(limited.map((entry) => [entry.id, entry]));
  const children = new Map<string, ConversationWorkMapEntry[]>();
  for (const entry of limited) {
    if (entry.parentId && byId.has(entry.parentId)) {
      const siblings = children.get(entry.parentId) ?? [];
      siblings.push(entry);
      children.set(entry.parentId, siblings);
    }
  }
  const seen = new Set<string>();
  const result: { entry: ConversationWorkMapEntry; depth: number }[] = [];
  function visit(start: ConversationWorkMapEntry) {
    const stack = [{ entry: start, depth: 0 }];
    while (stack.length) {
      const item = stack.pop()!;
      if (seen.has(item.entry.id)) continue;
      seen.add(item.entry.id);
      result.push(item);
      const next = children.get(item.entry.id) ?? [];
      for (let i = next.length - 1; i >= 0; i--) {
        stack.push({ entry: next[i], depth: Math.min(item.depth + 1, MAX_DEPTH) });
      }
    }
  }
  limited.filter((entry) => !entry.parentId || !byId.has(entry.parentId)).forEach(visit);
  limited.forEach(visit);
  return result;
}

export function ConversationWorkMapSheet({ visible, onDismiss, currentScopeKey, currentConversationId, data, onOpenWork, onRefresh }: ConversationWorkMapSheetProps) {
  const theme = useTheme();
  const insets = useSafeAreaInsets();
  const [page, setPage] = useState({ generation: 0, count: PAGE_SIZE });
  const identity = useRef({ scopeKey: currentScopeKey, conversationId: currentConversationId, generation: 0 });
  if (identity.current.scopeKey !== currentScopeKey || identity.current.conversationId !== currentConversationId) {
    identity.current = { scopeKey: currentScopeKey, conversationId: currentConversationId, generation: identity.current.generation + 1 };
  }
  const generation = identity.current.generation;
  const count = page.generation === generation ? page.count : PAGE_SIZE;
  const scoped = data.scopeKey === currentScopeKey && data.conversationId === currentConversationId ? data : null;
  const rows = scoped?.state === "ready" ? orderedEntries(scoped.entries) : [];
  return (
    <Modal testID="work-map-modal" visible={visible} transparent animationType="slide" onRequestClose={onDismiss}>
      <View style={styles.backdrop}>
        <View accessibilityViewIsModal style={[styles.sheet, { backgroundColor: theme.colors.surface, paddingBottom: Math.max(insets.bottom, 12) }]}>
          <View style={styles.header}>
            <Text accessibilityRole="header" style={[styles.title, { color: theme.colors.onSurface }]}>会話の担当マップ</Text>
            <Pressable accessibilityRole="button" accessibilityLabel="担当マップを閉じる" onPress={onDismiss} style={styles.button}>
              <Text style={{ color: theme.colors.primary }}>閉じる</Text>
            </Pressable>
          </View>
          <ScrollView contentContainerStyle={styles.content}>
            {onRefresh && <Pressable accessibilityRole="button" accessibilityLabel="担当マップを更新" onPress={onRefresh} style={styles.button}><Text style={{ color: theme.colors.primary }}>更新</Text></Pressable>}
            {(!scoped || scoped.state === "loading") && <Text style={{ color: theme.colors.onSurface }}>読み込み中…</Text>}
            {scoped?.state === "error" && <Text style={{ color: theme.colors.error }}>担当マップを読み込めませんでした</Text>}
            {scoped?.state === "ready" && rows.length === 0 && <Text style={{ color: theme.colors.onSurface }}>この会話の担当はありません</Text>}
            {rows.slice(0, count).map(({ entry, depth }) => (
              <View key={entry.id} testID={`work-map-row-${entry.id}`} style={[styles.row, { marginLeft: depth * 12, borderLeftColor: theme.colors.outline }]}>
                <Text numberOfLines={2} ellipsizeMode="tail" style={[styles.rowText, { color: theme.colors.onSurface }]}>{entry.instructorLabel} → {entry.assignedAgentLabel}</Text>
                <Text numberOfLines={2} ellipsizeMode="tail" style={[styles.rowText, { color: theme.colors.onSurface }]}>{entry.shortInstruction}</Text>
                <Text numberOfLines={1} ellipsizeMode="tail" style={[styles.rowText, { color: theme.colors.onSurfaceVariant }]}>状態: {entry.status?.trim() || "不明"}</Text>
                {onOpenWork && entry.workReference?.kind === "work" && !!entry.workReference.id.trim() && (
                  <Pressable accessibilityRole="button" accessibilityLabel={`${entry.assignedAgentLabel}の作業を開く`} style={styles.button} onPress={() => { if (identity.current.generation === generation) onOpenWork(entry.workReference!); }}>
                    <Text style={{ color: theme.colors.primary }}>作業を開く</Text>
                  </Pressable>
                )}
              </View>
            ))}
            {scoped?.state === "ready" && scoped.relationships?.slice(0, MAX_RELATIONSHIPS).map((relationship) => (
              <Text key={relationship.id} numberOfLines={2} ellipsizeMode="tail" style={{ color: theme.colors.onSurfaceVariant }}>関連: {relationship.sourceLabel} → {relationship.targetLabel}</Text>
            ))}
            {scoped?.state === "ready" && (scoped.relationships?.length ?? 0) > MAX_RELATIONSHIPS && <Text style={{ color: theme.colors.onSurfaceVariant }}>関連は表示上限の100件まで表示しています</Text>}
            {rows.length > count && <Pressable accessibilityRole="button" accessibilityLabel="担当をさらに表示" style={styles.button} onPress={() => setPage({ generation, count: count + PAGE_SIZE })}><Text style={{ color: theme.colors.primary }}>さらに表示</Text></Pressable>}
            {scoped?.state === "ready" && scoped.entries.length > MAX_ENTRIES && <Text style={{ color: theme.colors.onSurfaceVariant }}>表示上限に達しました</Text>}
            {scoped?.state === "ready" && scoped.hasMore && <Text style={{ color: theme.colors.onSurfaceVariant }}>一部の担当のみ表示しています</Text>}
          </ScrollView>
        </View>
      </View>
    </Modal>
  );
}
const styles = StyleSheet.create({
  backdrop: { flex: 1, justifyContent: "flex-end", backgroundColor: "rgba(0,0,0,0.45)" },
  sheet: { maxHeight: "80%", borderTopLeftRadius: 16, borderTopRightRadius: 16 },
  header: { flexDirection: "row", alignItems: "center", justifyContent: "space-between", paddingHorizontal: 16 },
  title: { flex: 1, fontSize: 18, fontWeight: "600" },
  content: { paddingHorizontal: 16, paddingBottom: 12 },
  row: { borderLeftWidth: 2, paddingLeft: 10, paddingVertical: 8, minWidth: 0 },
  rowText: { flexShrink: 1 },
  button: { minHeight: 44, minWidth: 44, justifyContent: "center", alignItems: "center", alignSelf: "flex-start", paddingHorizontal: 8 },
});
