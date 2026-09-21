import React, {
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import {
  KeyboardAvoidingView,
  Modal,
  Platform,
  ScrollView,
  StyleSheet,
  View,
} from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import {
  ActivityIndicator,
  Button,
  Divider,
  Text,
  TextInput,
} from "react-native-paper";
import type { ClipIngestResult } from "../../lib/docs-api";
import {
  resumeClipIngestOperation,
  runClipIngest,
  type ClipIngestOutcome,
} from "../../lib/clip-ingest";
import { getConfiguredApiServerFingerprint } from "../../lib/api-client";
import { docsRepo } from "../../repositories/docs";
import {
  listRecoverableClipIngestOperations,
  type ClipIngestStrandReason,
  type PendingClipIngestRow,
} from "../../repositories/pending-clip-ingest";
import { runSync } from "../../sync/engine";

type IngestResult = ClipIngestResult;

const ACTION_LABELS: Record<IngestResult["action"], string> = {
  create: "新規作成",
  append: "既存ノードへ追記",
  duplicate_skip: "重複のため保存をスキップ",
};

// サーバー未到達・オフライン時に端末だけで完結した場合の補足。
// オフラインではURL本文の取得もクラウドLLMの利用もできないため、
// 内容が未確認・未整理のまま保存されることがある（未確認事項に表示される）。
const LOCAL_MODE_NOTE =
  "AoiTalkサーバーへ接続できなかったため、端末だけで取り込みました。接続時に自動で同期されます。";

function strandMessage(reason: ClipIngestStrandReason): string {
  switch (reason) {
    case "auth_changed":
      return "取り込み開始時の認証スコープを現在利用できません。元のアカウントへ戻すと同じoperation keyで再開できます。";
    case "server_changed":
      return "この取り込みは別のAoiTalkサーバーに固定されています。元のAPIサーバーへ戻すと同じoperation keyで再開できます。";
    case "server_unknown":
      return "この旧journalには元のAoiTalkサーバー識別子がありません。安全のため自動再送しません。入力を復元して、新しい操作として明示的に取り込んでください。";
    default:
      return "取り込みjournalの識別情報が不完全なため自動再送できません。入力を確認して新しい操作として取り込んでください。";
  }
}

function recoveryStatusLabel(status: string): string {
  switch (status) {
    case "remote_succeeded":
      return "サーバー保存済み";
    case "remote_pending":
      return "サーバー処理中";
    case "remote_unknown":
      return "サーバー応答確認待ち";
    case "remote_ready":
      return "送信状態確認待ち";
    case "local_pending":
      return "端末処理未完了";
    default:
      return "未送信";
  }
}

function errorMessage(error: unknown): string {
  const raw = error instanceof Error ? error.message : String(error || "");
  const jsonStart = raw.indexOf("{");
  if (jsonStart >= 0) {
    try {
      const parsed = JSON.parse(raw.slice(jsonStart)) as { detail?: unknown };
      if (typeof parsed.detail === "string") return parsed.detail;
    } catch {
      // JSONでなければ元のエラーを表示する。
    }
  }
  return raw || "クリップ取り込みに失敗しました";
}

export function ClipIngestDialog({
  visible,
  onDismiss,
  onOpenNode,
  targetNodeId,
  sessionId,
  projectId,
}: {
  visible: boolean;
  onDismiss: () => void;
  onOpenNode: (nodeId: string) => void;
  /** Optional controller-provided scope context for durable jobs. */
  targetNodeId?: string | null;
  sessionId?: string | null;
  projectId?: string | null;
}) {
  const [source, setSource] = useState("");
  const [status, setStatus] = useState<
    "idle" | "running" | "success" | "queued" | "stranded" | "failure"
  >("idle");
  const [result, setResult] = useState<IngestResult | null>(null);
  const [error, setError] = useState("");
  const [syncWarning, setSyncWarning] = useState("");
  const [localNote, setLocalNote] = useState("");
  const [queuedRemoteAttempted, setQueuedRemoteAttempted] = useState(false);
  const [queuedLegacyShape, setQueuedLegacyShape] = useState(false);
  const [queuedOperationId, setQueuedOperationId] = useState<string | null>(null);
  const [strandedReason, setStrandedReason] =
    useState<ClipIngestStrandReason | null>(null);
  const [recoveryRows, setRecoveryRows] =
    useState<PendingClipIngestRow[]>([]);
  const [currentServerFingerprint, setCurrentServerFingerprint] =
    useState("");
  const [recoveryBusyId, setRecoveryBusyId] = useState<string | null>(null);
  const [openingNode, setOpeningNode] = useState(false);
  const inFlightRef = useRef(false);

  const hydrateRecovery = useCallback(async () => {
    try {
      const [rows, serverFingerprint] = await Promise.all([
        listRecoverableClipIngestOperations(),
        getConfiguredApiServerFingerprint(),
      ]);
      setRecoveryRows(rows);
      setCurrentServerFingerprint(serverFingerprint);
    } catch {
      // Recovery discovery must never make the normal Clip input unusable.
      setRecoveryRows([]);
      setCurrentServerFingerprint("");
    }
  }, []);

  useEffect(() => {
    if (!visible) return;
    setSource("");
    setStatus("idle");
    setResult(null);
    setError("");
    setSyncWarning("");
    setLocalNote("");
    setQueuedRemoteAttempted(false);
    setQueuedLegacyShape(false);
    setQueuedOperationId(null);
    setStrandedReason(null);
    setRecoveryRows([]);
    setCurrentServerFingerprint("");
    setRecoveryBusyId(null);
    setOpeningNode(false);
    inFlightRef.current = false;
    void hydrateRecovery();
  }, [visible, hydrateRecovery]);

  const dismiss = () => {
    if (inFlightRef.current) return;
    onDismiss();
  };

  const applyOutcome = (outcome: ClipIngestOutcome) => {
    setQueuedOperationId(null);
    setStrandedReason(null);
    setQueuedRemoteAttempted(false);
    setQueuedLegacyShape(false);
    setLocalNote("");
    setSyncWarning("");

    if (outcome.mode === "queued") {
      setQueuedOperationId(outcome.pendingId);
      setQueuedRemoteAttempted(outcome.remoteAttempted === true);
      setQueuedLegacyShape(
        !Object.prototype.hasOwnProperty.call(outcome, "remoteAttempted"),
      );
      setStatus("queued");
      return;
    }
    if (outcome.mode === "stranded") {
      setQueuedOperationId(outcome.pendingId);
      setStrandedReason(outcome.reason);
      setStatus("stranded");
      return;
    }
    setResult(outcome.result);
    setSyncWarning(outcome.mode === "server" ? outcome.syncWarning : "");
    setLocalNote(outcome.mode === "local" ? LOCAL_MODE_NOTE : "");
    setStatus("success");
  };

  const submit = async () => {
    if (inFlightRef.current || !source.trim()) return;
    inFlightRef.current = true;
    setStatus("running");
    setResult(null);
    setError("");
    setSyncWarning("");
    setLocalNote("");
    setQueuedRemoteAttempted(false);
    setQueuedLegacyShape(false);
    try {
      const hasContext = targetNodeId != null || sessionId != null || projectId != null;
      const outcome = hasContext
        ? await runClipIngest(source, { targetNodeId, sessionId, projectId })
        : await runClipIngest(source);
      applyOutcome(outcome);
    } catch (requestError) {
      setError(errorMessage(requestError));
      setStatus("failure");
    } finally {
      inFlightRef.current = false;
    }
  };

  const resumeOperation = async (operationId: string) => {
    if (inFlightRef.current) return;
    inFlightRef.current = true;
    setRecoveryBusyId(operationId);
    setError("");
    try {
      const outcome = await resumeClipIngestOperation(operationId);
      applyOutcome(outcome);
      await hydrateRecovery();
    } catch (requestError) {
      setError(errorMessage(requestError));
      setStatus("failure");
    } finally {
      setRecoveryBusyId(null);
      inFlightRef.current = false;
    }
  };

  const restoreRecoveryInput = (row: PendingClipIngestRow) => {
    if (inFlightRef.current) return;
    setSource(row.source);
    setStatus("idle");
    setResult(null);
    setError("");
    setSyncWarning("");
    setLocalNote("");
    setQueuedOperationId(null);
    setStrandedReason(null);
    setRecoveryRows((rows) => rows.filter((candidate) => candidate.id !== row.id));
  };

  const syncAndOpenResult = async () => {
    if (!result || inFlightRef.current) return;
    inFlightRef.current = true;
    setOpeningNode(true);
    setError("");
    try {
      await runSync();
      const node = await docsRepo.getNode(result.open_node_id);
      if (!node) {
        throw new Error(
          "同期後も保存ノードを確認できませんでした。接続状態を確認して再度お試しください。",
        );
      }
      onOpenNode(node.id);
    } catch (requestError) {
      setError(errorMessage(requestError));
      setStatus("failure");
    } finally {
      setOpeningNode(false);
      inFlightRef.current = false;
    }
  };

  return (
    <Modal
      visible={visible}
      animationType="slide"
      presentationStyle="fullScreen"
      onRequestClose={dismiss}
    >
      <SafeAreaView style={styles.safeArea}>
        <KeyboardAvoidingView
          style={styles.container}
          behavior={Platform.OS === "ios" ? "padding" : undefined}
        >
          <View style={styles.header}>
            <Button
              textColor="#a6adc8"
              disabled={
                status === "running"
                || recoveryBusyId !== null
                || openingNode
              }
              onPress={dismiss}
            >
              閉じる
            </Button>
            <Text variant="titleMedium" style={styles.headerTitle}>クリップ取り込み</Text>
            <View style={styles.headerSpacer} />
          </View>
          <Divider style={styles.divider} />

          <ScrollView
            contentContainerStyle={styles.content}
            keyboardShouldPersistTaps="handled"
          >
            <Text style={styles.description}>
              URL、文章、または両方を貼り付けると、設定済みの取り込み先へ整理して保存します。
            </Text>
            <TextInput
              accessibilityLabel="取り込むURLまたは文章"
              value={source}
              onChangeText={setSource}
              editable={
                status !== "running"
                && recoveryBusyId === null
                && !openingNode
              }
              mode="outlined"
              multiline
              numberOfLines={10}
              textAlignVertical="top"
              placeholder={"https://example.com/article\n補足したい文章やメモ"}
              style={styles.input}
            />

            {recoveryRows.length > 0 ? (
              <View style={styles.recoveryBox}>
                <Text style={styles.recoveryTitle}>
                  未完了・復旧可能な取り込み
                </Text>
                <Text style={styles.helperText}>
                  通常の「取り込む」は常に新しい操作です。クラッシュ後の処理は、ここから既存operation keyを再開してください。
                </Text>
                {recoveryRows.map((row) => {
                  const serverMismatch =
                    !row.serverFingerprint
                    || row.serverFingerprint !== currentServerFingerprint;
                  return (
                    <View key={row.id} style={styles.recoveryRow}>
                      <Text style={styles.resultText} numberOfLines={2}>
                        {row.source}
                      </Text>
                      <Text style={styles.recoveryMeta}>
                        {recoveryStatusLabel(row.status)}
                      </Text>
                      {serverMismatch ? (
                        <Text style={styles.helperText}>
                          {strandMessage(
                            row.serverFingerprint
                              ? "server_changed"
                              : "server_unknown",
                          )}
                        </Text>
                      ) : null}
                      <Button
                        compact
                        mode="outlined"
                        loading={recoveryBusyId === row.id}
                        disabled={
                          recoveryBusyId !== null
                          || openingNode
                        }
                        onPress={() => {
                          if (serverMismatch) {
                            restoreRecoveryInput(row);
                          } else {
                            void resumeOperation(row.id);
                          }
                        }}
                      >
                        {serverMismatch
                          ? "入力を復元"
                          : row.status === "remote_succeeded"
                            ? "結果を復元"
                            : "状態を確認"}
                      </Button>
                    </View>
                  );
                })}
              </View>
            ) : null}

            {status === "running" ? (
              <View accessibilityRole="progressbar" style={styles.statusBox}>
                <ActivityIndicator size="small" color="#c084fc" />
                <Text style={styles.statusText}>URLの取得と保存先の判定を行っています…</Text>
              </View>
            ) : null}

            {status === "failure" ? (
              <View accessibilityRole="alert" style={[styles.statusBox, styles.errorBox]}>
                <Text style={styles.errorTitle}>取り込みに失敗しました</Text>
                <Text style={styles.errorText}>{error}</Text>
                <Text style={styles.helperText}>
                  保存済みの可能性があります。再実行する前にDocsを同期して結果を確認してください。
                </Text>
              </View>
            ) : null}

            {status === "queued" ? (
              <View accessibilityRole="summary" style={[styles.statusBox, styles.successBox]}>
                <Text style={styles.successTitle}>取り込みを保留しました</Text>
                <Text style={styles.resultText}>
                  {queuedRemoteAttempted
                    ? "サーバーへの取り込み要求は送信済み、または応答確認中です。"
                    : "サーバーへ未送信の入力を端末に保存しました。"}
                </Text>
                <Text style={styles.helperText}>
                  {queuedLegacyShape
                    ? "接続できたときに自動で取り込みます。アプリを閉じても保留は消えません。"
                    : queuedRemoteAttempted
                    ? "同じoperation keyで状態を自動確認します。再実行する必要はありません。アプリを閉じても状態は消えません。"
                    : "接続できたときに同じoperation keyで自動取り込みします。アプリを閉じても保留は消えません。"}
                </Text>
              </View>
            ) : null}

            {status === "stranded" && strandedReason ? (
              <View
                accessibilityRole="alert"
                style={[styles.statusBox, styles.errorBox]}
              >
                <Text style={styles.errorTitle}>
                  取り込みを安全に再開できません
                </Text>
                <Text style={styles.errorText}>
                  {strandMessage(strandedReason)}
                </Text>
                <Text style={styles.helperText}>
                  このjournalは削除・別サーバー送信されず、そのまま端末に保持されます。
                </Text>
              </View>
            ) : null}

            {status === "success" && result ? (
              <View accessibilityRole="summary" style={[styles.statusBox, styles.successBox]}>
                <Text style={styles.successTitle}>取り込みが完了しました</Text>
                {localNote ? (
                  <View style={styles.syncWarningBox}>
                    <Text style={styles.helperText}>{localNote}</Text>
                  </View>
                ) : null}
                <Text style={styles.resultText}>保存先: {result.target_label}</Text>
                <Text style={styles.resultText}>処理: {ACTION_LABELS[result.action]}</Text>
                <Text style={styles.resultText}>ノード: {result.open_node_title}</Text>
                {result.used_urls.length > 0 ? (
                  <>
                    <Text style={styles.resultHeading}>保存根拠URL</Text>
                    {result.used_urls.map((url) => (
                      <Text key={url} style={styles.urlText}>• {url}</Text>
                    ))}
                  </>
                ) : null}
                {result.unconfirmed.length > 0 ? (
                  <>
                    <Text style={styles.resultHeading}>未確認事項</Text>
                    {result.unconfirmed.map((item) => (
                      <Text key={item} style={styles.resultText}>• {item}</Text>
                    ))}
                  </>
                ) : null}
                {syncWarning ? (
                  <View accessibilityRole="alert" style={styles.syncWarningBox}>
                    <Text style={styles.helperText}>{syncWarning}</Text>
                    <Text style={styles.helperText}>
                      同期後に保存ノードを開けます
                    </Text>
                  </View>
                ) : null}
              </View>
            ) : null}
          </ScrollView>

          <View style={styles.actions}>
            {status === "queued" ? (
              queuedOperationId && !queuedLegacyShape ? (
                <Button
                  mode="contained"
                  icon="sync"
                  buttonColor="#7c3aed"
                  loading={recoveryBusyId === queuedOperationId}
                  onPress={() => void resumeOperation(queuedOperationId)}
                >
                  {queuedRemoteAttempted ? "状態を確認" : "同じ操作を再開"}
                </Button>
              ) : (
                <Button
                  mode="contained"
                  icon="tray-full"
                  buttonColor="#7c3aed"
                  onPress={dismiss}
                >
                  閉じる
                </Button>
              )
            ) : status === "stranded" ? (
              <Button
                mode="contained"
                icon="shield-alert-outline"
                buttonColor="#7c3aed"
                onPress={dismiss}
              >
                閉じる
              </Button>
            ) : status === "success" && result ? (
              syncWarning ? (
                <Button
                  mode="contained"
                  icon="cloud-sync-outline"
                  buttonColor="#7c3aed"
                  loading={openingNode}
                  disabled={openingNode}
                  onPress={() => void syncAndOpenResult()}
                >
                  同期して保存ノードを開く
                </Button>
              ) : (
                <Button
                  mode="contained"
                  icon="file-document-outline"
                  buttonColor="#7c3aed"
                  onPress={() => onOpenNode(result.open_node_id)}
                >
                  保存したノードを開く
                </Button>
              )
            ) : (
              <Button
                mode="contained"
                icon="tray-arrow-down"
                buttonColor="#7c3aed"
                loading={status === "running"}
                disabled={!source.trim() || status === "running"}
                onPress={() => void submit()}
              >
                {status === "failure" ? "再実行" : status === "running" ? "取り込み中…" : "取り込む"}
              </Button>
            )}
          </View>
        </KeyboardAvoidingView>
      </SafeAreaView>
    </Modal>
  );
}

const styles = StyleSheet.create({
  safeArea: { flex: 1, backgroundColor: "#11111b" },
  container: { flex: 1, backgroundColor: "#11111b" },
  header: {
    minHeight: 56,
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    paddingHorizontal: 8,
  },
  headerTitle: { color: "#cdd6f4", fontWeight: "700" },
  headerSpacer: { width: 72 },
  divider: { backgroundColor: "#313244" },
  content: { padding: 16, gap: 16 },
  description: { color: "#a6adc8", lineHeight: 20 },
  input: { minHeight: 220, backgroundColor: "#181825" },
  statusBox: {
    flexDirection: "row",
    flexWrap: "wrap",
    alignItems: "center",
    gap: 10,
    borderWidth: 1,
    borderColor: "#45475a",
    borderRadius: 10,
    padding: 14,
    backgroundColor: "#181825",
  },
  statusText: { color: "#cdd6f4", flex: 1 },
  errorBox: { borderColor: "#f38ba8", backgroundColor: "#2a1720" },
  errorTitle: { color: "#f38ba8", fontWeight: "700", width: "100%" },
  errorText: { color: "#f5c2e7", width: "100%" },
  helperText: { color: "#a6adc8", fontSize: 12, width: "100%" },
  successBox: { borderColor: "#a6e3a1", backgroundColor: "#17251c" },
  successTitle: { color: "#a6e3a1", fontWeight: "700", width: "100%" },
  resultText: { color: "#cdd6f4", width: "100%" },
  resultHeading: { color: "#a6adc8", fontSize: 12, fontWeight: "700", marginTop: 4, width: "100%" },
  urlText: { color: "#89b4fa", fontSize: 12, width: "100%" },
  syncWarningBox: {
    width: "100%",
    borderWidth: 1,
    borderColor: "#f9e2af",
    borderRadius: 8,
    padding: 10,
    backgroundColor: "#302a1a",
  },
  recoveryBox: {
    gap: 10,
    borderWidth: 1,
    borderColor: "#45475a",
    borderRadius: 10,
    padding: 14,
    backgroundColor: "#181825",
  },
  recoveryTitle: {
    color: "#cdd6f4",
    fontWeight: "700",
  },
  recoveryRow: {
    gap: 6,
    borderTopWidth: 1,
    borderTopColor: "#313244",
    paddingTop: 10,
  },
  recoveryMeta: {
    color: "#a6adc8",
    fontSize: 12,
  },
  actions: {
    borderTopWidth: 1,
    borderTopColor: "#313244",
    padding: 16,
    backgroundColor: "#181825",
  },
});
