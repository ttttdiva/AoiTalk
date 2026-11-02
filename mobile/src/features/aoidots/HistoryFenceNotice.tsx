import React, { useState } from "react";
import { Button, Surface, Text } from "react-native-paper";
import { Pressable, ScrollView, useWindowDimensions, View } from "react-native";
import type { AoiDotsHistoryFenceReview } from "./types";

type Props = {
  state: "checking" | "clear" | "blocked" | "unavailable";
  reviews: AoiDotsHistoryFenceReview[];
  error: string | null;
  applyingWorkId: string | null;
  onApply: (review: AoiDotsHistoryFenceReview) => void;
  onRefresh: () => void;
};

function statusLabel(value: string | null): string {
  switch (value) {
    case "failed": return "失敗";
    case "succeeded":
    case "completed": return "完了";
    case "running":
    case "claimed": return "実行中";
    case "waiting":
    case "pending": return "待機中";
    case "blocked": return "停止中";
    case "cancelled": return "キャンセル";
    default: return "確認できません";
  }
}

function childWorkSummary(states: string[]): string {
  if (states.length === 0) return "子Work 0件";
  const counts = new Map<string, number>();
  for (const value of states) counts.set(value, (counts.get(value) ?? 0) + 1);
  return `子Work ${states.length}件（${[...counts.entries()]
    .map(([value, count]) => `${statusLabel(value)} ${count}件`)
    .join("・")}）`;
}

export function HistoryFenceNotice({
  state,
  reviews,
  error,
  applyingWorkId,
  onApply,
  onRefresh,
}: Props) {
  const [expanded, setExpanded] = useState(false);
  const { height } = useWindowDimensions();
  const detailsMaxHeight = Number.isFinite(height) && height > 0
    ? Math.min(280, Math.floor(height * 0.35))
    : 280;
  if (state === "clear") return null;
  const checking = state === "checking";
  return (
    <Surface
      testID="aoidots-history-fence"
      accessibilityLiveRegion="polite"
      style={{
        marginHorizontal: 12,
        marginBottom: 8,
        paddingHorizontal: 12,
        paddingVertical: 8,
        gap: 8,
        borderColor: "#f9e2af",
        borderWidth: 1,
        backgroundColor: "#302b20",
      }}
      elevation={0}
    >
      <View style={{ flexDirection: "row", alignItems: "center", gap: 8 }}>
        <View style={{ flex: 1, minWidth: 0 }}>
          <Text accessibilityRole="alert" numberOfLines={2} style={{ color: "#f9e2af", fontWeight: "700" }}>
            {checking
              ? "相談の継続可否を確認しています…"
              : "前回の実行結果を確認できないため、この相談は停止中"}
          </Text>
          <Text style={{ color: "#cdd6f4", fontSize: 12 }} numberOfLines={1}>
            確認項目 {reviews.length}件
          </Text>
        </View>
        <Pressable
          testID="aoidots-history-fence-toggle"
          accessibilityRole="button"
          accessibilityLabel={expanded ? "相談の確認詳細を折り畳む" : "相談の確認詳細を展開"}
          accessibilityState={{ expanded }}
          onPress={() => setExpanded((value) => !value)}
          hitSlop={7}
          style={{ minHeight: 36, justifyContent: "center", paddingHorizontal: 8, borderWidth: 1, borderColor: "#f9e2af", borderRadius: 6 }}
        >
          <Text style={{ color: "#f9e2af", fontSize: 12 }}>{expanded ? "折り畳む" : "詳細"}</Text>
        </Pressable>
      </View>
      {error && !expanded ? <Text accessibilityRole="alert" numberOfLines={1} style={{ color: "#f38ba8", fontSize: 12 }}>{error}</Text> : null}
      {expanded ? (
        <ScrollView
          testID="aoidots-history-fence-details"
          style={{ maxHeight: detailsMaxHeight }}
          contentContainerStyle={{ gap: 8 }}
          nestedScrollEnabled
          keyboardShouldPersistTaps="handled"
        >
          {!checking ? (
            <Text style={{ color: "#cdd6f4", fontSize: 12, lineHeight: 18 }}>
              結果は不明のまま記録されます。前回の依頼は再送しません。確認内容を読んで明示操作した後に続行できます。
            </Text>
          ) : null}
          {error ? <Text accessibilityRole="alert" style={{ color: "#f38ba8", fontSize: 12 }}>{error}</Text> : null}
          {reviews.map((review) => (
            <View key={review.workItemId} style={{ borderTopWidth: 1, borderColor: "#45475a", paddingTop: 7, gap: 4 }}>
              <Text style={{ color: "#cdd6f4", fontSize: 12, fontWeight: "600" }}>Work {review.workItemId}</Text>
              <Text style={{ color: "#cdd6f4", fontSize: 12 }}>Run状態: {statusLabel(review.runStatus)}</Text>
              <Text style={{ color: "#cdd6f4", fontSize: 12 }}>{childWorkSummary(review.childWorkStates)}</Text>
              <Text style={{ color: "#a6adc8", fontSize: 12 }}>結果不明 · providerへの再送なし · 利用量も不明のままです。</Text>
              {review.status === "dry_run_ok" && review.stateDigest ? (
                <Button
                  compact
                  mode="outlined"
                  textColor="#f9e2af"
                  disabled={applyingWorkId !== null}
                  onPress={() => onApply(review)}
                  accessibilityLabel="結果不明のまま履歴を区切り、相談を続ける"
                >
                  {applyingWorkId === review.workItemId ? "確認を適用中…" : "結果不明のまま履歴を区切り、相談を続ける"}
                </Button>
              ) : (
                <Text style={{ color: "#f38ba8", fontSize: 12 }}>
                  {review.errorMessage ?? "このWorkは自動で解除できません。追加確認が必要です。"}
                </Text>
              )}
            </View>
          ))}
          <Button compact mode="text" textColor="#89b4fa" disabled={checking} onPress={onRefresh}>
            状態を再確認
          </Button>
        </ScrollView>
      ) : null}
    </Surface>
  );
}
