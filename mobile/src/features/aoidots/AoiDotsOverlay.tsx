import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { BackHandler, Pressable, ScrollView, StyleSheet, View } from "react-native";
import { ActivityIndicator, Button, Portal, Surface, Text, TextInput } from "react-native-paper";
import { useRouter } from "expo-router";
import { useAuth } from "../../contexts/AuthContext";
import { useProject } from "../../contexts/ProjectContext";
import { aoidotsApi } from "../../lib/aoidots-api";
import { readCurrentAoiDotsRootPointer } from "./root-pointer";
import type { AoiDotsInput, AoiDotsProfile, AoiDotsWorkItem } from "./types";
import { ConversationSurface } from "../conversation/ConversationSurface";
import { AoiDotsContext } from "./context";
export { AoiDotsTrigger, useAoiDots } from "./context";

export function isAoiDotsStaleError(error: unknown): boolean {
  return Boolean(
    error &&
      typeof error === "object" &&
      "status" in error &&
      Number((error as { status?: unknown }).status) === 409,
  );
}

export function isAoiDotsConversationActive(
  isOpen: boolean,
  bootstrapLoading: boolean,
  bootstrapError: string | null,
): boolean {
  return isOpen && !bootstrapLoading && !bootstrapError;
}

export function buildAoiDotsAnswer(
  input: AoiDotsInput,
  freeText: string,
  selectedChoices: string[],
): string {
  const normalizedText = input.allow_free_text ? freeText.trim() : "";
  const offeredChoices = new Set(input.choices);
  const normalizedChoices = selectedChoices
    .filter((choice) => offeredChoices.has(choice))
    .slice(0, input.allow_multiple ? input.choices.length : 1);
  if (input.choices.length === 0) return normalizedText;
  if (normalizedChoices.length === 0 && !normalizedText) return "";
  return JSON.stringify({
    choices: normalizedChoices,
    ...(normalizedText ? { free_text: normalizedText } : {}),
  });
}

export function canSubmitAoiDotsAnswer(
  input: AoiDotsInput,
  freeText: string,
  selectedChoices: string[],
): boolean {
  const answer = buildAoiDotsAnswer(input, freeText, selectedChoices);
  if (!answer) return false;
  if (input.choices.length === 0) return input.allow_free_text;
  return (
    selectedChoices.some((choice) => input.choices.includes(choice)) ||
    (input.allow_free_text && freeText.trim().length > 0)
  );
}

function humanWorkStatus(item: AoiDotsWorkItem): string {
  const canonical = item.human_status?.trim();
  if (canonical) return canonical;
  if (item.phase === "receiving_result") return "成果を受信中";
  switch (item.job_status) {
    case "queued":
      return "準備中";
    case "running":
      return "実行中";
    case "waiting":
    case "waiting_for_input":
      return "判断待ち";
    case "blocked":
      return "要確認";
    case "succeeded":
    case "completed":
      return "完了";
    case "failed":
      return "要確認";
    case "cancelled":
    case "expired":
      return "停止";
    default:
      return "状態を確認中";
  }
}

function fallbackProfile(pointer: Awaited<ReturnType<typeof readCurrentAoiDotsRootPointer>>): AoiDotsProfile | null {
  if (!pointer) return null;
  return {
    root_id: pointer.root_id,
    session_id: pointer.session_id,
    agent_id: pointer.agent_id,
    agent_name: "AoiDots",
    state: "reconnect_pending",
    pending_input_count: 0,
  };
}

export function AoiDotsWorkRail({
  items,
  onAnswer,
  onControl,
  onOpenWork,
  loading,
  error,
  onRetry,
}: {
  items: AoiDotsWorkItem[];
  onAnswer: (item: AoiDotsInput, answer: string) => Promise<void>;
  onControl: (item: AoiDotsWorkItem, action: "stop" | "resume" | "rework", instruction?: string) => Promise<void>;
  onOpenWork: (workId: string) => void;
  loading: boolean;
  error: string | null;
  onRetry: () => void;
}) {
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [selectedChoices, setSelectedChoices] = useState<Record<string, string[]>>({});
  const [reworkDrafts, setReworkDrafts] = useState<Record<string, string>>({});
  const [reworkOpen, setReworkOpen] = useState<Record<string, boolean>>({});
  const [reworkPending, setReworkPending] = useState<Record<string, boolean>>({});
  const [actionError, setActionError] = useState<string | null>(null);
  const runAction = useCallback((action: () => Promise<void>) => {
    setActionError(null);
    void action().catch((error) => {
      setActionError(error instanceof Error ? error.message : "AoiDots操作に失敗しました。");
    });
  }, []);
  const toggleChoice = useCallback((inputId: string, choice: string, allowMultiple: boolean) => {
    setSelectedChoices((current) => {
      const selected = current[inputId] ?? [];
      return {
        ...current,
        [inputId]: allowMultiple
          ? selected.includes(choice)
            ? selected.filter((item) => item !== choice)
            : [...selected, choice]
          : [choice],
      };
    });
  }, []);
  const answerPayload = useCallback((input: AoiDotsInput): string => {
    return buildAoiDotsAnswer(
      input,
      answers[input.id] ?? "",
      selectedChoices[input.id] ?? [],
    );
  }, [answers, selectedChoices]);
  const pendingInputs = items.flatMap((item) =>
    item.allowed_actions.includes("answer")
      ? (item.pending_inputs ?? []).filter((input) => input.state === "pending")
      : [],
  );
  return (
    <Surface testID="aoidots-work-rail" style={styles.workRail} elevation={0}>
      <View style={styles.workRailHeading}>
        <Text style={styles.workRailTitle}>仕事・確認待ち</Text>
        <Text style={styles.workRailCount}>{loading ? "更新中…" : `${items.length}件`}</Text>
      </View>
      {error ? (
        <View style={styles.workErrorRow}>
          <Text style={styles.workError} accessibilityRole="alert">{error}</Text>
          <Button compact textColor="#89b4fa" onPress={onRetry}>再試行</Button>
        </View>
      ) : null}
      {actionError ? <Text style={styles.workError} accessibilityRole="alert">{actionError}</Text> : null}
      <ScrollView horizontal showsHorizontalScrollIndicator={false} contentContainerStyle={styles.workRailList}>
        {items.length === 0 ? <Text style={styles.muted}>実行中の仕事はありません。</Text> : items.map((item) => (
          <View key={item.dialogue_id} style={styles.workCard}>
            <Text style={styles.workTitle}>{item.title || item.label || item.dialogue_id}</Text>
            <Text style={styles.workStatus}>{humanWorkStatus(item)}</Text>
            <Text style={styles.workAgents}>
              {item.root_agent_name || "相談窓口"} → {item.worker_agent_name || "担当Agent"}
            </Text>
            {item.safe_error ? <Text style={styles.workSummary} numberOfLines={2}>{item.safe_error}</Text> : null}
            {item.artifacts?.length ? (
              <View style={styles.workArtifacts}>
                <Text style={styles.workArtifactsTitle}>成果 {item.artifacts.length}件</Text>
                {item.artifacts.slice(0, 3).map((artifact, index) => (
                  <Text
                    key={artifact.id || artifact.artifact_ref || `${item.dialogue_id}:artifact:${index}`}
                    style={styles.workArtifact}
                    numberOfLines={1}
                  >
                    {artifact.phase ? `${artifact.phase}: ` : ""}{artifact.artifact_ref || artifact.id || "成果"}
                  </Text>
                ))}
                {item.artifacts.length > 3 ? (
                  <Text style={styles.workArtifact}>ほか {item.artifacts.length - 3}件</Text>
                ) : null}
              </View>
            ) : null}
            {item.dialogue_id ? (
              <View style={styles.workControlRow}>
                {item.refs.work_item_id ? (
                  <Button compact textColor="#89b4fa" onPress={() => onOpenWork(item.refs.work_item_id!)}>Work</Button>
                ) : null}
                {item.allowed_actions.includes("resume") ? <Button compact textColor="#89b4fa" onPress={() => runAction(() => onControl(item, "resume"))}>再開</Button> : null}
                {item.allowed_actions.includes("stop") ? <Button compact textColor="#f38ba8" onPress={() => runAction(() => onControl(item, "stop"))}>停止</Button> : null}
                {item.allowed_actions.includes("rework") ? (
                  <Button
                    compact
                    textColor="#f9e2af"
                    onPress={() => setReworkOpen((current) => ({ ...current, [item.dialogue_id]: true }))}
                    disabled={Boolean(reworkPending[item.dialogue_id])}
                  >
                    修正
                  </Button>
                ) : null}
              </View>
            ) : null}
            {item.dialogue_id && reworkOpen[item.dialogue_id] ? (
              <View style={styles.reworkEditor}>
                <TextInput
                  dense
                  mode="outlined"
                  value={reworkDrafts[item.dialogue_id] ?? ""}
                  onChangeText={(value) => setReworkDrafts((current) => ({ ...current, [item.dialogue_id]: value }))}
                  placeholder="修正指示（任意）"
                  maxLength={30_000}
                  multiline
                  numberOfLines={3}
                  disabled={Boolean(reworkPending[item.dialogue_id])}
                  style={styles.reworkInput}
                />
                <View style={styles.workControlRow}>
                  <Button
                    compact
                    onPress={() => setReworkOpen((current) => ({ ...current, [item.dialogue_id]: false }))}
                    disabled={Boolean(reworkPending[item.dialogue_id])}
                  >
                    キャンセル
                  </Button>
                  <Button
                    compact
                    mode="contained"
                    buttonColor="#7c3aed"
                    onPress={() => {
                      setReworkOpen((current) => ({ ...current, [item.dialogue_id]: false }));
                      setReworkPending((current) => ({ ...current, [item.dialogue_id]: true }));
                      runAction(() => onControl(item, "rework", reworkDrafts[item.dialogue_id] ?? "").finally(() => {
                        setReworkPending((current) => ({ ...current, [item.dialogue_id]: false }));
                      }));
                    }}
                    disabled={Boolean(reworkPending[item.dialogue_id])}
                  >
                    送信
                  </Button>
                </View>
              </View>
            ) : null}
          </View>
        ))}
      </ScrollView>
      {pendingInputs.length > 0 ? (
        <View style={styles.inputRail}>
          <Text style={styles.inputRailTitle}>AoiDotsからの確認</Text>
          {pendingInputs.slice(0, 3).map((input) => (
            <View key={input.id} style={styles.inputRow}>
              <Text style={styles.inputPrompt} numberOfLines={2}>{input.question || input.question_message_id || "確認が必要です"}</Text>
              {input.choices.map((choice) => (
                <Button
                  key={choice}
                  compact
                  mode={selectedChoices[input.id]?.includes(choice) ? "contained" : "outlined"}
                  onPress={() => toggleChoice(input.id, choice, input.allow_multiple)}
                >
                  {choice}
                </Button>
              ))}
              {input.allow_free_text ? (
                <>
                  <TextInput
                    dense
                    mode="outlined"
                    value={answers[input.id] ?? ""}
                    onChangeText={(value) => setAnswers((current) => ({ ...current, [input.id]: value }))}
                    placeholder="回答"
                    style={styles.inputField}
                  />
                  {input.choices.length === 0 ? (
                    <Button
                      compact
                      mode="contained"
                      buttonColor="#7c3aed"
                      disabled={
                        !canSubmitAoiDotsAnswer(
                          input,
                          answers[input.id] ?? "",
                          selectedChoices[input.id] ?? [],
                        ) || !input.dialogue_id
                      }
                      onPress={() => runAction(() => onAnswer(input, answerPayload(input)))}
                    >
                      回答
                    </Button>
                  ) : null}
                </>
              ) : null}
              {input.choices.length > 0 ? (
                <Button
                  compact
                  mode="contained"
                  buttonColor="#7c3aed"
                  disabled={
                    !canSubmitAoiDotsAnswer(
                      input,
                      answers[input.id] ?? "",
                      selectedChoices[input.id] ?? [],
                    ) || !input.dialogue_id
                  }
                  onPress={() => runAction(() => onAnswer(input, answerPayload(input)))}
                >
                  回答
                </Button>
              ) : null}
            </View>
          ))}
        </View>
      ) : null}
    </Surface>
  );
}

export function AoiDotsProvider({ children }: { children: React.ReactNode }) {
  const { isAuthenticated, user } = useAuth();
  const { selectedProjectId } = useProject();
  const router = useRouter();
  const [isOpen, setIsOpen] = useState(false);
  const [bootstrapLoading, setBootstrapLoading] = useState(false);
  const [bootstrapError, setBootstrapError] = useState<string | null>(null);
  const [profile, setProfile] = useState<AoiDotsProfile | null>(null);
  const [workItems, setWorkItems] = useState<AoiDotsWorkItem[]>([]);
  const [workLoading, setWorkLoading] = useState(false);
  const [workError, setWorkError] = useState<string | null>(null);
  const [projectHint, setProjectHint] = useState<string | null>(selectedProjectId);
  const [focusEpoch, setFocusEpoch] = useState(0);
  const bootstrapGenerationRef = useRef(0);
  const workRefreshGenerationRef = useRef(0);

  useEffect(() => {
    setProfile(null);
    setWorkItems([]);
    setWorkLoading(false);
    setWorkError(null);
    setBootstrapError(null);
    setIsOpen(false);
    setProjectHint(selectedProjectId);
    setFocusEpoch((epoch) => epoch + 1);
    bootstrapGenerationRef.current += 1;
    workRefreshGenerationRef.current += 1;
  }, [isAuthenticated, user?.user_id]);

  const bootstrap = useCallback(async (hint: string | null) => {
    if (!isAuthenticated) {
      setBootstrapError("AoiDotsを使うにはログインしてください。");
      return;
    }
    const generation = ++bootstrapGenerationRef.current;
    workRefreshGenerationRef.current += 1;
    setBootstrapLoading(true);
    setBootstrapError(null);
    setWorkError(null);
    setWorkItems([]);
    setWorkLoading(false);
    try {
      const next = await aoidotsApi.bootstrap(hint);
      if (generation !== bootstrapGenerationRef.current) return;
      setProfile(next);
      const workRequestGeneration = ++workRefreshGenerationRef.current;
      setWorkLoading(true);
      setWorkError(null);
      try {
        const work = await aoidotsApi.listWork({ rootSessionId: next.session_id, limit: 50 });
        if (
          generation !== bootstrapGenerationRef.current ||
          workRequestGeneration !== workRefreshGenerationRef.current
        ) return;
        setWorkItems(work.items);
      } catch (workFailure) {
        if (
          generation !== bootstrapGenerationRef.current ||
          workRequestGeneration !== workRefreshGenerationRef.current
        ) return;
        setWorkError(workFailure instanceof Error ? workFailure.message : "仕事一覧を取得できませんでした。");
      } finally {
        if (
          generation === bootstrapGenerationRef.current &&
          workRequestGeneration === workRefreshGenerationRef.current
        ) {
          setWorkLoading(false);
        }
      }
    } catch (error) {
      if (generation !== bootstrapGenerationRef.current) return;
      // A cached pointer is only a reconnect hint. It never authorizes a
      // session by itself, and all sends still go through the server API.
      const pointer = await readCurrentAoiDotsRootPointer().catch(() => null);
      if (generation !== bootstrapGenerationRef.current) return;
      setProfile(fallbackProfile(pointer));
      setWorkItems([]);
      setWorkError("AoiDotsの最新Workを確認できません。再接続後に再試行してください。");
      setBootstrapError(
        error instanceof Error ? error.message : "AoiDotsを準備できませんでした。",
      );
    } finally {
      if (generation === bootstrapGenerationRef.current) setBootstrapLoading(false);
    }
  }, [isAuthenticated]);

  const open = useCallback(() => {
    const hint = selectedProjectId ?? null;
    setProjectHint(hint);
    setIsOpen(true);
    setFocusEpoch((epoch) => epoch + 1);
    void bootstrap(hint);
  }, [bootstrap, selectedProjectId]);

  const close = useCallback(() => {
    // Closing only releases UI focus. The server generation/job and pending
    // inputs remain owned by the ordinary conversation session.
    setIsOpen(false);
  }, []);

  useEffect(() => {
    if (!isOpen) return;
    const subscription = BackHandler.addEventListener("hardwareBackPress", () => {
      close();
      return true;
    });
    return () => subscription.remove();
  }, [close, isOpen]);

  const context = useMemo(() => ({ isOpen, open, close }), [close, isOpen, open]);
  const openWork = useCallback((reference: { kind: "work"; id: string }) => {
    close();
    router.push({ pathname: "/operations/work/[workId]", params: { workId: reference.id } });
  }, [close, router]);
  const openWorkId = useCallback((workId: string) => {
    openWork({ kind: "work", id: workId });
  }, [openWork]);
  const refreshWork = useCallback(async () => {
    if (!profile) return;
    const requestGeneration = ++workRefreshGenerationRef.current;
    const requestedSessionId = profile.session_id;
    setWorkLoading(true);
    setWorkError(null);
    try {
      const work = await aoidotsApi.listWork({ rootSessionId: requestedSessionId, limit: 50 });
      if (
        requestGeneration !== workRefreshGenerationRef.current ||
        profile.session_id !== requestedSessionId
      ) {
        return;
      }
      setWorkItems(work.items);
    } catch (error) {
      if (requestGeneration !== workRefreshGenerationRef.current) return;
      setWorkError(error instanceof Error ? error.message : "仕事一覧を取得できませんでした。");
    } finally {
      if (requestGeneration === workRefreshGenerationRef.current) {
        setWorkLoading(false);
      }
    }
  }, [profile]);
  const answerInput = useCallback(async (input: AoiDotsInput, answer: string) => {
    if (!input.dialogue_id) return;
    try {
      await aoidotsApi.answerInput(input.dialogue_id, input.id, answer, {
        expectedInputRevision: input.expected_input_revision,
        expectedControlRevision: input.expected_control_revision,
      });
      await refreshWork();
    } catch (error) {
      if (isAoiDotsStaleError(error)) await refreshWork();
      throw error;
    }
  }, [refreshWork]);
  const controlWork = useCallback(async (item: AoiDotsWorkItem, action: "stop" | "resume" | "rework", instruction?: string) => {
    if (!item.dialogue_id) return;
    try {
      await aoidotsApi.control(item.dialogue_id, action, item.control_revision, instruction);
      await refreshWork();
    } catch (error) {
      if (isAoiDotsStaleError(error)) await refreshWork();
      throw error;
    }
  }, [refreshWork]);

  useEffect(() => {
    if (!isOpen || !profile) return;
    const interval = setInterval(() => {
      void refreshWork();
    }, 15_000);
    return () => {
      clearInterval(interval);
    };
  }, [isOpen, profile, refreshWork]);

  const conversationActive = isAoiDotsConversationActive(
    isOpen,
    bootstrapLoading,
    bootstrapError,
  );

  return (
    <AoiDotsContext.Provider value={context}>
      {children}
      <Portal>
        <View
          testID="aoidots-overlay"
          pointerEvents={isOpen ? "auto" : "none"}
          style={[styles.overlay, !isOpen && styles.overlayHidden]}
        >
          <Surface style={styles.modal} elevation={5}>
            {bootstrapLoading && !profile ? (
              <View style={styles.loading}>
                <ActivityIndicator color="#c084fc" />
                <Text style={styles.muted}>AoiDotsを準備しています…</Text>
                <Pressable accessibilityRole="button" accessibilityLabel="AoiDotsを閉じる" style={styles.close} onPress={close}>
                  <Text style={styles.closeText}>閉じる</Text>
                </Pressable>
              </View>
            ) : profile ? (
              <View style={styles.surfaceWrap}>
                <View style={styles.surfaceBody}>
                  <ConversationSurface
                    sessionId={profile.session_id}
                    isAuthenticated={isAuthenticated}
                    userId={user?.user_id}
                    userRole={user?.role}
                    selectedProjectId={selectedProjectId}
                    contextProjectHintId={projectHint}
                    runtimeFocus={{ isFocused: conversationActive, focusEpoch }}
                    active={conversationActive}
                    embedded
                    embeddedHeaderAccessory={(
                      <AoiDotsWorkRail
                        items={workItems}
                        onAnswer={answerInput}
                        onControl={controlWork}
                        onOpenWork={openWorkId}
                        loading={workLoading}
                        error={workError}
                        onRetry={() => void refreshWork()}
                      />
                    )}
                    onClose={close}
                    onOpenWork={openWork}
                    onProjectHintChange={setProjectHint}
                  />
                </View>
              </View>
            ) : (
              <View style={styles.loading}>
                <Text style={styles.title}>AoiDots</Text>
                <Text style={styles.muted}>{bootstrapError ?? "AoiDotsを開けませんでした。"}</Text>
                <Pressable accessibilityRole="button" accessibilityLabel="AoiDotsを再試行" style={styles.retry} onPress={open}>
                  <Text style={styles.retryText}>再試行</Text>
                </Pressable>
                <Pressable accessibilityRole="button" accessibilityLabel="AoiDotsを閉じる" style={styles.close} onPress={close}>
                  <Text style={styles.closeText}>閉じる</Text>
                </Pressable>
              </View>
            )}
            {bootstrapError && profile ? (
              <Text style={styles.error} accessibilityRole="alert">{bootstrapError}</Text>
            ) : null}
          </Surface>
        </View>
      </Portal>
    </AoiDotsContext.Provider>
  );
}

const styles = StyleSheet.create({
  overlay: {
    ...StyleSheet.absoluteFillObject,
    zIndex: 100,
    backgroundColor: "rgba(10, 10, 20, 0.68)",
    paddingTop: 32,
    paddingBottom: 24,
    paddingHorizontal: 8,
  },
  overlayHidden: { display: "none" },
  modal: { flex: 1, overflow: "hidden", borderRadius: 16, backgroundColor: "#11111b" },
  surfaceWrap: { flex: 1, minHeight: 0 },
  surfaceBody: { flex: 1, minHeight: 0 },
  workRail: { backgroundColor: "#181825", paddingHorizontal: 10, paddingVertical: 6, borderBottomColor: "#313244", borderBottomWidth: StyleSheet.hairlineWidth },
  workRailHeading: { flexDirection: "row", alignItems: "center", gap: 8 },
  workRailTitle: { color: "#cdd6f4", fontSize: 12, fontWeight: "700" },
  workRailCount: { color: "#a6adc8", fontSize: 11 },
  workErrorRow: { flexDirection: "row", alignItems: "center", gap: 6 },
  workError: { flex: 1, color: "#f38ba8", fontSize: 11 },
  workRailList: { gap: 8, paddingVertical: 5 },
  workCard: { width: 170, borderColor: "#45475a", borderWidth: 1, borderRadius: 8, padding: 7, backgroundColor: "#1e1e2e" },
  workTitle: { color: "#cdd6f4", fontSize: 12, fontWeight: "700" },
  workStatus: { color: "#89b4fa", fontSize: 10, marginTop: 2 },
  workAgents: { color: "#a6adc8", fontSize: 10, marginTop: 2 },
  workSummary: { color: "#a6adc8", fontSize: 10, marginTop: 4 },
  workArtifacts: { marginTop: 4, gap: 1 },
  workArtifactsTitle: { color: "#f9e2af", fontSize: 10, fontWeight: "700" },
  workArtifact: { color: "#a6adc8", fontSize: 9 },
  inputRail: { borderTopColor: "#313244", borderTopWidth: StyleSheet.hairlineWidth, paddingTop: 5 },
  inputRailTitle: { color: "#f9e2af", fontSize: 11, fontWeight: "700", marginBottom: 3 },
  inputRow: { flexDirection: "row", alignItems: "center", gap: 5 },
  inputPrompt: { color: "#cdd6f4", flex: 1, fontSize: 11 },
  inputField: { width: 110, height: 38, backgroundColor: "#1e1e2e" },
  workControlRow: { flexDirection: "row", justifyContent: "flex-end", gap: 0, marginTop: 3 },
  reworkEditor: { marginTop: 5, gap: 3 },
  reworkInput: { minHeight: 66, backgroundColor: "#1e1e2e" },
  loading: { flex: 1, alignItems: "center", justifyContent: "center", gap: 14, padding: 24 },
  title: { color: "#cdd6f4", fontSize: 20, fontWeight: "700" },
  muted: { color: "#a6adc8", textAlign: "center" },
  error: { color: "#f9e2af", backgroundColor: "#453a2b", paddingHorizontal: 12, paddingVertical: 8 },
  retry: { backgroundColor: "#7c3aed", borderRadius: 8, paddingHorizontal: 16, paddingVertical: 10 },
  retryText: { color: "#ffffff", fontWeight: "700" },
  close: { borderColor: "#45475a", borderWidth: 1, borderRadius: 8, paddingHorizontal: 16, paddingVertical: 10 },
  closeText: { color: "#cdd6f4", fontWeight: "700" },
});
