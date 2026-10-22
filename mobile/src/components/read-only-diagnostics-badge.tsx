import React from "react";
import { StyleSheet, Text, View } from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { isMobileReadOnlyDiagnosticsEnabled } from "../lib/read-only-diagnostics";

/**
 * QA読み取り診断ビルドの識別表示。画面の高さを消費せず、ステータスバー直下の
 * 余白にだけ重ねる（操作はすり抜ける）。safe-areaは各画面のヘッダーが一度だけ
 * 確保するため、ここでは縦領域を確保しない。
 */
export function ReadOnlyDiagnosticsBadge() {
  const insets = useSafeAreaInsets();
  if (!isMobileReadOnlyDiagnosticsEnabled()) return null;
  return (
    <View
      testID="read-only-diagnostics-banner"
      pointerEvents="none"
      accessible
      accessibilityLabel="QA読み取り診断モード: 同期停止・未送信データ保持"
      style={[styles.wrap, { top: insets.top + 1 }]}
    >
      {/* 文字拡大に追従するとヘッダーのタイトルや操作と重なるため固定サイズにする。全文はaccessibilityLabelで読み上げる。 */}
      <Text allowFontScaling={false} style={styles.text}>QA診断・同期停止</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  wrap: {
    position: "absolute",
    left: 0,
    right: 0,
    alignItems: "center",
    zIndex: 1000,
    elevation: 1000,
  },
  text: {
    backgroundColor: "#7c2d12",
    color: "#fff7ed",
    fontSize: 9,
    lineHeight: 12,
    fontWeight: "600",
    paddingHorizontal: 8,
    borderRadius: 6,
    overflow: "hidden",
  },
});
