import React, { useCallback, useEffect, useRef, useState } from "react";
import { Alert, ScrollView, StyleSheet, View } from "react-native";
import { Button, Text } from "react-native-paper";
import { getConflictStore } from "../../sync-v3/contract";
import type { ConflictKind, ConflictRecord } from "../../sync-v3/contract";
import { kindLabel } from "./status-presentation";

const REAPPLY_KINDS: ConflictKind[] = ["field_conflict", "migrated_unresolved", "scope_unknown"];

export type SyncConflictsScreenProps = {
  authScope: string;
  onCopy?: (text: string) => void;
  onCreateTaskFromConflict?: (conflict: ConflictRecord) => void;
};

function formatValue(value: unknown): string {
  if (value === undefined) return "(なし)";
  if (typeof value === "string") return value;
  return JSON.stringify(value);
}

function ValueBlock({ title, value }: { title: string; value: unknown }) {
  const [open, setOpen] = useState(false);
  const isJson = value !== null && typeof value === "object";
  if (isJson && !open) {
    return (
      <View style={styles.value}>
        <Text variant="labelSmall">{title}</Text>
        <Button compact onPress={() => setOpen(true)}>
          JSONを表示
        </Button>
      </View>
    );
  }
  return (
    <View style={styles.value}>
      <Text variant="labelSmall">{title}</Text>
      <Text variant="bodySmall" selectable>
        {isJson ? JSON.stringify(value, null, 2) : formatValue(value)}
      </Text>
    </View>
  );
}

export function SyncConflictsScreen({
  authScope,
  onCopy,
  onCreateTaskFromConflict,
}: SyncConflictsScreenProps) {
  const [loaded, setLoaded] = useState<{ scope: string; items: ConflictRecord[] }>({
    scope: authScope,
    items: [],
  });
  // 要求世代。authScope の変更・unmount・新しい reload で進め、古い完了結果は捨てる。
  const generation = useRef(0);
  const currentScope = useRef(authScope);
  currentScope.current = authScope;
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const reload = useCallback(async () => {
    // 切替済みscope・unmount後の操作完了からの再読込は、現在scopeの要求世代を壊さないよう何もしない。
    if (!mounted.current || currentScope.current !== authScope) return;
    const mine = ++generation.current;
    const store = getConflictStore();
    const next = store ? await store.listOpen(authScope) : [];
    if (generation.current !== mine || !mounted.current) return;
    setLoaded({ scope: authScope, items: next });
  }, [authScope]);

  useEffect(() => {
    void reload();
    return () => {
      generation.current += 1;
    };
  }, [reload]);

  // 表示は現在の authScope の結果だけ。切替直後に旧scopeの一覧を出さない。
  const items = loaded.scope === authScope ? loaded.items : [];

  const reapply = async (id: string) => {
    await getConflictStore()?.reapply(id);
    await reload();
  };

  const confirmDismiss = (id: string) => {
    Alert.alert("この項目を破棄", "この端末の変更は反映されません。破棄しますか？", [
      { text: "キャンセル", style: "cancel" },
      {
        text: "破棄",
        style: "destructive",
        onPress: () => {
          void (async () => {
            await getConflictStore()?.dismiss(id);
            await reload();
          })();
        },
      },
    ]);
  };

  if (items.length === 0) {
    return <Text variant="bodyMedium">確認が必要な項目はありません。</Text>;
  }

  return (
    <ScrollView contentContainerStyle={styles.list}>
      {items.map((item) => (
        <View key={item.id} style={styles.item} testID={`conflict-${item.id}`}>
          <Text variant="titleSmall">
            {item.title_hint ?? item.entity_id}（{item.table}）
          </Text>
          <Text variant="labelMedium">{kindLabel(item.kind)}</Text>
          {item.fields.map((field) => (
            <View key={field} style={styles.field}>
              <Text variant="labelMedium">{field}</Text>
              <ValueBlock title="この端末" value={item.local[field]} />
              <ValueBlock title="サーバー" value={item.server?.[field]} />
            </View>
          ))}
          <View style={styles.actions}>
            {REAPPLY_KINDS.includes(item.kind) ? (
              <Button mode="contained" onPress={() => void reapply(item.id)}>
                この端末の値で上書き
              </Button>
            ) : null}
            {item.kind === "entity_deleted" && item.table === "tasks" && onCreateTaskFromConflict ? (
              <Button mode="contained" onPress={() => onCreateTaskFromConflict(item)}>
                新規タスクとして作成
              </Button>
            ) : null}
            <Button
              onPress={() =>
                (onCopy ?? (() => undefined))(
                  JSON.stringify({ local: item.local, server: item.server, base: item.base }, null, 2),
                )
              }
              disabled={!onCopy}
            >
              内容をコピー
            </Button>
            <Button onPress={() => confirmDismiss(item.id)}>破棄</Button>
          </View>
        </View>
      ))}
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  list: { gap: 12 },
  item: { gap: 6 },
  field: { gap: 2 },
  value: { gap: 2 },
  actions: { flexDirection: "row", flexWrap: "wrap", gap: 8 },
});
