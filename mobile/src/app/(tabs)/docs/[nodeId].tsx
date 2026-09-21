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
import {
  adoptDocsNodeServerConflict,
  docsRepo,
} from "../../../repositories/docs";
import { docsApi } from "../../../lib/docs-api";
import { useNetworkStore } from "../../../stores/network";
import { runSync } from "../../../sync/engine";
import {
  getOutboxConflict,
  rebaseOutboxConflict,
  type OutboxConflictReference,
} from "../../../repositories/outbox";
import type { DocsNode, DocsSupertag } from "../../../types/api";

type ConflictField = "title" | "description" | "body_text" | "body_json";

function parseConflictPayload(value: unknown): Record<string, unknown> {
  if (value && typeof value === "object" && !Array.isArray(value)) {
    return value as Record<string, unknown>;
  }
  if (typeof value !== "string" || !value.trim()) return {};
  try {
    const parsed = JSON.parse(value);
    return parsed && typeof parsed === "object" && !Array.isArray(parsed)
      ? parsed as Record<string, unknown>
      : {};
  } catch {
    return {};
  }
}

function formatConflictValue(value: unknown): string {
  if (value == null || value === "") return "（空）";
  if (typeof value === "string") return value;
  try {
    const text = JSON.stringify(value);
    return text.length > 600 ? `${text.slice(0, 600)}…` : text;
  } catch {
    return String(value);
  }
}

function docsScopeKey(node: DocsNode | null): string | null {
  if (!node?.workspace_id) return null;
  return `${node.workspace_id}|project:${node.project_id ?? ""}`;
}

function conflictReasonMessage(reason?: string): string {
  switch (reason) {
    case "auth_scope_changed":
      return "アカウントが切り替わったため、競合の解決を中止しました。再読み込みしてください。";
    case "docs_scope_missing":
    case "docs_scope_mismatch":
    case "docs_scope_not_writable":
    case "node_not_writable":
      return "Docsの権限が変わったため、競合を解決できません。最新の権限を確認してください。";
    case "server_scope_missing":
    case "server_scope_mismatch":
    case "server_id_mismatch":
      return "サーバー版のDocsスコープを確認できないため、競合を解決できません。再同期してください。";
    case "missing_server_snapshot":
    case "server_version_missing":
      return "サーバー版が取得できないため、競合を解決できません。再同期してください。";
    case "outbox_replaced":
      return "端末で新しい編集が発生したため、古い競合操作は適用しませんでした。画面を更新してください。";
    default:
      return "競合を解決できませんでした。最新状態を取得して再試行してください。";
  }
}

function conflictFieldValue(
  field: ConflictField,
  localPayload: Record<string, unknown>,
  localNode: DocsNode | null,
  serverPayload: Record<string, unknown>,
): { local: unknown; server: unknown } {
  const localFallback: Record<ConflictField, unknown> = {
    title: localNode?.title ?? "",
    description: localNode?.description ?? "",
    body_text: localNode?.body_text ?? "",
    body_json: localNode?.body_json ?? null,
  };
  const serverKey = field;
  const localKey = field;
  return {
    local: localPayload[localKey] ?? localFallback[field],
    server: serverPayload[serverKey],
  };
}

export default function DocsNodeScreen() {
  const router = useRouter();
  const params = useLocalSearchParams<{ nodeId: string; created?: string }>();
  const nodeId = params.nodeId;
  // Docs APIs use the AoiTalk server path; Internet reachability is unrelated
  // when the server is hosted on the same LAN.
  const online = useNetworkStore((state) => state.connected ?? state.online);
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
  const [conflict, setConflict] = useState<OutboxConflictReference | null>(null);
  const [conflictBusy, setConflictBusy] = useState(false);
  const [conflictMessage, setConflictMessage] = useState<string | null>(null);
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

  const protectedProjectNode = Boolean(
    node
    && (() => {
      const systemKey = typeof node.system_key === "string" ? node.system_key.trim() : "";
      return systemKey === "project_information_root"
        || systemKey.startsWith("project_information:");
    })(),
  );
  const conflictLocalPayload = conflict
    ? parseConflictPayload(conflict.payload)
    : {};
  const conflictServerPayload = conflict
    ? parseConflictPayload(conflict.conflictPayload)
    : {};
  const conflictFields: ConflictField[] = [
    "title",
    "description",
    "body_text",
    "body_json",
  ];
  const conflictWritable = Boolean(
    conflict
    && node
    && !protectedProjectNode
    && !node.read_only
    && node.access !== "read"
    && conflict.docsScopeKey,
  );

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
      // A remote Clip/search/today result may be navigated to before the
      // staged Docs pull has promoted its row.  Use the ACL-checked canonical
      // node endpoint as a read-through fallback instead of showing a false
      // "not found" screen; the API adapter applies the snapshot itself.
      let loaded = await docsRepo.getNode(nodeId);
      if (!loaded && online) {
        try {
          loaded = (await docsApi.getNode(nodeId)).node;
        } catch {
          // Keep the existing null/empty state when the canonical read is
          // unavailable or the node is not visible to this account.
        }
      }
      if (!isCurrent()) {
        return null;
      }
      setNode(loaded);
      if (!loaded) {
        setConflict(null);
        setConflictMessage(null);
      } else if (typeof getOutboxConflict === "function") {
        // Conflicts are read through the account/scope-bound outbox helper;
        // never infer one from a sibling project that happens to share UUIDs.
        try {
          const loadedConflict = await getOutboxConflict(
            "knowledge_nodes",
            nodeId,
            { docsScopeKey: docsScopeKey(loaded) },
          );
          if (isCurrent()) {
            setConflict(loadedConflict);
            if (loadedConflict) setConflictMessage(null);
          }
        } catch {
          if (isCurrent()) setConflict(null);
        }
      }
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
  }, [nodeId, online]);

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
    setConflict(null);
    setConflictMessage(null);
    setConflictBusy(false);
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
    if (protectedProjectNode) {
      // Project metadata is the title authority.  The mobile page does not
      // own a project-name cache, so restore the last server-provided title
      // and never enqueue a generic canonical-root rename/clear.
      titleDraftRef.current = node.title ?? "";
      titleDirtyRef.current = false;
      setTitleDraft(titleDraftRef.current);
      return;
    }
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
  }, [node, nodeId, protectedProjectNode]);

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

  const resolveServerConflict = useCallback(async () => {
    const reference = conflict;
    if (!reference || !node || protectedProjectNode) return;
    if (node.read_only || node.access === "read" || !reference.docsScopeKey) {
      setConflictMessage(conflictReasonMessage("node_not_writable"));
      return;
    }
    setConflictBusy(true);
    setConflictMessage(null);
    try {
      if (typeof adoptDocsNodeServerConflict !== "function") {
        throw new Error("競合解決機能を利用できません");
      }
      const result = await adoptDocsNodeServerConflict(reference);
      if (!result.ok) {
        setConflictMessage(conflictReasonMessage(result.reason));
        return;
      }
      setConflict(null);
      setConflictMessage(null);
      const generation = loadGeneration.current;
      await loadNode(generation, true);
      await outlineRef.current?.reloadFromRepository();
    } catch (error) {
      setConflictMessage(
        error instanceof Error && error.message
          ? error.message
          : conflictReasonMessage(),
      );
    } finally {
      setConflictBusy(false);
    }
  }, [conflict, loadNode, node, nodeId, protectedProjectNode]);

  const reapplyDeviceConflict = useCallback(async () => {
    const reference = conflict;
    if (!reference || !node || protectedProjectNode) return;
    if (node.read_only || node.access === "read" || !reference.docsScopeKey) {
      setConflictMessage(conflictReasonMessage("node_not_writable"));
      return;
    }
    setConflictBusy(true);
    setConflictMessage(null);
    try {
      if (typeof rebaseOutboxConflict !== "function") {
        throw new Error("競合解決機能を利用できません");
      }
      const result = await rebaseOutboxConflict(reference);
      if (!result.ok) {
        setConflictMessage(conflictReasonMessage(result.reason));
        return;
      }
      // The local node/payload remains untouched.  Only the outbox base and
      // conflict marker changed, so the next sync can safely push the device
      // edit against the accepted server version.
      setConflict(null);
      setConflictMessage(null);
      const generation = loadGeneration.current;
      await loadNode(generation, true);
    } catch (error) {
      setConflictMessage(
        error instanceof Error && error.message
          ? error.message
          : conflictReasonMessage(),
      );
    } finally {
      setConflictBusy(false);
    }
  }, [conflict, loadNode, node, protectedProjectNode]);

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
              titleEditing && !protectedProjectNode ? (
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
                    if (protectedProjectNode) return;
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
            {conflict ? (
              <Surface
                testID="docs-sync-conflict"
                style={styles.conflictCard}
                elevation={0}
              >
                <Text style={styles.conflictTitle}>同期競合</Text>
                <Text style={styles.conflictDescription}>
                  端末編集とサーバー編集が同じ項目で競合しています。どちらを残すか選択してください。
                </Text>
                {conflictFields.map((field) => {
                  const values = conflictFieldValue(
                    field,
                    conflictLocalPayload,
                    node,
                    conflictServerPayload,
                  );
                  if (
                    values.server === undefined
                    && values.local === undefined
                  ) return null;
                  const labels: Record<ConflictField, string> = {
                    title: "タイトル",
                    description: "概要",
                    body_text: "本文",
                    body_json: "本文データ",
                  };
                  return (
                    <View key={field} style={styles.conflictValueRow}>
                      <Text style={styles.conflictFieldLabel}>{labels[field]}</Text>
                      <Text style={styles.conflictValue}>
                        端末: {formatConflictValue(values.local)}
                      </Text>
                      <Text style={styles.conflictValue}>
                        サーバー: {formatConflictValue(values.server)}
                      </Text>
                    </View>
                  );
                })}
                <View style={styles.conflictActions}>
                  <Button
                    compact
                    mode="outlined"
                    textColor="#89b4fa"
                    disabled={!conflictWritable || conflictBusy}
                    loading={conflictBusy}
                    onPress={() => void resolveServerConflict()}
                  >
                    サーバー版を採用
                  </Button>
                  <Button
                    compact
                    mode="contained"
                    buttonColor="#7c3aed"
                    disabled={!conflictWritable || conflictBusy}
                    loading={conflictBusy}
                    onPress={() => void reapplyDeviceConflict()}
                  >
                    端末編集を再適用
                  </Button>
                </View>
                {!conflictWritable ? (
                  <Text style={styles.conflictWarning}>
                    権限またはDocsスコープが変わったため、解決操作を無効にしています。
                  </Text>
                ) : null}
                {conflictMessage ? (
                  <Text style={styles.conflictWarning}>{conflictMessage}</Text>
                ) : null}
              </Surface>
            ) : null}
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
              <TagPicker nodeId={nodeId} tags={tags} onChanged={bump} readOnly={protectedProjectNode} />
              <Text style={styles.sectionLabel}>フィールド</Text>
              {protectedProjectNode ? (
                <Text style={styles.readOnlyNote}>Project正本のフィールドは専用画面で管理されます。</Text>
              ) : (
                <FieldEditor nodeId={nodeId} reloadToken={reloadToken} />
              )}
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
              {!protectedProjectNode ? (
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
              ) : null}
              {!protectedProjectNode ? <Button mode="outlined" icon="archive-outline" textColor="#f38ba8" onPress={() => void archiveSelf()}>
                このページをアーカイブ
              </Button> : null}
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
        targetNodeId={nodeId}
        projectId={node?.project_id ?? null}
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
  conflictCard: {
    backgroundColor: "#2a1f35",
    borderColor: "#c084fc",
    borderWidth: 1,
    borderRadius: 8,
    marginHorizontal: 10,
    marginBottom: 10,
    padding: 10,
    gap: 6,
  },
  conflictTitle: { color: "#f9e2af", fontSize: 16, fontWeight: "700" },
  conflictDescription: { color: "#cdd6f4", fontSize: 12, lineHeight: 18 },
  conflictValueRow: {
    borderTopColor: "#45405a",
    borderTopWidth: StyleSheet.hairlineWidth,
    paddingTop: 5,
    gap: 2,
  },
  conflictFieldLabel: { color: "#c084fc", fontSize: 12, fontWeight: "700" },
  conflictValue: { color: "#a6adc8", fontSize: 12, lineHeight: 17 },
  conflictActions: { flexDirection: "row", flexWrap: "wrap", gap: 8, marginTop: 4 },
  conflictWarning: { color: "#f38ba8", fontSize: 12, lineHeight: 17 },
  dialog: { backgroundColor: "#1e1e2e", maxHeight: "88%" },
  dialogTitle: { color: "#cdd6f4" },
  dialogScrollArea: { maxHeight: 620, borderColor: "#313244" },
  propertiesContent: { paddingVertical: 8, gap: 12 },
  sectionLabel: { color: "#c084fc", fontSize: 12, fontWeight: "700", marginTop: 4 },
  readOnlyNote: { color: "#a6adc8", fontSize: 12, lineHeight: 18 },
  descInput: { backgroundColor: "#181825" },
  backlinkTitle: { color: "#cdd6f4", fontSize: 14 },
});
