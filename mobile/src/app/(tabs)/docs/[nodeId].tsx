import React, { useCallback, useEffect, useRef, useState } from "react";
import {
  KeyboardAvoidingView,
  Platform,
  Pressable,
  ScrollView,
  StyleSheet,
  View,
} from "react-native";
import {
  ActivityIndicator,
  Button,
  Dialog,
  List,
  Portal,
  Surface,
  Text,
  TextInput,
} from "react-native-paper";
import { useFocusEffect, useLocalSearchParams, useRouter } from "expo-router";
import { goBackOrReplace } from "../../../lib/navigation";
import { ScreenHeader } from "../../../components/screen-header";
import {
  OutlineEditor,
  type OutlineEditorHandle,
} from "../../../components/docs/outline-editor";
import { TagPicker } from "../../../components/docs/tag-picker";
import { FieldEditor } from "../../../components/docs/field-editor";
import { MovePicker } from "../../../components/docs/move-picker";
import { docsRepo } from "../../../repositories/docs";
import type { DocsNode, DocsSupertag } from "../../../types/api";

export default function DocsNodeScreen() {
  const router = useRouter();
  const params = useLocalSearchParams<{ nodeId: string; created?: string }>();
  const nodeId = params.nodeId;
  const [node, setNode] = useState<DocsNode | null>(null);
  const [tags, setTags] = useState<DocsSupertag[]>([]);
  const [backlinks, setBacklinks] = useState<DocsNode[]>([]);
  const [loading, setLoading] = useState(true);
  const [titleDraft, setTitleDraft] = useState("");
  const [titleEditing, setTitleEditing] = useState(false);
  const [descDraft, setDescDraft] = useState("");
  const [propertiesVisible, setPropertiesVisible] = useState(false);
  const [showArchived, setShowArchived] = useState(false);
  const [reloadToken, setReloadToken] = useState(0);
  const [moveTargetId, setMoveTargetId] = useState<string | null>(null);
  const draftsInitialized = useRef(false);
  const initialFocusApplied = useRef(false);
  const titleInputRef = useRef<{ focus: () => void } | null>(null);
  const outlineRef = useRef<OutlineEditorHandle>(null);
  const loadGeneration = useRef(0);

  const bump = useCallback(() => setReloadToken((value) => value + 1), []);
  const loadNode = useCallback(async (generation: number) => {
    const isCurrent = () => loadGeneration.current === generation;
    const tagsRequest = docsRepo.getNodeTags(nodeId);
    const backlinksRequest = docsRepo.getBacklinks(nodeId);

    void tagsRequest
      .then((loadedTags) => {
        if (isCurrent()) setTags(loadedTags);
      })
      .catch(() => {
        if (isCurrent()) setTags([]);
      });
    void backlinksRequest
      .then((links) => {
        if (isCurrent()) setBacklinks(links);
      })
      .catch(() => {
        if (isCurrent()) setBacklinks([]);
      });

    try {
      const loaded = await docsRepo.getNode(nodeId);
      if (!isCurrent()) return;
      setNode(loaded);
      if (loaded && !draftsInitialized.current) {
        setTitleDraft(loaded.title ?? "");
        setDescDraft(loaded.description ?? "");
        draftsInitialized.current = true;
      }
    } catch {
      if (isCurrent()) setNode(null);
    } finally {
      if (isCurrent()) setLoading(false);
    }
  }, [nodeId]);

  useEffect(() => {
    loadGeneration.current += 1;
    setNode(null);
    setTags([]);
    setBacklinks([]);
    setLoading(true);
    draftsInitialized.current = false;
    initialFocusApplied.current = false;
    setTitleEditing(false);
  }, [nodeId]);
  useFocusEffect(
    useCallback(() => {
      const generation = ++loadGeneration.current;
      void loadNode(generation);
      return () => {
        if (loadGeneration.current === generation) loadGeneration.current += 1;
      };
    }, [loadNode, reloadToken]),
  );
  useEffect(() => {
    if (!node || params.created !== "1" || initialFocusApplied.current) return;
    initialFocusApplied.current = true;
    setTitleEditing(true);
    setTimeout(() => titleInputRef.current?.focus(), 120);
  }, [node, params.created]);

  const saveTitle = useCallback(async () => {
    if (!node) return;
    const next = titleDraft.replace(/[\r\n]+/g, " ").slice(0, 500);
    if (next !== titleDraft) setTitleDraft(next);
    if (next === (node.title ?? "")) return;
    const updated = await docsRepo.updateNode(nodeId, { title: next });
    setNode(updated);
  }, [node, nodeId, titleDraft]);

  const submitTitle = useCallback(async () => {
    await saveTitle();
    setTitleEditing(false);
    await outlineRef.current?.focusFirstOrCreate();
  }, [saveTitle]);

  const saveDescription = useCallback(async () => {
    if (!node || descDraft === (node.description ?? "")) return;
    const updated = await docsRepo.updateNode(nodeId, { description: descDraft });
    setNode(updated);
  }, [descDraft, node, nodeId]);

  const archiveSelf = useCallback(async () => {
    await docsRepo.archiveNode(nodeId);
    goBackOrReplace(router, "/(tabs)/docs");
  }, [nodeId, router]);

  const handleMoveConfirm = useCallback(
    async (targetId: string, leaveReference: boolean) => {
      const source = moveTargetId;
      setMoveTargetId(null);
      if (!source) return;
      await docsRepo.moveNode(source, targetId, undefined, leaveReference);
      bump();
    },
    [bump, moveTargetId],
  );

  if (!loading && !node) {
    return (
      <View style={styles.container}>
        <ScreenHeader title="Docs" onBack={() => goBackOrReplace(router, "/(tabs)/docs")} />
        <View style={styles.center}><Text style={styles.emptyText}>ノードが見つかりません</Text></View>
      </View>
    );
  }

  return (
    <KeyboardAvoidingView
      style={styles.container}
      behavior={Platform.OS === "ios" ? "padding" : undefined}
      keyboardVerticalOffset={Platform.OS === "ios" ? 88 : 0}
    >
      <ScreenHeader
        title="Docs"
        subtitle={node?.archived_at ? "アーカイブ済み" : undefined}
        onBack={() => goBackOrReplace(router, "/(tabs)/docs")}
        right={
          <Button
            compact
            icon="tune-variant"
            textColor="#cdd6f4"
            disabled={!node}
            onPress={() => setPropertiesVisible(true)}
          >
            プロパティ
          </Button>
        }
      />
      <OutlineEditor
        ref={outlineRef}
        rootNodeId={nodeId}
        showArchived={showArchived}
        reloadToken={reloadToken}
        onOpen={(childId) => router.push(`/(tabs)/docs/${childId}`)}
        onMoveRequest={setMoveTargetId}
        onChanged={bump}
        listHeader={
          <Surface style={styles.editorHeader} elevation={0}>
            {loading ? (
              <View style={styles.titleLoading}>
                <ActivityIndicator size="small" color="#7c3aed" />
              </View>
            ) : (
              titleEditing ? (
                <TextInput
                  ref={(input: { focus: () => void } | null) => {
                    titleInputRef.current = input;
                  }}
                  value={titleDraft}
                  onChangeText={(value) =>
                    setTitleDraft(value.replace(/[\r\n]+/g, " ").slice(0, 500))
                  }
                  onBlur={() => {
                    void saveTitle();
                    setTitleEditing(false);
                  }}
                  onSubmitEditing={() => void submitTitle()}
                  submitBehavior="submit"
                  multiline
                  scrollEnabled={false}
                  rejectResponderTermination={false}
                  mode="flat"
                  placeholder="タイトル"
                  maxLength={500}
                  style={styles.titleInput}
                  underlineColor="transparent"
                  activeUnderlineColor="transparent"
                />
              ) : (
                <Pressable
                  accessibilityRole="button"
                  accessibilityLabel="ページタイトルを編集"
                  style={styles.titleDisplay}
                  onPress={() => {
                    setTitleEditing(true);
                    requestAnimationFrame(() => titleInputRef.current?.focus());
                  }}
                >
                  <Text
                    style={
                      titleDraft ? styles.titleDisplayText : styles.titlePlaceholder
                    }
                  >
                    {titleDraft || "タイトル"}
                  </Text>
                </Pressable>
              )
            )}
            {tags.length > 0 ? (
              <Text style={styles.tagSummary} numberOfLines={1}>
                {tags.map((tag) => `#${tag.name}`).join("  ")}
              </Text>
            ) : null}
          </Surface>
        }
      />

      <Portal>
        <Dialog visible={propertiesVisible} onDismiss={() => setPropertiesVisible(false)} style={styles.dialog}>
          <Dialog.Title style={styles.dialogTitle}>プロパティ</Dialog.Title>
          <Dialog.ScrollArea style={styles.dialogScrollArea}>
            <ScrollView contentContainerStyle={styles.propertiesContent} keyboardShouldPersistTaps="handled">
              <Text style={styles.sectionLabel}>タグ</Text>
              <TagPicker nodeId={nodeId} tags={tags} onChanged={bump} />
              <Text style={styles.sectionLabel}>フィールド</Text>
              <FieldEditor nodeId={nodeId} reloadToken={reloadToken} />
              <Text style={styles.sectionLabel}>概要</Text>
              <TextInput
                value={descDraft}
                onChangeText={setDescDraft}
                onBlur={() => void saveDescription()}
                mode="outlined"
                multiline
                numberOfLines={4}
                placeholder="概要（任意）"
                style={styles.descInput}
              />
              {backlinks.length > 0 ? (
                <>
                  <Text style={styles.sectionLabel}>バックリンク</Text>
                  {backlinks.map((link) => (
                    <List.Item
                      key={link.id}
                      title={link.title || "無題"}
                      titleStyle={styles.backlinkTitle}
                      left={(props) => <List.Icon {...props} icon="link-variant" color="#89b4fa" />}
                      onPress={() => {
                        setPropertiesVisible(false);
                        router.push(`/(tabs)/docs/${link.id}`);
                      }}
                    />
                  ))}
                </>
              ) : null}
              <Button
                mode="outlined"
                icon={showArchived ? "eye-off-outline" : "eye-outline"}
                onPress={() => setShowArchived((value) => !value)}
              >
                {showArchived ? "アーカイブ済みを隠す" : "アーカイブ済みを表示"}
              </Button>
              <Button
                mode="outlined"
                icon="folder-move-outline"
                onPress={() => {
                  setPropertiesVisible(false);
                  setMoveTargetId(nodeId);
                }}
              >
                このページを移動
              </Button>
              <Button mode="outlined" icon="archive-outline" textColor="#f38ba8" onPress={() => void archiveSelf()}>
                このページをアーカイブ
              </Button>
            </ScrollView>
          </Dialog.ScrollArea>
          <Dialog.Actions>
            <Button textColor="#a6adc8" onPress={() => setPropertiesVisible(false)}>閉じる</Button>
          </Dialog.Actions>
        </Dialog>
      </Portal>

      <MovePicker
        visible={moveTargetId !== null}
        currentNodeId={moveTargetId ?? nodeId}
        onDismiss={() => setMoveTargetId(null)}
        onConfirm={(targetId, leaveReference) => void handleMoveConfirm(targetId, leaveReference)}
      />
    </KeyboardAvoidingView>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: "#11111b" },
  center: { flex: 1, alignItems: "center", justifyContent: "center" },
  emptyText: { color: "#a6adc8", fontSize: 16 },
  editorHeader: { backgroundColor: "#11111b", paddingHorizontal: 2, paddingTop: 4 },
  titleLoading: { height: 58, alignItems: "flex-start", justifyContent: "center", paddingLeft: 14 },
  titleInput: { backgroundColor: "transparent", fontSize: 21, fontWeight: "700" },
  titleDisplay: {
    minHeight: 58,
    justifyContent: "center",
    paddingHorizontal: 12,
    paddingVertical: 8,
  },
  titleDisplayText: { color: "#cdd6f4", fontSize: 21, fontWeight: "700" },
  titlePlaceholder: { color: "#6c7086", fontSize: 21, fontWeight: "700" },
  tagSummary: { color: "#a6adc8", fontSize: 12, paddingHorizontal: 12, paddingBottom: 5 },
  dialog: { backgroundColor: "#1e1e2e", maxHeight: "88%" },
  dialogTitle: { color: "#cdd6f4" },
  dialogScrollArea: { maxHeight: 620, borderColor: "#313244" },
  propertiesContent: { paddingVertical: 8, gap: 12 },
  sectionLabel: { color: "#c084fc", fontSize: 12, fontWeight: "700", marginTop: 4 },
  descInput: { backgroundColor: "#181825" },
  backlinkTitle: { color: "#cdd6f4", fontSize: 14 },
});
