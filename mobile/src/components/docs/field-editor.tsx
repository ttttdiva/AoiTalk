/**
 * 型付きフィールド値エディタ
 *
 * ノードのタグに紐づくフィールド定義を取得し、field_type 別の入力 UI を出す。
 * 保存は docsRepo.setField(nodeId, fieldId, value)。value は生値でサーバへ渡し、
 * サーバが型別に格納・task 系 system_key の連携を行う（UI 側で特別分岐は不要）。
 */

import React, { useCallback, useEffect, useState } from "react";
import { ScrollView, StyleSheet, View } from "react-native";
import {
  ActivityIndicator,
  Button,
  Dialog,
  List,
  Menu,
  Portal,
  Switch,
  Text,
  TextInput,
} from "react-native-paper";
import { docsRepo } from "../../repositories/docs";
import type { DocsField, DocsFieldValue, DocsNode } from "../../types/api";

type FieldEntry = { field: DocsField; value: DocsFieldValue | null };

type FieldEditorProps = {
  nodeId: string;
  /** タグ変更などで再取得したい時に増やすトークン */
  reloadToken?: number;
};

function optionValues(field: DocsField): string[] {
  const options = field.options_json as unknown;
  if (Array.isArray(options)) return options.map((o) => String(o));
  if (options && typeof options === "object") {
    const values = (options as { values?: unknown }).values;
    if (Array.isArray(values)) return values.map((v) => String(v));
  }
  return [];
}

function currentValue(entry: FieldEntry): unknown {
  const raw = entry.value;
  if (!raw) return null;
  if (entry.field.field_type === "checkbox") {
    // サーバ格納形は value_json = { value: bool }。生の value_json（オブジェクト）を
    // 返すと Boolean(obj) が常に true になるため、value を取り出して boolean を返す。
    const vj = raw.value_json as { value?: unknown } | null | undefined;
    if (vj && typeof vj === "object" && "value" in vj) return Boolean(vj.value);
    return Boolean(raw.value_json);
  }
  if (raw.value_json !== undefined && raw.value_json !== null)
    return raw.value_json;
  if (raw.value_number !== undefined && raw.value_number !== null)
    return raw.value_number;
  if (raw.value_datetime) return raw.value_datetime;
  if (raw.target_node_id) return raw.target_node_id;
  if (raw.value_text !== undefined && raw.value_text !== null)
    return raw.value_text;
  return null;
}

function ReferencePicker({
  visible,
  onDismiss,
  onSelect,
}: {
  visible: boolean;
  onDismiss: () => void;
  onSelect: (node: DocsNode) => void;
}) {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<DocsNode[]>([]);

  useEffect(() => {
    if (!visible) {
      setQuery("");
      setResults([]);
    }
  }, [visible]);

  useEffect(() => {
    if (!visible) return;
    let active = true;
    const q = query.trim();
    if (!q) {
      setResults([]);
      return;
    }
    void docsRepo
      .searchLocal(q)
      .then((rows) => {
        if (active) setResults(rows.slice(0, 30));
      })
      .catch(() => {
        if (active) setResults([]);
      });
    return () => {
      active = false;
    };
  }, [query, visible]);

  return (
    <Portal>
      <Dialog visible={visible} onDismiss={onDismiss} style={styles.dialog}>
        <Dialog.Title style={styles.dialogTitle}>参照先を検索</Dialog.Title>
        <Dialog.Content>
          <TextInput
            value={query}
            onChangeText={setQuery}
            mode="outlined"
            dense
            placeholder="ノードタイトルで検索"
            autoCorrect={false}
          />
          <ScrollView style={styles.refResults}>
            {results.map((node) => (
              <List.Item
                key={node.id}
                title={node.title || "無題"}
                titleStyle={styles.refItemTitle}
                onPress={() => onSelect(node)}
              />
            ))}
          </ScrollView>
        </Dialog.Content>
        <Dialog.Actions>
          <Button onPress={onDismiss} textColor="#a6adc8">
            閉じる
          </Button>
        </Dialog.Actions>
      </Dialog>
    </Portal>
  );
}

function FieldRow({
  entry,
  onSave,
}: {
  entry: FieldEntry;
  onSave: (fieldId: string, value: unknown) => Promise<void>;
}) {
  const { field } = entry;
  const initial = currentValue(entry);
  const [textDraft, setTextDraft] = useState(
    initial == null ? "" : String(initial),
  );
  const [boolDraft, setBoolDraft] = useState(Boolean(initial));
  const [menuVisible, setMenuVisible] = useState(false);
  const [refVisible, setRefVisible] = useState(false);
  const [refLabel, setRefLabel] = useState<string | null>(
    field.field_type === "reference" && initial ? String(initial) : null,
  );

  const label = `${field.name}${field.required ? " *" : ""}`;

  const saveText = useCallback(
    (value: string) => {
      const trimmed = value.trim();
      void onSave(field.id, trimmed === "" ? null : trimmed);
    },
    [field.id, onSave],
  );

  switch (field.field_type) {
    case "checkbox":
      return (
        <View style={styles.switchRow}>
          <Text style={styles.fieldLabel}>{label}</Text>
          <Switch
            value={boolDraft}
            onValueChange={(next) => {
              setBoolDraft(next);
              void onSave(field.id, next);
            }}
          />
        </View>
      );
    case "number":
      return (
        <View style={styles.fieldBlock}>
          <Text style={styles.fieldLabel}>{label}</Text>
          <TextInput
            value={textDraft}
            onChangeText={setTextDraft}
            onBlur={() => {
              const trimmed = textDraft.trim();
              void onSave(field.id, trimmed === "" ? null : Number(trimmed));
            }}
            mode="outlined"
            dense
            keyboardType="decimal-pad"
          />
        </View>
      );
    case "long_text":
      return (
        <View style={styles.fieldBlock}>
          <Text style={styles.fieldLabel}>{label}</Text>
          <TextInput
            value={textDraft}
            onChangeText={setTextDraft}
            onBlur={() => saveText(textDraft)}
            mode="outlined"
            multiline
            numberOfLines={3}
          />
        </View>
      );
    case "options":
    case "options_from_supertag": {
      const values = optionValues(field);
      return (
        <View style={styles.fieldBlock}>
          <Text style={styles.fieldLabel}>{label}</Text>
          <Menu
            visible={menuVisible}
            onDismiss={() => setMenuVisible(false)}
            anchor={
              <Button
                mode="outlined"
                textColor="#cdd6f4"
                style={styles.optionButton}
                contentStyle={styles.optionButtonContent}
                onPress={() => setMenuVisible(true)}
              >
                {textDraft || "選択"}
              </Button>
            }
            contentStyle={styles.menuContent}
          >
            <Menu.Item
              title="（クリア）"
              onPress={() => {
                setTextDraft("");
                setMenuVisible(false);
                void onSave(field.id, null);
              }}
            />
            {values.map((value) => (
              <Menu.Item
                key={value}
                title={value}
                leadingIcon={value === textDraft ? "check" : undefined}
                onPress={() => {
                  setTextDraft(value);
                  setMenuVisible(false);
                  void onSave(field.id, value);
                }}
              />
            ))}
          </Menu>
        </View>
      );
    }
    case "reference":
      return (
        <View style={styles.fieldBlock}>
          <Text style={styles.fieldLabel}>{label}</Text>
          <Button
            mode="outlined"
            textColor="#cdd6f4"
            style={styles.optionButton}
            contentStyle={styles.optionButtonContent}
            onPress={() => setRefVisible(true)}
          >
            {refLabel || "参照先を選択"}
          </Button>
          <ReferencePicker
            visible={refVisible}
            onDismiss={() => setRefVisible(false)}
            onSelect={(node) => {
              setRefLabel(node.title || node.id);
              setRefVisible(false);
              void onSave(field.id, node.id);
            }}
          />
        </View>
      );
    case "date":
    case "url":
    case "email":
    case "user":
    case "text":
    default: {
      const keyboardType =
        field.field_type === "email"
          ? "email-address"
          : field.field_type === "url"
            ? "url"
            : "default";
      return (
        <View style={styles.fieldBlock}>
          <Text style={styles.fieldLabel}>{label}</Text>
          <TextInput
            value={textDraft}
            onChangeText={setTextDraft}
            onBlur={() => saveText(textDraft)}
            mode="outlined"
            dense
            keyboardType={keyboardType}
            autoCapitalize={
              field.field_type === "url" || field.field_type === "email"
                ? "none"
                : "sentences"
            }
            autoCorrect={false}
            placeholder={
              field.field_type === "date" ? "yyyy-MM-dd" : undefined
            }
          />
        </View>
      );
    }
  }
}

export function FieldEditor({ nodeId, reloadToken }: FieldEditorProps) {
  const [entries, setEntries] = useState<FieldEntry[]>([]);
  const [loading, setLoading] = useState(true);

  const reload = useCallback(async () => {
    setLoading(true);
    try {
      setEntries(await docsRepo.getNodeFieldValues(nodeId));
    } catch {
      setEntries([]);
    } finally {
      setLoading(false);
    }
  }, [nodeId]);

  useEffect(() => {
    void reload();
  }, [reload, reloadToken]);

  const handleSave = useCallback(
    async (fieldId: string, value: unknown) => {
      await docsRepo.setField(nodeId, fieldId, value);
      await reload();
    },
    [nodeId, reload],
  );

  if (loading) {
    return <ActivityIndicator color="#7c3aed" style={styles.loading} />;
  }

  if (entries.length === 0) {
    return <Text style={styles.empty}>フィールドはありません</Text>;
  }

  return (
    <View style={styles.container}>
      <Text style={styles.sectionLabel}>フィールド</Text>
      {entries.map((entry) => (
        <FieldRow key={entry.field.id} entry={entry} onSave={handleSave} />
      ))}
    </View>
  );
}

const styles = StyleSheet.create({
  container: { gap: 10 },
  sectionLabel: { color: "#a6adc8", fontSize: 13, fontWeight: "700" },
  fieldBlock: { gap: 4 },
  fieldLabel: { color: "#a6adc8", fontSize: 12 },
  switchRow: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
  },
  optionButton: { borderColor: "#45475a", alignSelf: "flex-start" },
  optionButtonContent: { height: 40 },
  menuContent: { backgroundColor: "#1e1e2e" },
  loading: { marginVertical: 16 },
  empty: { color: "#585b70", fontSize: 12 },
  dialog: { backgroundColor: "#1e1e2e" },
  dialogTitle: { color: "#cdd6f4" },
  refResults: { maxHeight: 260, marginTop: 8 },
  refItemTitle: { color: "#cdd6f4" },
});
