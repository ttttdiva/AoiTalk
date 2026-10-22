import React, {
  useEffect,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";
import { Pressable, StyleSheet, View } from "react-native";
import { Button, Text } from "react-native-paper";
import { useAuth } from "../contexts/AuthContext";
import {
  getOutboxDiagnostics,
  type OutboxDiagnostic,
} from "../repositories/outbox";
import {
  requestOutboxResolutionTarget,
  confirmOutboxResolutionTarget,
  inspectOutboxResolution,
  prepareOutboxResolution,
  adoptOutboxServer,
} from "../repositories/outbox-resolution";
import type {
  ResolutionField,
  ResolutionPreview,
  ResolutionRecordView,
} from "../repositories/outbox-resolution-types";
import { useSyncStatusStore } from "../stores/sync-status";
import { exportOutboxResolutionRecord } from "../lib/outbox-resolution-export";
import {
  getOutboxResolutionEditorRevision,
  subscribeOutboxResolutionEditors,
} from "../lib/outbox-resolution-editor-guard";
import {
  outboxResolutionDomainKey,
  useOutboxResolutionIdentity,
  type OutboxResolutionIdentity,
} from "./use-outbox-resolution-identity";
import { isMobileReadOnlyDiagnosticsEnabled } from "../lib/read-only-diagnostics";

const REASONS: Record<OutboxDiagnostic["errorKind"], string> = {
  missing_entity: "サーバーに対象が見つかりません。端末の更新は保持しています",
  conflict: "サーバー上の変更と競合しています",
  scope: "保存先または権限を確認する必要があります",
  missing_base: "サーバーの更新版を確認する必要があります",
  server_error: "サーバーが操作を受理できませんでした",
  pending: "送信を待っています",
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
  server_version_missing: "サーバー版を確認できません。原値を保持しています。",
  local_snapshot_changed: "端末の操作が更新されています。原値を保持しています。",
  local_snapshot_unavailable: "端末の最新状態を取得できません。原値を保持しています。",
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

const SERVER_STATE_LABELS: Record<string, string> = {
  present: "対象あり",
  not_found: "対象なし（404）",
  forbidden: "参照権限なし",
  unavailable: "取得できません",
  invalid: "応答を確認できません",
  unsupported: "対象外",
};

const ACTION_LABELS: Record<string, string> = {
  create: "新規保存",
  update: "更新",
  delete: "削除",
  archive: "アーカイブ",
};

const TABLE_LABELS: Record<string, string> = {
  tasks: "Task",
  knowledge_nodes: "Docs",
};

function areaLabel(table: string): string {
  return TABLE_LABELS[table]
    ?? (table.startsWith("knowledge_") ? "Docs" : "保存データ");
}

function reasonLabel(reason: string | null | undefined): string {
  if (!reason) return "";
  return REASON_LABELS[reason] ?? "安全のため、この操作は実行できません。元の操作を保持しています。";
}

function resultReason(result: unknown): string | null {
  if (!result || typeof result !== "object") return "transaction_failed";
  const candidate = result as { ok?: unknown; reason?: unknown };
  return candidate.ok === false && typeof candidate.reason === "string"
    ? candidate.reason
    : "transaction_failed";
}

const CLEAR_RESOLUTION_REASONS = new Set([
  "context_changed",
  "auth_scope_changed",
  "scope_changed",
  "origin_confirmation_required",
]);

function shouldClearResolution(reason: unknown): boolean {
  return typeof reason === "string" && CLEAR_RESOLUTION_REASONS.has(reason);
}

function isSuccess<T>(result: unknown): result is { ok: true; value: T } {
  return Boolean(
    result
      && typeof result === "object"
      && (result as { ok?: unknown }).ok === true,
  );
}

function finiteString(value: unknown): string | null {
  if (typeof value !== "string" || !value.trim()) return null;
  return value.length <= 256 ? value : `${value.slice(0, 256)}…`;
}

const SAFE_VALUE_KEYS = new Set([
  "id", "title", "description", "status", "priority", "project_id", "projectId",
  "workspace_id", "workspaceId", "parent_id", "parentId", "root_page_id", "rootPageId",
  "node_type", "nodeType", "system_key", "systemKey", "aliases", "content", "body",
  "body_json", "bodyJson", "body_text", "bodyText", "query_json", "queryJson",
  "view_json", "viewJson", "start_at", "startAt", "end_at", "endAt", "all_day", "allDay",
  "reminder_offsets", "notifications_enabled", "auto_close_on_due", "estimated_hours",
  "parent_task_id", "parentTaskId", "task_metadata", "taskMetadata", "metadata", "sort_order",
  "sortOrder", "assignee_ids", "assigneeIds", "tag_ids", "tagIds", "created_at", "createdAt",
  "updated_at", "updatedAt", "completed_at", "completedAt", "archived_at", "archivedAt",
]);
const PRIVATE_VALUE_KEY = /(?:token|password|passwd|cookie|authorization|credential|secret|api[_-]?key|last[_-]?error|blocked[_-]?reason)/i;
const MAX_DISPLAY_LENGTH = 512;

type SafeDisplay = { text: string; truncated: boolean };

function safeObjectKey(key: string): boolean {
  return SAFE_VALUE_KEYS.has(key) && !PRIVATE_VALUE_KEY.test(key);
}

function controlledValue(value: unknown, depth = 0): unknown {
  if (value === null || value === undefined) return null;
  if (typeof value === "string" || typeof value === "boolean") return value;
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  if (depth >= 3) return "複合値";
  if (Array.isArray(value)) return value.slice(0, 128).map((item) => controlledValue(item, depth + 1));
  if (typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>)
      .filter(([key]) => safeObjectKey(key))
      .map(([key, item]) => [key, controlledValue(item, depth + 1)] as const);
    return entries.length ? Object.fromEntries(entries) : "複合値";
  }
  return "未取得";
}

function displaySafeValue(value: unknown, fieldKey: string, expanded: boolean, missing = false): SafeDisplay {
  if (missing || value === undefined) return { text: "未取得", truncated: false };
  if (value === null) return { text: "null（空）", truncated: false };
  // Docs typed content is user content.  Show its content member as text and
  // keep the other body_json members behind the controlled object projection.
  if ((fieldKey === "body_json" || fieldKey === "bodyJson")
    && value && typeof value === "object" && !Array.isArray(value)) {
    const content = (value as Record<string, unknown>).content;
    if (typeof content === "string") return displaySafeValue(content, "content", expanded);
  }
  const controlled = controlledValue(value);
  const raw = typeof controlled === "string"
    ? controlled
    : JSON.stringify(controlled) ?? "未取得";
  if (raw.length <= MAX_DISPLAY_LENGTH || expanded) return { text: raw === "" ? "空文字" : raw, truncated: false };
  return { text: `${raw.slice(0, MAX_DISPLAY_LENGTH)}…`, truncated: true };
}

function comparisonLabel(comparison: ResolutionField["comparison"]): string {
  if (comparison === "same") return "一致";
  if (comparison === "different") return "異なります";
  return "比較未確認";
}

function useEditorRevision(): number {
  return useSyncExternalStore(
    subscribeOutboxResolutionEditors,
    getOutboxResolutionEditorRevision,
    getOutboxResolutionEditorRevision,
  );
}

function safeRows(value: unknown): OutboxDiagnostic[] {
  if (!Array.isArray(value)) return [];
  return value.filter((row): row is OutboxDiagnostic => {
    if (!row || typeof row !== "object") return false;
    const candidate = row as Partial<OutboxDiagnostic>;
    return typeof candidate.opId === "string"
      && Boolean(candidate.opId.trim())
      && typeof candidate.entityId === "string"
      && Boolean(candidate.entityId.trim())
      && typeof candidate.tableName === "string"
      && Boolean(candidate.tableName.trim())
      && typeof candidate.action === "string"
      && Boolean(candidate.action.trim())
      && typeof candidate.retryCount === "number"
      && Number.isFinite(candidate.retryCount)
      && typeof candidate.errorKind === "string"
      && Object.prototype.hasOwnProperty.call(REASONS, candidate.errorKind);
  }).map((row) => ({
    opId: row.opId,
    tableName: row.tableName,
    action: row.action,
    entityId: row.entityId,
    retryCount: Math.max(0, row.retryCount),
    docsScopeKey: finiteString(row.docsScopeKey) ?? null,
    baseUpdatedAt: finiteString(row.baseUpdatedAt) ?? null,
    hasBaseSnapshot: row.hasBaseSnapshot === true,
    hasConflictSnapshot: row.hasConflictSnapshot === true,
    blockedReason: null,
    errorKind: row.errorKind,
  }));
}

function PreviewMetadata({
  preview,
  fallback,
  historyMode = false,
}: {
  preview: ResolutionPreview;
  fallback: Pick<OutboxDiagnostic, "tableName" | "action" | "docsScopeKey" | "baseUpdatedAt" | "hasBaseSnapshot" | "hasConflictSnapshot">;
  historyMode?: boolean;
}) {
  const view = preview.view;
  const operationOrigin = (view as ResolutionPreview["view"] & {
    operationOrigin?: string;
    resolutionTargetLabel?: string;
  }).operationOrigin;
  const resolutionTargetLabel = (view as ResolutionPreview["view"] & {
    operationOrigin?: string;
    resolutionTargetLabel?: string;
  }).resolutionTargetLabel;
  const serverSource = view.serverSource;
  const isLiveSource = serverSource === "live_get" || serverSource === "live_current";
  const liveReadAt = view.liveReadAt;
  const serverCapturedAt = view.serverCapturedAt;
  const tableName = finiteString(view.tableName) ?? fallback.tableName;
  const action = finiteString(view.action) ?? fallback.action;
  const docsScopeKey = finiteString(view.docsScopeKey) ?? fallback.docsScopeKey;
  return <View style={styles.metadata}>
    <Text style={styles.metaText}>種類: {areaLabel(tableName)}（{view.kind}）</Text>
    <Text style={styles.metaText}>操作作成元server: {operationOrigin === "unrecorded" ? "未記録" : "未取得"}</Text>
    <Text style={styles.metaText}>比較・解決先: {finiteString(resolutionTargetLabel) ?? "未取得"}</Text>
    <Text style={styles.metaText}>table: {tableName}</Text>
    <Text style={styles.metaText}>action: {action}</Text>
    {tableName.startsWith("knowledge_") || docsScopeKey ? (
      <>
        <Text style={styles.metaText}>docsScopeKey: {docsScopeKey ?? "未取得"}</Text>
        <Text style={styles.metaText}>base version: {finiteString(view.baseUpdatedAt) ?? finiteString(fallback.baseUpdatedAt) ?? "未取得"}</Text>
        <Text style={styles.metaText}>base snapshot: {view.hasBaseSnapshot || fallback.hasBaseSnapshot ? "あり" : "なし"}</Text>
        <Text style={styles.metaText}>conflict snapshot: {view.hasConflictSnapshot || fallback.hasConflictSnapshot ? "あり" : "なし"}</Text>
      </>
    ) : null}
    <Text style={styles.metaText}>サーバー状態: {SERVER_STATE_LABELS[view.serverState] ?? "確認できません"}</Text>
    {historyMode ? (
      <>
        <Text style={styles.metaText}>保存時サーバーsnapshot: {isLiveSource ? "保存時にlive取得" : "未取得"}</Text>
        <Text style={styles.metaText}>保存時の取得時刻: {finiteString(liveReadAt) ?? "未記録"}</Text>
        <Text style={styles.metaText}>保存時サーバー版: {finiteString(view.serverUpdatedAt) ?? "未取得"}</Text>
      </>
    ) : (
      <>
        <Text style={styles.metaText}>現在サーバー: {isLiveSource ? "今回取得" : "未取得"}</Text>
        <Text style={styles.metaText}>取得時刻: {finiteString(liveReadAt) ?? "未記録"}</Text>
        <Text style={styles.metaText}>サーバー版: {finiteString(view.serverUpdatedAt) ?? "未取得"}</Text>
      </>
    )}
    <Text style={styles.metaText}>サーバー記録時刻: {finiteString(serverCapturedAt) ?? "未記録"}</Text>
    <Text style={styles.metaText}>照合時刻: {finiteString(view.checkedAt) ?? "未記録"}</Text>
  </View>;
}

function PreviewFields({ fields, historyMode = false }: { fields: ResolutionField[]; historyMode?: boolean }) {
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  if (!fields.length) return <Text style={styles.bodyText}>比較できる項目はありません。</Text>;
  return <View style={styles.fields}>
    {fields.map((field) => (
      <View key={field.key} style={styles.field}>
        <Text style={styles.fieldTitle}>
          {finiteString(field.label) ?? field.key}（{comparisonLabel(field.comparison)}）
        </Text>
        {(() => {
          const device = displaySafeValue(field.device, field.key, expanded[`${field.key}:device`] === true);
          const server = displaySafeValue(
            field.server,
            field.key,
            expanded[`${field.key}:server`] === true,
            !field.serverKnown,
          );
          return <>
            <Text selectable style={styles.bodyText}>端末変更: {device.text}</Text>
            {device.truncated ? <Button mode="text" compact onPress={() => setExpanded((current) => ({ ...current, [`${field.key}:device`]: true }))}>全文を表示</Button> : null}
            <Text selectable style={styles.bodyText}>
              {historyMode ? "保存時サーバー値" : "サーバー現在値"}: {server.text}
            </Text>
            {server.truncated ? <Button mode="text" compact onPress={() => setExpanded((current) => ({ ...current, [`${field.key}:server`]: true }))}>全文を表示</Button> : null}
          </>;
        })()}
      </View>
    ))}
  </View>;
}

export function ResolutionPreviewDetails({
  preview,
  fallback,
  historyMode = false,
}: {
  preview: ResolutionPreview;
  fallback: Pick<OutboxDiagnostic, "tableName" | "action" | "docsScopeKey" | "baseUpdatedAt" | "hasBaseSnapshot" | "hasConflictSnapshot">;
  historyMode?: boolean;
}) {
  const view = preview.view;
  return <View style={styles.detail}>
    <PreviewMetadata preview={preview} fallback={fallback} historyMode={historyMode} />
    <PreviewFields fields={Array.isArray(view.fields) ? view.fields : []} historyMode={historyMode} />
    {view.reason ? <Text style={styles.reason}>{reasonLabel(view.reason)}</Text> : null}
    {!view.localComplete ? <Text style={styles.reason}>端末側の必要な記録を確認できません。元の操作を保持しています。</Text> : null}
    {view.serverComplete === false ? <Text style={styles.reason}>サーバー値の一部だけ確認できたため、採用は無効です。</Text> : null}
  </View>;
}

type ResolutionCardProps = {
  row: OutboxDiagnostic;
  authScope: string;
  resolutionIdentityKey?: string;
};

type ResolutionTargetRequestView = {
  confirmation: object;
  targetLabel: string;
  operationOrigin: string;
  serverHost?: string | null;
  host?: string | null;
  serverPort?: number | string | null;
  port?: number | string | null;
};

/** A single explicit compare / prepare / adopt flow shared by Task and Docs. */
export function OutboxResolution({ row, authScope, resolutionIdentityKey }: ResolutionCardProps) {
  const identity = `${resolutionIdentityKey ?? authScope}|${row.opId}|${row.tableName}|${row.entityId}`;
  const currentIdentity = useRef(identity);
  currentIdentity.current = identity;
  const editorRevision = useEditorRevision();
  const dataKey = `${identity}|editor:${editorRevision}`;
  const generation = useRef(0);
  const mountedRef = useRef(true);
  const busyRef = useRef(false);
  const [busy, setBusy] = useState(false);
  const [preview, setPreview] = useState<ResolutionPreview | null>(null);
  const [record, setRecord] = useState<ResolutionRecordView | null>(null);
  const [dataIdentity, setDataIdentity] = useState<string | null>(null);
  const [targetRequest, setTargetRequest] = useState<ResolutionTargetRequestView | null>(null);
  const [capability, setCapability] = useState<object | null>(null);
  const [confirmAdopt, setConfirmAdopt] = useState(false);
  const [status, setStatus] = useState<"saved" | "adopted" | "exported" | "shared" | "cancelled" | null>(null);
  const [errorReason, setErrorReason] = useState<string | null>(null);

  const clearResolutionState = () => {
    setPreview(null);
    setRecord(null);
    setDataIdentity(null);
    setTargetRequest(null);
    setCapability(null);
    setConfirmAdopt(false);
    setStatus(null);
  };

  useEffect(() => {
    mountedRef.current = true;
    generation.current += 1;
    busyRef.current = false;
    setBusy(false);
    setPreview(null);
    setRecord(null);
    setDataIdentity(null);
    setTargetRequest(null);
    setCapability(null);
    setConfirmAdopt(false);
    setStatus(null);
    setErrorReason(null);
    return () => {
      mountedRef.current = false;
      generation.current += 1;
    };
  }, [identity, editorRevision]);

  const supported = row.tableName === "tasks" || row.tableName.startsWith("knowledge_");
  const run = async <T,>(
    operation: (isCurrent: () => boolean) => Promise<T>,
    onSuccess: (value: T) => void,
  ) => {
    if (!mountedRef.current || currentIdentity.current !== identity || busyRef.current) return;
    busyRef.current = true;
    const request = ++generation.current;
    const requestRevision = editorRevision;
    const active = () => currentIdentity.current === identity
      && mountedRef.current
      && generation.current === request
      && getOutboxResolutionEditorRevision() === requestRevision;
    setBusy(true);
    setErrorReason(null);
    try {
      const value = await operation(active);
      if (active()) onSuccess(value);
    } catch {
      if (active()) setErrorReason("transaction_failed");
    } finally {
      if (active()) {
        busyRef.current = false;
        setBusy(false);
      }
    }
  };

  const requestTarget = () => {
    void run(
      () => requestOutboxResolutionTarget(row.opId, authScope),
      (result: unknown) => {
        if (isSuccess<ResolutionTargetRequestView>(result)) {
          setTargetRequest(result.value);
          setErrorReason(null);
        } else {
          if (shouldClearResolution(resultReason(result))) clearResolutionState();
          setErrorReason(resultReason(result));
        }
      },
    );
  };

  const confirmTarget = () => {
    const pending = targetRequest;
    if (!pending) return;
    void run(
      async (isCurrent) => {
        const confirmed = await confirmOutboxResolutionTarget(pending.confirmation);
        if (!isSuccess<object>(confirmed)) return confirmed;
        // The target confirmation is a capability boundary. If the card was
        // blurred, unmounted, or moved to another owner while it awaited the
        // confirmation, do not start the live GET at all.
        if (!isCurrent()) return { ok: false, reason: "context_changed" };
        const inspected = await inspectOutboxResolution(row.opId, authScope, confirmed.value);
        if (!isCurrent()) return { ok: false, reason: "context_changed" };
        if (!isSuccess<ResolutionPreview>(inspected)) return inspected;
        return { ok: true, value: { preview: inspected.value, capability: confirmed.value } };
      },
      (result: unknown) => {
        setTargetRequest(null);
        if (isSuccess<{ preview: ResolutionPreview; capability: object }>(result)) {
          setCapability(result.value.capability);
          setPreview(result.value.preview);
          setDataIdentity(dataKey);
          setRecord(null);
        } else {
          setPreview(null);
          if (shouldClearResolution(resultReason(result))) clearResolutionState();
          setErrorReason(resultReason(result));
        }
      },
    );
  };

  const prepare = () => {
    const visiblePreview = dataIdentity === dataKey ? preview : null;
    if (!visiblePreview || !visiblePreview.view.canPrepare) return;
    void run(
      () => prepareOutboxResolution(visiblePreview),
      (result: unknown) => {
        if (isSuccess<ResolutionRecordView>(result)) {
          setRecord(result.value);
          setDataIdentity(dataKey);
          setStatus("saved");
        } else {
          if (shouldClearResolution(resultReason(result))) clearResolutionState();
          setErrorReason(resultReason(result));
        }
      },
    );
  };

  const exportOriginal = () => {
    const visibleRecord = dataIdentity === dataKey ? record : null;
    if (!visibleRecord) return;
    void run(
      () => exportOutboxResolutionRecord(visibleRecord.item.id, authScope, visibleRecord.item.revision),
      (result: unknown) => {
        if (isSuccess<{ status: string }>(result)) {
          const exportStatus = result.value.status;
          setStatus(exportStatus === "shared" || exportStatus === "cancelled" ? exportStatus : "exported");
        } else {
          if (shouldClearResolution(resultReason(result))) clearResolutionState();
          setErrorReason(resultReason(result));
        }
      },
    );
  };

  const adopt = () => {
    const visibleRecord = dataIdentity === dataKey ? record : null;
    if (!visibleRecord || !visibleRecord.view.canAdopt) return;
    setConfirmAdopt(true);
  };

  const confirmAdoption = () => {
    const visibleRecord = dataIdentity === dataKey ? record : null;
    if (!visibleRecord) return;
    setConfirmAdopt(false);
    void run(
      () => adoptOutboxServer(visibleRecord.item.id, authScope, visibleRecord.item.revision, capability ?? undefined),
      (result: unknown) => {
        if (isSuccess<ResolutionRecordView>(result)) {
          setRecord(result.value);
          setDataIdentity(dataKey);
          setStatus("adopted");
        } else {
          if (shouldClearResolution(resultReason(result))) clearResolutionState();
          setErrorReason(resultReason(result));
        }
      },
    );
  };

  const visiblePreview = dataIdentity === dataKey ? preview : null;
  const visibleRecord = dataIdentity === dataKey ? record : null;

  return <View style={styles.resolution} testID={`outbox-resolution-${row.opId}`}>
    {supported ? (
      <Button testID={`outbox-resolution-compare-${row.opId}`} mode="outlined" disabled={busy} loading={busy} onPress={requestTarget}>
        現在のサーバーと比較
      </Button>
    ) : (
      <Text style={styles.reason}>この操作は解決対象外です。理由を確認できないため、原値を保持しています。</Text>
    )}
    {errorReason ? <Text style={styles.reason}>{reasonLabel(errorReason)}</Text> : null}
    {targetRequest ? <View style={styles.confirmation} testID={`outbox-resolution-target-${row.opId}`}>
      <Text style={styles.confirmTitle}>比較・解決先の確認</Text>
      <Text style={styles.bodyText}>操作作成元serverは未記録</Text>
      <Text style={styles.bodyText}>現在の接続先を比較・解決先として指定</Text>
      <Text style={styles.metaText}>対象: {finiteString(targetRequest.targetLabel) ?? "未取得"}</Text>
      <Text style={styles.metaText}>接続先host: {finiteString(targetRequest.serverHost ?? targetRequest.host) ?? "未取得"}</Text>
      <Text style={styles.metaText}>接続先port: {targetRequest.serverPort ?? targetRequest.port ?? "未取得"}</Text>
      <View style={styles.confirmActions}>
        <Pressable accessibilityRole="button" onPress={confirmTarget} disabled={busy} style={styles.nativeButton}>
          <Text style={styles.nativeButtonText}>この接続先を指定して比較</Text>
        </Pressable>
        <Pressable accessibilityRole="button" onPress={() => setTargetRequest(null)} disabled={busy} style={styles.nativeButton}>
          <Text style={styles.nativeButtonText}>戻る</Text>
        </Pressable>
      </View>
    </View> : null}
    {visiblePreview ? <>
      <ResolutionPreviewDetails preview={visiblePreview} fallback={row} />
      {!visibleRecord ? (
        <Button
          mode="outlined"
          disabled={busy || !visiblePreview.view.canPrepare}
          loading={busy}
          onPress={prepare}
        >
          原値を保存して保持
        </Button>
      ) : null}
    </> : null}
    {visibleRecord ? <View style={styles.recordActions}>
      <Text style={styles.bodyText}>原値を保存しました。送信待ちは保持しています。</Text>
      <Button mode="outlined" disabled={busy} loading={busy} onPress={exportOriginal}>
        原値をファイルに保存
      </Button>
      <Button
        mode="outlined"
        disabled={busy || !visibleRecord.view.canAdopt}
        loading={busy}
        onPress={adopt}
      >
        現在のサーバー版を採用
      </Button>
    </View> : null}
    {visibleRecord && status === "exported" ? <Text style={styles.success}>原値をファイルに保存しました。</Text> : null}
    {visibleRecord && status === "shared" ? <Text style={styles.success}>共有画面を開きました。保存先で確認してください。</Text> : null}
    {visibleRecord && status === "cancelled" ? <Text style={styles.reason}>保存をキャンセルしました。原値は保持しています。</Text> : null}
    {visibleRecord && status === "adopted" ? <Text style={styles.success}>この1件を採用しました。送信待ちの一覧を手動で再確認してください。</Text> : null}
    {confirmAdopt && visibleRecord ? <View style={styles.confirmation} testID={`outbox-resolution-confirm-${row.opId}`}>
      <Text style={styles.confirmTitle}>現在のサーバー版を採用しますか？</Text>
      <Text style={styles.bodyText}>この1件の端末変更を保全して採用、選択した操作だけ除きます。</Text>
      <View style={styles.confirmActions}>
        <Pressable accessibilityRole="button" onPress={confirmAdoption} disabled={busy} style={styles.nativeButton}>
          <Text style={styles.nativeButtonText}>採用を確認</Text>
        </Pressable>
        <Pressable accessibilityRole="button" onPress={() => setConfirmAdopt(false)} disabled={busy} style={styles.nativeButton}>
          <Text style={styles.nativeButtonText}>戻る</Text>
        </Pressable>
      </View>
    </View> : null}
  </View>;
}

export function OutboxDiagnostics({
  resolutionIdentity = undefined,
}: {
  resolutionIdentity?: OutboxResolutionIdentity | null;
} = {}) {
  const { user, isAuthenticated, isAnonymous } = useAuth();
  const identityHint = typeof user?.user_id === "string" ? user.user_id : "";
  const ownIdentityState = useOutboxResolutionIdentity(
    resolutionIdentity === undefined && isAuthenticated && !isAnonymous,
    identityHint,
  );
  const canonicalIdentity = resolutionIdentity === undefined
    ? ownIdentityState.identity
    : resolutionIdentity;
  const scope = isAuthenticated && !isAnonymous ? canonicalIdentity?.authScope ?? null : null;
  const resolutionIdentityKey = canonicalIdentity ? outboxResolutionDomainKey(canonicalIdentity) : null;
  const currentAuthScope = useSyncStatusStore((value) => value.authScope);
  const state = useSyncStatusStore((value) => value.outboxState);
  const readOnlyDiagnostics = isMobileReadOnlyDiagnosticsEnabled();
  const currentIdentity = `${resolutionIdentityKey ?? ""}|${currentAuthScope ?? ""}|${state}`;
  const currentScope = useRef(currentIdentity);
  currentScope.current = currentIdentity;
  const generation = useRef(0);
  const [result, setResult] = useState<{ identity: string; rows: OutboxDiagnostic[] } | null>(null);
  const [loading, setLoading] = useState(false);
  const [errorIdentity, setErrorIdentity] = useState<string | null>(null);
  const eligible = Boolean(
    canonicalIdentity
      && scope
      && (readOnlyDiagnostics || (currentAuthScope === scope && state !== "empty")),
  );

  useEffect(() => {
    ++generation.current;
    setResult(null);
    setErrorIdentity(null);
    setLoading(false);
    return () => {
      ++generation.current;
    };
  }, [currentIdentity]);

  const load = async () => {
    if (!eligible || !scope) return;
    const requestedScope = scope;
    const requestedIdentity = currentIdentity;
    const request = ++generation.current;
    const active = () => currentScope.current === requestedIdentity && generation.current === request;
    setLoading(true);
    setErrorIdentity(null);
    try {
      const rows = await getOutboxDiagnostics(requestedScope);
      if (active()) setResult({ identity: requestedIdentity, rows: safeRows(rows) });
    } catch {
      if (active()) setErrorIdentity(requestedIdentity);
    } finally {
      if (active()) setLoading(false);
    }
  };

  if (!eligible || !scope) return null;
  const rows = result?.identity === currentIdentity ? result.rows : null;
  const displayedRows = rows
    ? [...rows].sort((a, b) =>
      Number(a.errorKind === "pending" && a.retryCount < 5)
      - Number(b.errorKind === "pending" && b.retryCount < 5),
    ).slice(0, 20)
    : [];

  return <View style={styles.container} testID="outbox-diagnostics">
    <Button mode="outlined" loading={loading} disabled={loading} onPress={() => { void load(); }}>
      送信待ちの詳細を確認
    </Button>
    {errorIdentity === currentIdentity ? <Text style={styles.reason}>詳細を取得できませんでした。もう一度確認してください。</Text> : null}
    {rows ? <>
      <Text style={styles.reason}>確認時点の未送信: {rows.length}件</Text>
      {displayedRows.map((row) => <View key={row.opId} style={styles.row}>
        <Text style={styles.title}>{areaLabel(row.tableName)}・{ACTION_LABELS[row.action] ?? row.action}</Text>
        <Text style={styles.reason}>{REASONS[row.errorKind]}（再試行{row.retryCount}回）</Text>
        <Text selectable style={styles.id}>対象ID: {row.entityId}</Text>
        <Text selectable style={styles.id}>操作ID: {row.opId}</Text>
        {row.tableName.startsWith("knowledge_") ? <>
          <Text style={styles.metaText}>docsScopeKey: {row.docsScopeKey ?? "未取得"}</Text>
          <Text style={styles.metaText}>base version: {row.baseUpdatedAt ?? "未取得"}</Text>
          <Text style={styles.metaText}>base snapshot: {row.hasBaseSnapshot ? "あり" : "なし"}</Text>
          <Text style={styles.metaText}>conflict snapshot: {row.hasConflictSnapshot ? "あり" : "なし"}</Text>
        </> : null}
        {!readOnlyDiagnostics ? (
          <OutboxResolution
            row={row}
            authScope={scope}
            resolutionIdentityKey={resolutionIdentityKey ?? undefined}
          />
        ) : null}
      </View>)}
      {rows.length > 20 ? <Text style={styles.reason}>保留理由のある操作を優先して20件を表示しています。</Text> : null}
    </> : null}
  </View>;
}

const styles = StyleSheet.create({
  container: { marginTop: 8, gap: 8 },
  row: { gap: 3, paddingVertical: 8, borderTopWidth: 1, borderTopColor: "#343149" },
  resolution: { gap: 6, marginTop: 4 },
  metadata: { gap: 2 },
  detail: { gap: 6 },
  fields: { gap: 6 },
  field: { borderTopWidth: 1, borderTopColor: "#343149", paddingTop: 6, gap: 3 },
  recordActions: { gap: 6 },
  confirmation: { gap: 8, padding: 10, borderRadius: 8, backgroundColor: "#26243a" },
  confirmActions: { flexDirection: "row", gap: 8, flexWrap: "wrap" },
  nativeButton: { borderWidth: 1, borderColor: "#777294", borderRadius: 6, paddingHorizontal: 12, paddingVertical: 8 },
  nativeButtonText: { color: "#f0efff", fontSize: 13 },
  title: { color: "#f0efff", fontSize: 13 },
  confirmTitle: { color: "#f0efff", fontSize: 14, fontWeight: "600" },
  fieldTitle: { color: "#f0efff", fontSize: 13 },
  bodyText: { color: "#cdd6f4", fontSize: 12 },
  reason: { color: "#cdd6f4", fontSize: 12 },
  success: { color: "#a6e3a1", fontSize: 12 },
  metaText: { color: "#8f8aa8", fontSize: 11 },
  id: { color: "#8f8aa8", fontSize: 10 },
});
