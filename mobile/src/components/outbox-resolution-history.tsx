import React, {
  useEffect,
  useRef,
  useState,
} from "react";
import { StyleSheet, TextInput, View } from "react-native";
import { Button, Text } from "react-native-paper";
import {
  listOutboxResolutionHistory,
  readOutboxResolutionRecord,
  restoreOutboxResolutionDraft,
  saveOutboxResolutionDraft,
} from "../repositories/outbox-resolution";
import type {
  ResolutionHistoryItem,
  ResolutionJsonObject,
  ResolutionRecordView,
} from "../repositories/outbox-resolution-types";
import { exportOutboxResolutionRecord } from "../lib/outbox-resolution-export";
import { ResolutionPreviewDetails } from "./outbox-diagnostics";

const STATE_LABELS: Record<string, string> = {
  prepared: "原値を保存済み",
  adopted: "サーバー版を採用済み",
  draft: "下書きあり",
  held: "保持中",
};

const REASON_LABELS: Record<string, string> = {
  invalid_input: "操作の情報を確認できません。元の操作を保持しています。",
  origin_confirmation_required: "現在の接続先の確認が必要です。元の操作を保持しています。",
  journal_unavailable: "原値を安全に保存できません。元の操作を保持しています。",
  record_not_found: "保存した記録が見つかりません。元の操作を保持しています。",
  revision_mismatch: "記録が更新されています。もう一度内容を確認してください。",
  context_changed: "認証または接続先が変わりました。内容を表示しません。",
  auth_scope_changed: "認証が変わりました。内容を表示しません。",
  server_changed: "サーバーの状態が変わりました。原値を保持しています。",
  server_unavailable: "サーバーの現在値を確認できません。原値を保持しています。",
  server_not_found: "サーバーに対象がありません。原値を保持しています。",
  server_forbidden: "サーバーの参照権限を確認できません。原値を保持しています。",
  local_snapshot_changed: "端末の操作が更新されています。原値を保持しています。",
  related_snapshot_changed: "関連する端末データが更新されています。原値を保持しています。",
  membership_changed: "保存先の範囲が更新されています。原値を保持しています。",
  cache_snapshot_changed: "端末の保存状態が更新されています。原値を保持しています。",
  editor_changed: "対象の編集画面で保存を完了し、戻る操作で閉じてから確認してください。未保存内容は保持しています。",
  sync_inflight: "同期処理が進行中です。完了後に確認してください。",
  duplicate_entity: "同じ対象を特定できません。原値を保持しています。",
  acl_changed: "保存先の権限が更新されています。原値を保持しています。",
  scope_changed: "保存先の範囲が変わりました。内容を表示しません。",
  unsupported: "この操作は安全な比較の対象外です。原値を保持しています。",
  not_prepareable: "原値を安全に保存できません。元の操作を保持しています。",
  not_adoptable: "現在のサーバー版を採用できません。原値を保持しています。",
  outbox_replaced: "送信待ちの操作が更新されています。原値を保持しています。",
  draft_missing: "未送信下書きが見つかりません。原値を保持しています。",
  transaction_failed: "端末の保存を完了できません。原値を保持しています。",
  export_unavailable: "ファイル保存を完了できません。原値を保持しています。",
  export_completion_unknown: "保存先にファイルが残っている可能性があります。保存先で確認してください。",
};

function reasonLabel(reason: unknown): string {
  return typeof reason === "string" && REASON_LABELS[reason]
    ? REASON_LABELS[reason]
    : "安全のため、この操作は実行できません。原値を保持しています。";
}

function isSuccess<T>(result: unknown): result is { ok: true; value: T } {
  return Boolean(
    result
      && typeof result === "object"
      && (result as { ok?: unknown }).ok === true,
  );
}

function resultReason(result: unknown): unknown {
  return result && typeof result === "object"
    ? (result as { reason?: unknown }).reason
    : "transaction_failed";
}

function safeHistory(value: unknown): ResolutionHistoryItem[] {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is ResolutionHistoryItem => {
    if (!item || typeof item !== "object") return false;
    const candidate = item as Partial<ResolutionHistoryItem>;
    return typeof candidate.id === "string"
      && Boolean(candidate.id.trim())
      && typeof candidate.revision === "number"
      && Number.isFinite(candidate.revision)
      && typeof candidate.kind === "string"
      && (candidate.kind === "task" || candidate.kind === "docs" || candidate.kind === "unsupported")
      && typeof candidate.entityId === "string"
      && typeof candidate.opId === "string"
      && typeof candidate.createdAt === "number"
      && Number.isFinite(candidate.createdAt)
      && typeof candidate.state === "string"
      && Object.prototype.hasOwnProperty.call(STATE_LABELS, candidate.state)
      && typeof candidate.hasDraft === "boolean";
  }).map((item) => ({
    id: item.id,
    revision: item.revision,
    kind: item.kind,
    entityId: item.entityId,
    opId: item.opId,
    createdAt: item.createdAt,
    state: item.state,
    hasDraft: item.hasDraft,
  }));
}

type DraftValues = {
  title: string;
  description: string;
  content: string;
  contentPath: string[] | null;
  searchMirror: string | null;
  dirty: {
    title: boolean;
    description: boolean;
    content: boolean;
  };
};

function valueToText(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function draftValues(record: ResolutionRecordView | null): DraftValues | null {
  if (!record?.draft) return null;
  const changes = record.draft.changes;
  const bodyKey = ["body_json", "bodyJson"]
    .find((key) => {
      const value = changes[key];
      return Boolean(value && typeof value === "object" && !Array.isArray(value)
        && Object.prototype.hasOwnProperty.call(value, "content"));
    });
  const contentKey = ["content", "typed", "typed_content", "typedContent", "body", "text"]
    .find((key) => Object.prototype.hasOwnProperty.call(changes, key));
  const contentPath = bodyKey ? [bodyKey, "content"] : contentKey ? [contentKey] : null;
  const bodyValue = bodyKey ? changes[bodyKey] : null;
  const bodyContent = bodyValue && typeof bodyValue === "object" && !Array.isArray(bodyValue)
    ? (bodyValue as Record<string, unknown>).content
    : null;
  const searchMirrorValue = changes.body_text ?? changes.bodyText;
  return {
    title: valueToText(changes.title),
    description: valueToText(changes.description),
    content: valueToText(bodyKey ? bodyContent : changes[contentKey ?? "content"]),
    contentPath,
    searchMirror: typeof searchMirrorValue === "string" ? searchMirrorValue : null,
    dirty: { title: false, description: false, content: false },
  };
}

function fallbackFor(record: ResolutionRecordView) {
  return {
    tableName: record.view.tableName,
    action: record.view.action,
    docsScopeKey: record.view.docsScopeKey,
    baseUpdatedAt: record.view.baseUpdatedAt,
    hasBaseSnapshot: record.view.hasBaseSnapshot,
    hasConflictSnapshot: record.view.hasConflictSnapshot,
  };
}

export type OutboxResolutionHistoryProps = {
  authScope: string;
  namespaceKey?: string;
  /** Settings passes a monotonically increasing token after its explicit entry press. */
  loadRequest?: number;
  /** Settings keeps the private editor mounted while blurred and hides it. */
  active?: boolean;
};

/** Explicit history entry; mounting this component never reads the journal. */
export function OutboxResolutionHistory({ authScope, namespaceKey, loadRequest, active = true }: OutboxResolutionHistoryProps) {
  const scope = authScope.trim();
  const identity = `${scope}|${namespaceKey?.trim() || scope}`;
  const isActive = active !== false;
  const currentIdentity = useRef(identity);
  currentIdentity.current = identity;
  const activeRef = useRef(isActive);
  activeRef.current = isActive;
  // Private history/draft values survive unrelated editor revision changes;
  // scope changes still replace the identity and clear them below.
  const dataKey = identity;
  const generation = useRef(0);
  const mountedRef = useRef(true);
  const busyRef = useRef(false);
  const [busy, setBusy] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [loadedKey, setLoadedKey] = useState<string | null>(null);
  const [items, setItems] = useState<ResolutionHistoryItem[]>([]);
  const [record, setRecord] = useState<ResolutionRecordView | null>(null);
  const [recordKey, setRecordKey] = useState<string | null>(null);
  const [draft, setDraft] = useState<DraftValues | null>(null);
  const draftDirtyRef = useRef(false);
  const [pendingAction, setPendingAction] = useState<
    | { kind: "list" }
    | { kind: "read"; item: ResolutionHistoryItem }
    | { kind: "restore" }
    | { kind: "export" }
    | null
  >(null);
  const [sessionVisible, setSessionVisible] = useState(false);
  const [status, setStatus] = useState<"saved" | "exported" | "shared" | "cancelled" | null>(null);
  const [errorReason, setErrorReason] = useState<unknown>(null);

  useEffect(() => {
    mountedRef.current = true;
    generation.current += 1;
    busyRef.current = false;
    setBusy(false);
    setLoaded(false);
    setLoadedKey(null);
    setItems([]);
    setRecord(null);
    setRecordKey(null);
    setDraft(null);
    draftDirtyRef.current = false;
    setPendingAction(null);
    setSessionVisible(false);
    setStatus(null);
    setErrorReason(null);
    return () => {
      mountedRef.current = false;
      generation.current += 1;
    };
  }, [identity]);

  useEffect(() => {
    generation.current += 1;
    busyRef.current = false;
    setBusy(false);
    // A blur hides private values but keeps dirty input in memory for the
    // lifetime of this Settings screen. Re-focus requires an explicit list
    // and record read before the draft is shown again.
    setSessionVisible(false);
    if (!isActive) setPendingAction(null);
  }, [isActive]);

  const run = async <T,>(operation: () => Promise<T>, onSuccess: (value: T) => void) => {
    if (!activeRef.current || !mountedRef.current || currentIdentity.current !== identity || busyRef.current) return;
    busyRef.current = true;
    const request = ++generation.current;
    const active = () => currentIdentity.current === identity
      && activeRef.current
      && mountedRef.current
      && generation.current === request;
    setBusy(true);
    setErrorReason(null);
    try {
      const value = await operation();
      if (active()) onSuccess(value);
    } catch {
      if (active()) {
        clearHistoryVisibleState(true);
        setErrorReason("transaction_failed");
      }
    } finally {
      if (mountedRef.current && currentIdentity.current === identity) {
        busyRef.current = false;
        setBusy(false);
      }
    }
  };

  const hasDirtyDraft = () => draftDirtyRef.current;

  const clearHistoryVisibleState = (clearList = false) => {
    setSessionVisible(false);
    setRecord(null);
    setRecordKey(null);
    setStatus(null);
    setPendingAction(null);
    if (clearList) {
      setLoaded(false);
      setLoadedKey(null);
      setItems([]);
    }
  };

  const guardDirty = (
    action: Exclude<NonNullable<typeof pendingAction>, null>,
  ): boolean => {
    if (!hasDirtyDraft()) return false;
    setPendingAction(action);
    return true;
  };

  const loadHistory = () => {
    if (!identity || !activeRef.current) return;
    void run(
      () => listOutboxResolutionHistory(scope),
      (result: unknown) => {
        if (isSuccess<unknown>(result)) {
          setItems(safeHistory(result.value));
          setLoaded(true);
          setLoadedKey(dataKey);
          setSessionVisible(false);
        } else {
          clearHistoryVisibleState(true);
          setItems([]);
          setLoaded(true);
          setErrorReason(resultReason(result));
        }
      },
    );
  };

  const loadHistoryRef = useRef(loadHistory);
  loadHistoryRef.current = loadHistory;
  const previousLoadRequest = useRef<number | undefined>(undefined);
  useEffect(() => {
    if (!isActive || loadRequest === undefined || loadRequest <= 0 || previousLoadRequest.current === loadRequest) return;
    previousLoadRequest.current = loadRequest;
    loadHistoryRef.current();
  }, [loadRequest]);

  const readRecord = (item: ResolutionHistoryItem) => {
    if (!activeRef.current) return;
    if (guardDirty({ kind: "read", item })) return;
    void run(
      () => readOutboxResolutionRecord(item.id, scope),
      (result: unknown) => {
        if (isSuccess<ResolutionRecordView>(result)) {
          setRecord(result.value);
          setRecordKey(dataKey);
          setDraft(draftValues(result.value));
          draftDirtyRef.current = false;
          setSessionVisible(true);
          setStatus(null);
        } else {
          clearHistoryVisibleState();
          setRecord(null);
          setErrorReason(resultReason(result));
        }
      },
    );
  };

  const refreshPendingRecordPreservingDraft = () => {
    const pending = pendingAction;
    if (!activeRef.current || !pending || pending.kind !== "read") return;
    void run(
      () => readOutboxResolutionRecord(pending.item.id, scope),
      (result: unknown) => {
        if (isSuccess<ResolutionRecordView>(result)) {
          setRecord(result.value);
          setRecordKey(dataKey);
          setSessionVisible(true);
          setPendingAction(null);
        } else {
          clearHistoryVisibleState();
          setErrorReason(resultReason(result));
        }
      },
    );
  };

  const restoreDraft = () => {
    if (!activeRef.current) return;
    const visibleRecord = recordKey === dataKey ? record : null;
    if (!visibleRecord) return;
    if (guardDirty({ kind: "restore" })) return;
    void run(
      () => restoreOutboxResolutionDraft(visibleRecord.item.id, scope, visibleRecord.item.revision),
      (result: unknown) => {
        if (isSuccess<ResolutionRecordView>(result)) {
          setRecord(result.value);
          setRecordKey(dataKey);
          setDraft(draftValues(result.value));
          draftDirtyRef.current = false;
          setStatus(null);
        } else {
          clearHistoryVisibleState();
          setErrorReason(resultReason(result));
        }
      },
    );
  };

  const saveDraft = () => {
    if (!activeRef.current) return;
    const visibleRecord = recordKey === dataKey ? record : null;
    if (!visibleRecord || !draft || !hasDirtyDraft()) return;
    // Persist only fields explicitly edited in this session. The core CAS
    // merges this partial change set with the immutable journal snapshot.
    const changes: ResolutionJsonObject = {};
    if (draft.dirty.title) changes.title = draft.title;
    if (draft.dirty.description) changes.description = draft.description;
    if (draft.dirty.content && draft.contentPath?.length === 2) {
      const [containerKey, contentKey] = draft.contentPath;
      const container = changes[containerKey];
      const original = container && typeof container === "object" && !Array.isArray(container)
        ? container as Record<string, never>
        : {};
      changes[containerKey] = { ...original, [contentKey]: draft.content } as never;
    } else if (draft.dirty.content && draft.contentPath?.length === 1) {
      changes[draft.contentPath[0]] = draft.content;
    }
    void run(
      () => saveOutboxResolutionDraft(visibleRecord.item.id, scope, visibleRecord.item.revision, changes),
      (result: unknown) => {
        if (isSuccess<ResolutionRecordView>(result)) {
          setRecord(result.value);
          setRecordKey(dataKey);
          setDraft(draftValues(result.value));
          draftDirtyRef.current = false;
          setPendingAction(null);
          setStatus("saved");
        } else {
          clearHistoryVisibleState();
          setErrorReason(resultReason(result));
        }
      },
    );
  };

  const exportOriginal = () => {
    if (!isActive) return;
    const visibleRecord = recordKey === dataKey ? record : null;
    if (!visibleRecord) return;
    if (guardDirty({ kind: "export" })) return;
    void run(
      () => exportOutboxResolutionRecord(visibleRecord.item.id, scope, visibleRecord.item.revision),
      (result: unknown) => {
        if (isSuccess<{ status: string }>(result)) {
          const exportStatus = result.value.status;
          setStatus(exportStatus === "shared" || exportStatus === "cancelled" ? exportStatus : "exported");
        } else {
          clearHistoryVisibleState();
          setErrorReason(resultReason(result));
        }
      },
    );
  };

  const visibleLoaded = isActive && loaded && loadedKey === dataKey;
  const visibleItems = visibleLoaded ? items : [];
  const visibleRecord = isActive && sessionVisible && recordKey === dataKey ? record : null;

  return <View style={styles.container} testID="outbox-resolution-history">
    <Button testID="outbox-resolution-history-open" mode="outlined" disabled={!isActive || busy} loading={busy} onPress={loadHistory}>
      {loadRequest === undefined ? "保存した原値と下書きを確認" : "履歴を再読み込み"}
    </Button>
    {errorReason ? <Text style={styles.reason}>{reasonLabel(errorReason)}</Text> : null}
    {isActive && pendingAction ? <View style={styles.confirmation} testID="outbox-resolution-unsaved-confirmation">
      <Text style={styles.itemTitle}>未保存の下書きがあります</Text>
      <Text style={styles.bodyText}>先に下書きを保存するか、保持して戻ってください。未保存の入力は自動で破棄しません。</Text>
      <Button mode="outlined" disabled={busy} onPress={saveDraft}>下書きを保存</Button>
      {pendingAction?.kind === "read" ? (
        <Button mode="outlined" disabled={busy} onPress={refreshPendingRecordPreservingDraft}>
          保持したまま再表示
        </Button>
      ) : null}
      <Button mode="text" disabled={busy} onPress={() => setPendingAction(null)}>保持して戻る</Button>
    </View> : null}
    {visibleLoaded && visibleItems.length === 0 ? <Text style={styles.bodyText}>保存した原値はありません。</Text> : null}
    {visibleItems.map((item) => <View key={item.id} style={styles.item}>
      <Text style={styles.itemTitle}>{item.kind}・{item.entityId}</Text>
      <Text style={styles.bodyText}>状態: {STATE_LABELS[item.state] ?? "保持中"}</Text>
      <Text style={styles.bodyText}>操作ID: {item.opId}</Text>
      {item.hasDraft ? <Text style={styles.bodyText}>未送信下書きあり</Text> : null}
      <Button mode="outlined" disabled={busy} onPress={() => readRecord(item)}>
        内容を確認
      </Button>
    </View>)}
    {visibleRecord ? <View style={styles.record}>
      <Text style={styles.bodyText}>記録版: {visibleRecord.item.revision}</Text>
      <ResolutionPreviewDetails preview={{
        capture: {} as never,
        view: visibleRecord.view,
      }} fallback={fallbackFor(visibleRecord)} historyMode />
      <Button mode="outlined" disabled={busy} onPress={exportOriginal}>
        原値をファイルに保存
      </Button>
      {!visibleRecord.item.hasDraft && !visibleRecord.draft ? (
        <Button mode="outlined" disabled={busy} onPress={restoreDraft}>
          原値を未送信下書きに復元
        </Button>
      ) : visibleRecord.item.hasDraft && !visibleRecord.draft ? (
        <Button mode="outlined" disabled={busy} onPress={() => readRecord(visibleRecord.item)}>
          保存した下書きを開く
        </Button>
      ) : null}
      {draft ? <View style={styles.draft} testID="outbox-resolution-draft">
        <Text style={styles.itemTitle}>未送信下書き</Text>
        <Text style={styles.label}>タイトル</Text>
        <TextInput
          accessibilityLabel="下書きタイトル"
          value={draft.title}
          editable={!busy}
          onChangeText={(title) => {
            draftDirtyRef.current = true;
            setDraft((current) => current ? {
              ...current,
              title,
              dirty: { ...current.dirty, title: true },
            } : current);
          }}
          style={styles.input}
        />
        <Text style={styles.label}>説明</Text>
        <TextInput
          accessibilityLabel="下書き説明"
          value={draft.description}
          editable={!busy}
          onChangeText={(description) => {
            draftDirtyRef.current = true;
            setDraft((current) => current ? {
              ...current,
              description,
              dirty: { ...current.dirty, description: true },
            } : current);
          }}
          style={styles.input}
        />
        {draft.contentPath ? <>
          <Text style={styles.label}>入力内容</Text>
          <TextInput
            accessibilityLabel="下書き入力内容"
            value={draft.content}
            editable={!busy}
            multiline
            onChangeText={(content) => {
              draftDirtyRef.current = true;
              setDraft((current) => current ? {
                ...current,
                content,
                dirty: { ...current.dirty, content: true },
              } : current);
            }}
            style={[styles.input, styles.multiline]}
          />
        </> : null}
        {draft.searchMirror !== null ? <>
          <Text style={styles.label}>検索用ミラー</Text>
          <Text selectable style={styles.mirror}>{draft.searchMirror}</Text>
        </> : null}
        <Text style={styles.bodyText}>原値の構造と未編集の項目は保持されます。保存しても送信待ちやサーバーは変更しません。</Text>
        <Button mode="outlined" disabled={busy || !hasDirtyDraft()} loading={busy} onPress={saveDraft}>
          下書きを保存
        </Button>
      </View> : null}
    </View> : null}
    {visibleRecord && status === "saved" ? <Text style={styles.success}>端末内の下書きを保存しました。</Text> : null}
    {visibleRecord && status === "exported" ? <Text style={styles.success}>原値をファイルに保存しました。</Text> : null}
    {visibleRecord && status === "shared" ? <Text style={styles.success}>共有画面を開きました。保存先で確認してください。</Text> : null}
    {visibleRecord && status === "cancelled" ? <Text style={styles.reason}>保存をキャンセルしました。原値は保持しています。</Text> : null}
  </View>;
}

const styles = StyleSheet.create({
  container: { gap: 8, marginTop: 8 },
  item: { gap: 5, paddingVertical: 8, borderTopWidth: 1, borderTopColor: "#343149" },
  record: { gap: 8, paddingTop: 8 },
  draft: { gap: 6, padding: 10, borderRadius: 8, backgroundColor: "#26243a" },
  confirmation: { gap: 6, padding: 10, borderRadius: 8, backgroundColor: "#26243a" },
  itemTitle: { color: "#f0efff", fontSize: 13, fontWeight: "600" },
  label: { color: "#cdd6f4", fontSize: 12 },
  input: { color: "#f0efff", backgroundColor: "#1b1a2b", borderWidth: 1, borderColor: "#343149", borderRadius: 6, paddingHorizontal: 8, paddingVertical: 7, minHeight: 40 },
  multiline: { minHeight: 84, textAlignVertical: "top" },
  mirror: { color: "#a6adc8", fontSize: 12, padding: 8, backgroundColor: "#1b1a2b", borderRadius: 6 },
  bodyText: { color: "#cdd6f4", fontSize: 12 },
  reason: { color: "#f38ba8", fontSize: 12 },
  success: { color: "#a6e3a1", fontSize: 12 },
});
