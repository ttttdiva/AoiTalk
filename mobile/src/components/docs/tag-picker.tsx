/**
 * スーパータグ付与エディタ
 *
 * ノードの現在タグをチップ表示し、× で除去する。追加は
 *  - 既存 supertag 一覧（検索フィルタ付き）から選択 → addTag({ supertagId })
 *  - 一致するものが無い名前を入力 → addTag({ name })（サーバ resolve_supertag(create=True)）
 * のどちらか。タグ定義=フィールド構成の編集はスコープ外。
 */

import React, { useEffect, useMemo, useState } from "react";
import { ScrollView, StyleSheet, View } from "react-native";
import { Button, Chip, Text, TextInput } from "react-native-paper";
import { docsRepo } from "../../repositories/docs";
import type { DocsSupertag } from "../../types/api";

type TagPickerProps = {
  nodeId: string;
  tags: DocsSupertag[];
  onChanged: () => void;
};

export function TagPicker({ nodeId, tags, onChanged }: TagPickerProps) {
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [allSupertags, setAllSupertags] = useState<DocsSupertag[]>([]);

  const reloadSupertags = async () => {
    setAllSupertags(await docsRepo.listSupertags());
  };

  useEffect(() => {
    void reloadSupertags();
  }, []);

  const assignedIds = useMemo(
    () => new Set(tags.map((t) => t.id)),
    [tags],
  );

  const query = draft.trim().toLowerCase();
  const candidates = useMemo(() => {
    return allSupertags
      .filter((s) => !assignedIds.has(s.id))
      .filter((s) =>
        query ? String(s.name ?? "").toLowerCase().includes(query) : true,
      )
      .slice(0, 30);
  }, [allSupertags, assignedIds, query]);

  const exactMatch = useMemo(
    () =>
      allSupertags.some(
        (s) => String(s.name ?? "").trim().toLowerCase() === query && query,
      ),
    [allSupertags, query],
  );

  const addExisting = async (supertagId: string) => {
    if (busy) return;
    setBusy(true);
    try {
      await docsRepo.addTag(nodeId, { supertagId });
      setDraft("");
      onChanged();
      await reloadSupertags();
    } finally {
      setBusy(false);
    }
  };

  const addByName = async () => {
    const name = draft.trim();
    if (!name || busy) return;
    setBusy(true);
    try {
      await docsRepo.addTag(nodeId, { name });
      setDraft("");
      onChanged();
      await reloadSupertags();
    } finally {
      setBusy(false);
    }
  };

  const removeTag = async (supertagId: string) => {
    if (busy) return;
    setBusy(true);
    try {
      await docsRepo.removeTag(nodeId, supertagId);
      onChanged();
    } finally {
      setBusy(false);
    }
  };

  return (
    <View style={styles.container}>
      <Text style={styles.sectionLabel}>スーパータグ</Text>
      <View style={styles.chipRow}>
        {tags.length === 0 ? (
          <Text style={styles.empty}>タグ未設定</Text>
        ) : (
          tags.map((tag) => (
            <Chip
              key={tag.id}
              compact
              onClose={() => void removeTag(tag.id)}
              style={[
                styles.chip,
                tag.color ? { borderColor: tag.color } : null,
              ]}
              textStyle={styles.chipText}
              icon={tag.icon || "pound"}
            >
              {tag.name}
            </Chip>
          ))
        )}
      </View>
      <View style={styles.addRow}>
        <TextInput
          value={draft}
          onChangeText={setDraft}
          mode="outlined"
          dense
          placeholder="タグを検索 / 新規名を入力"
          style={styles.input}
          autoCorrect={false}
          autoCapitalize="none"
          onSubmitEditing={() => void addByName()}
        />
        <Button
          mode="outlined"
          onPress={() => void addByName()}
          disabled={!draft.trim() || busy || exactMatch}
          loading={busy}
        >
          新規
        </Button>
      </View>
      {candidates.length > 0 ? (
        <ScrollView
          style={styles.candidateList}
          keyboardShouldPersistTaps="handled"
          nestedScrollEnabled
        >
          <View style={styles.candidateWrap}>
            {candidates.map((s) => (
              <Chip
                key={s.id}
                compact
                onPress={() => void addExisting(s.id)}
                style={styles.candidateChip}
                textStyle={styles.chipText}
                icon={s.icon || "pound"}
              >
                {s.name}
              </Chip>
            ))}
          </View>
        </ScrollView>
      ) : query && !exactMatch ? (
        <Text style={styles.empty}>
          「{draft.trim()}」は未登録。「新規」で作成できます。
        </Text>
      ) : null}
    </View>
  );
}

const styles = StyleSheet.create({
  container: { gap: 8 },
  sectionLabel: { color: "#a6adc8", fontSize: 13, fontWeight: "700" },
  chipRow: { flexDirection: "row", flexWrap: "wrap", gap: 6 },
  chip: { backgroundColor: "#313244" },
  chipText: { color: "#cdd6f4", fontSize: 12 },
  empty: { color: "#585b70", fontSize: 12 },
  addRow: { flexDirection: "row", gap: 8, alignItems: "center" },
  input: { flex: 1, backgroundColor: "transparent" },
  candidateList: { maxHeight: 140 },
  candidateWrap: { flexDirection: "row", flexWrap: "wrap", gap: 6 },
  candidateChip: { backgroundColor: "#1e1e2e", borderColor: "#45475a" },
});
