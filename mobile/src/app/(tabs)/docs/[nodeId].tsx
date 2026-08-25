import React, { useCallback, useEffect, useRef, useState } from "react";
import {
  AppState,
  Pressable,
  ScrollView,
  StyleSheet,
  View,
} from "react-native";
import {
  ActivityIndicator,
  Button,
  Dialog,
  IconButton,
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
import { ClipIngestDialog } from "../../../components/docs/clip-ingest-dialog";
import { DocsTaskBinding } from "../../../components/docs/task-binding";
import { DocBlockEditor } from "../../../components/docs/verbatim-blocks";
import { docsRepo } from "../../../repositories/docs";
import { useNetworkStore } from "../../../stores/network";
import { runSync } from "../../../sync/engine";
import type { DocsNode, DocsSupertag } from "../../../types/api";

export default function DocsNodeScreen() {
  const router = useRouter();
  const params = useLocalSearchParams<{ nodeId: string; created?: string }>();
  const nodeId = params.nodeId;
  const online = useNetworkStore((state) => state.online);
  const [node, setNode] = useState<DocsNode | null>(null);
  const [tags, setTags] = useState<DocsSupertag[]>([]);
  const [backlinks, setBacklinks] = useState<DocsNode[]>([]);
  const [outgoingReferences, setOutgoingReferences] = useState<DocsNode[]>([]);
  const [loading, setLoading] = useState(true);
  const [titleDraft, setTitleDraft] = useState("");
  const [titleEditing, setTitleEditing] = useState(false);
  const [descDraft, setDescDraft] = useState("");
  const [propertiesVisible, setPropertiesVisible] = useState(false);
  const [showArchived, setShowArchived] = useState(false);
  const [reloadToken, setReloadToken] = useState(0);
  const [moveTargetId, setMoveTargetId] = useState<string | null>(null);
  const [clipIngestVisible, setClipIngestVisible] = useState(false);
  const draftsInitialized = useRef(false);
  const initialFocusApplied = useRef(false);
  const titleInputRef = useRef<{ focus: () => void } | null>(null);
  const outlineRef = useRef<OutlineEditorHandle>(null);
  const activeNodeIdRef = useRef(nodeId);
  activeNodeIdRef.current = nodeId;
  const loadGeneration = useRef(0);
  const nodeLoadRequestRef = useRef(0);
  const focusedRef = useRef(false);
  const titleEditingRef = useRef(false);
  const propertiesVisibleRef = useRef(false);
  const titleDraftRef = useRef("");
  const descDraftRef = useRef("");
  const titleDirtyRef = useRef(false);
  const descDirtyRef = useRef(false);
  const titleSaveCountRef = useRef(0);
  const descSaveCountRef = useRef(0);
  const titleDraftRevisionRef = useRef(0);
  const descDraftRevisionRef = useRef(0);
  const revalidateFlightRef = useRef<Promise<void> | null>(null);

  const bump = useCallback(() => setReloadToken((value) => value + 1), []);
  const loadNode = useCallback(async (
    generation: number,
    refreshDrafts = false,
  ): Promise<DocsNode | null> => {
    const requestId = ++nodeLoadRequestRef.current;
    const isCurrent = () =>
      loadGeneration.current === generation &&
      nodeLoadRequestRef.current === requestId;
    const titleDraftRevision = titleDraftRevisionRef.current;
    const descDraftRevision = descDraftRevisionRef.current;
    const tagsRequest = docsRepo.getNodeTags(nodeId);
    const backlinksRequest = docsRepo.getBacklinks(nodeId);
    const outgoingReferencesRequest = docsRepo.getOutgoingReferences(nodeId);

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
    void outgoingReferencesRequest
      .then((links) => {
        if (isCurrent()) setOutgoingReferences(links);
      })
      .catch(() => {
        if (isCurrent()) setOutgoingReferences([]);
      });

    try {
      const loaded = await docsRepo.getNode(nodeId);
      if (!isCurrent()) return null;
      setNode(loaded);
      if (loaded) {
        if (
          !draftsInitialized.current ||
          (
            refreshDrafts &&
            !titleEditingRef.current &&
            !titleDirtyRef.current &&
            titleSaveCountRef.current === 0 &&
            titleDraftRevisionRef.current === titleDraftRevision
          )
        ) {
          const nextTitle = loaded.title ?? "";
          titleDraftRef.current = nextTitle;
          titleDirtyRef.current = false;
          setTitleDraft(nextTitle);
        }
        if (
          !draftsInitialized.current ||
          (
            refreshDrafts &&
            !propertiesVisibleRef.current &&
            !descDirtyRef.current &&
            descSaveCountRef.current === 0 &&
            descDraftRevisionRef.current === descDraftRevision
          )
        ) {
          const nextDescription = loaded.description ?? "";
          descDraftRef.current = nextDescription;
          descDirtyRef.current = false;
          setDescDraft(nextDescription);
        }
        draftsInitialized.current = true;
      }
      return loaded;
    } catch {
      if (isCurrent()) setNode(null);
      return null;
    } finally {
      if (isCurrent()) setLoading(false);
    }
  }, [nodeId]);

  const revalidateNode = useCallback(
    (generation: number): Promise<void> => {
      if (!online) return Promise.resolve();
      let syncFlight = revalidateFlightRef.current;
      if (!syncFlight) {
        const flight = runSync()
          .catch(() => {
            // ローカル表示は維持し、次のfocus/foregroundで再試行する。
          })
          .finally(() => {
            if (revalidateFlightRef.current === flight) {
              revalidateFlightRef.current = null;
            }
          });
        revalidateFlightRef.current = flight;
        syncFlight = flight;
      }

      return syncFlight.then(async () => {
        if (
          !focusedRef.current ||
          loadGeneration.current !== generation
        ) {
          return;
        }
        await loadNode(generation, true);
        if (
          focusedRef.current &&
          loadGeneration.current === generation
        ) {
          try {
            await outlineRef.current?.reloadFromRepository();
          } catch {
            // 保存できなかったdraftはOutlineEditor内に残し、次回の同期で再試行する。
          }
        }
      });
    },
    [loadNode, online],
  );

  useEffect(() => {
    loadGeneration.current += 1;
    setNode(null);
    setTags([]);
    setBacklinks([]);
    setOutgoingReferences([]);
    setLoading(true);
    draftsInitialized.current = false;
    initialFocusApplied.current = false;
    titleDraftRef.current = "";
    descDraftRef.current = "";
    titleDirtyRef.current = false;
    descDirtyRef.current = false;
    titleDraftRevisionRef.current += 1;
    descDraftRevisionRef.current += 1;
    setTitleDraft("");
    setDescDraft("");
    setTitleEditing(false);
  }, [nodeId]);
  useEffect(() => {
    titleEditingRef.current = titleEditing;
  }, [titleEditing]);
  useEffect(() => {
    propertiesVisibleRef.current = propertiesVisible;
  }, [propertiesVisible]);
  useFocusEffect(
    useCallback(() => {
      focusedRef.current = true;
      const generation = ++loadGeneration.current;
      void loadNode(generation);
      void revalidateNode(generation);
      return () => {
        focusedRef.current = false;
        if (loadGeneration.current === generation) loadGeneration.current += 1;
      };
    }, [loadNode, reloadToken, revalidateNode]),
  );
  useEffect(() => {
    const subscription = AppState.addEventListener("change", (state) => {
      if (state === "active" && focusedRef.current) {
        void revalidateNode(loadGeneration.current);
      }
    });
    return () => subscription.remove();
  }, [revalidateNode]);
  useEffect(() => {
    if (!node || params.created !== "1" || initialFocusApplied.current) return;
    initialFocusApplied.current = true;
    setTitleEditing(true);
    setTimeout(() => titleInputRef.current?.focus(), 120);
  }, [node, params.created]);

  const saveTitle = useCallback(async () => {
    if (!node) return;
    const next = titleDraftRef.current.replace(/[\r\n]+/g, " ").slice(0, 500);
    if (next !== titleDraftRef.current) {
      titleDraftRef.current = next;
      setTitleDraft(next);
    }
    if (next === (node.title ?? "")) {
      titleDirtyRef.current = false;
      titleDraftRevisionRef.current += 1;
      return;
    }
    titleSaveCountRef.current += 1;
    try {
      const updated = await docsRepo.updateNode(nodeId, { title: next });
      if (activeNodeIdRef.current === nodeId) {
        setNode(updated);
        if (titleDraftRef.current === next) {
          titleDirtyRef.current = false;
          titleDraftRevisionRef.current += 1;
        }
      }
    } finally {
      titleSaveCountRef.current -= 1;
    }
  }, [node, nodeId]);

  const submitTitle = useCallback(async () => {
    await saveTitle();
    setTitleEditing(false);
    await outlineRef.current?.focusFirstOrCreate();
  }, [saveTitle]);

  const saveDescription = useCallback(async () => {
    if (!node) return;
    const next = descDraftRef.current;
    if (next === (node.description ?? "")) {
      descDirtyRef.current = false;
      descDraftRevisionRef.current += 1;
      return;
    }
    descSaveCountRef.current += 1;
    try {
      const updated = await docsRepo.updateNode(nodeId, { description: next });
      if (activeNodeIdRef.current === nodeId) {
        setNode(updated);
        if (descDraftRef.current === next) {
          descDirtyRef.current = false;
          descDraftRevisionRef.current += 1;
        }
      }
    } finally {
      descSaveCountRef.current -= 1;
    }
  }, [node, nodeId]);

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
    <View style={styles.container}>
      <ScreenHeader
        title="Docs"
        subtitle={node?.archived_at ? "アーカイブ済み" : undefined}
        onBack={() => goBackOrReplace(router, "/(tabs)/docs")}
        right={
          <>
            <IconButton
              icon="tray-arrow-down"
              size={22}
              iconColor="#a6adc8"
              accessibilityLabel="クリップ取り込み"
              onPress={() => setClipIngestVisible(true)}
            />
            <Button
              compact
              icon="tune-variant"
              textColor="#cdd6f4"
              disabled={!node}
              onPress={() => setPropertiesVisible(true)}
            >
              プロパティ
            </Button>
          </>
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
                  onChangeText={(value) => {
                    const next = value.replace(/[\r\n]+/g, " ").slice(0, 500);
                    titleDraftRef.current = next;
                    titleDirtyRef.current = true;
                    titleDraftRevisionRef.current += 1;
                    setTitleDraft(next);
                  }}
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
            {node ? (
              <DocBlockEditor
                node={node}
                testIdPrefix="docs-page-block"
                onSaved={setNode}
              />
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
                onChangeText={(value) => {
                  descDraftRef.current = value;
                  descDirtyRef.current = true;
                  descDraftRevisionRef.current += 1;
                  setDescDraft(value);
                }}
                onBlur={() => void saveDescription()}
                mode="outlined"
                multiline
                numberOfLines={4}
                placeholder="概要（任意）"
                style={styles.descInput}
              />
              <DocsTaskBinding
                nodeId={nodeId}
                projectId={node?.project_id}
                readOnly={Boolean(node?.read_only || node?.access === "read")}
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
              {outgoingReferences.length > 0 ? (
                <>
                  <Text style={styles.sectionLabel}>参照先</Text>
                  {outgoingReferences.map((link) => (
                    <List.Item
                      key={link.id}
                      title={link.title || "無題"}
                      titleStyle={styles.backlinkTitle}
                      left={(props) => <List.Icon {...props} icon="link-variant-plus" color="#a6e3a1" />}
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
      <ClipIngestDialog
        visible={clipIngestVisible}
        onDismiss={() => setClipIngestVisible(false)}
        onOpenNode={(openNodeId) => {
          setClipIngestVisible(false);
          router.push(`/(tabs)/docs/${openNodeId}`);
        }}
      />
    </View>
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
