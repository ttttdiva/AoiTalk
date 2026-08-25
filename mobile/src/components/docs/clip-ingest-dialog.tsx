import React, { useEffect, useRef, useState } from "react";
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
import { runClipIngest } from "../../lib/clip-ingest";

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
}: {
  visible: boolean;
  onDismiss: () => void;
  onOpenNode: (nodeId: string) => void;
}) {
  const [source, setSource] = useState("");
  const [status, setStatus] = useState<
    "idle" | "running" | "success" | "queued" | "failure"
  >("idle");
  const [result, setResult] = useState<IngestResult | null>(null);
  const [error, setError] = useState("");
  const [syncWarning, setSyncWarning] = useState("");
  const [localNote, setLocalNote] = useState("");
  const inFlightRef = useRef(false);

  useEffect(() => {
    if (!visible) return;
    setSource("");
    setStatus("idle");
    setResult(null);
    setError("");
    setSyncWarning("");
    setLocalNote("");
    inFlightRef.current = false;
  }, [visible]);

  const dismiss = () => {
    if (inFlightRef.current) return;
    onDismiss();
  };

  const submit = async () => {
    if (inFlightRef.current || !source.trim()) return;
    inFlightRef.current = true;
    setStatus("running");
    setResult(null);
    setError("");
    setSyncWarning("");
    setLocalNote("");
    try {
      const outcome = await runClipIngest(source);
      if (outcome.mode === "queued") {
        setStatus("queued");
        return;
      }
      setResult(outcome.result);
      setSyncWarning(outcome.mode === "server" ? outcome.syncWarning : "");
      setLocalNote(outcome.mode === "local" ? LOCAL_MODE_NOTE : "");
      setStatus("success");
    } catch (requestError) {
      setError(errorMessage(requestError));
      setStatus("failure");
    } finally {
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
            <Button textColor="#a6adc8" disabled={status === "running"} onPress={dismiss}>
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
              editable={status !== "running"}
              mode="outlined"
              multiline
              numberOfLines={10}
              textAlignVertical="top"
              placeholder={"https://example.com/article\n補足したい文章やメモ"}
              style={styles.input}
            />

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
                  AoiTalkサーバーへ接続できなかったため、入力を端末に保存しました。
                </Text>
                <Text style={styles.helperText}>
                  接続できたときに自動で取り込みます。アプリを閉じても保留は消えません。
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
                  </View>
                ) : null}
              </View>
            ) : null}
          </ScrollView>

          <View style={styles.actions}>
            {status === "queued" ? (
              <Button mode="contained" icon="tray-full" buttonColor="#7c3aed" onPress={dismiss}>
                閉じる
              </Button>
            ) : status === "success" && result ? (
              syncWarning ? (
                <Button mode="contained" icon="cloud-sync-outline" disabled>
                  同期後に保存ノードを開けます
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
  actions: {
    borderTopWidth: 1,
    borderTopColor: "#313244",
    padding: 16,
    backgroundColor: "#181825",
  },
});
