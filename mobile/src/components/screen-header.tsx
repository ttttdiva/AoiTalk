/**
 * 共通スクリーンヘッダー
 *
 * 各タブ画面（Chat / Tasks / Calendar / Files / Docs）の自前ヘッダを統一し、
 * 右上に常設の歯車ボタン（設定 Stack への導線）を加える。
 * 画面固有のアクション（検索・追加・表示範囲切替など）は `right` に渡す。
 */

import React from "react";
import { StyleSheet, View } from "react-native";
import { useRouter } from "expo-router";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { IconButton, Surface, Text } from "react-native-paper";

const TEXT = "#cdd6f4";
const MUTED = "#a6adc8";
const SURFACE = "#1e1e2e";

export type ScreenHeaderProps = {
  title: string;
  subtitle?: string;
  /** 画面固有アクション（検索/追加/表示範囲切替 等）。歯車の左に並ぶ。 */
  right?: React.ReactNode;
  /** 歯車押下時の遷移。既定は /(tabs)/settings。 */
  onSettings?: () => void;
  /** 指定時は左端に戻る矢印を表示（Stack の戻る用）。 */
  onBack?: () => void;
  /** Settings root itself can hide the self-referential settings action. */
  showSettings?: boolean;
};

export function ScreenHeader({
  title,
  subtitle,
  right,
  onSettings,
  onBack,
  showSettings = true,
}: ScreenHeaderProps) {
  const router = useRouter();
  const insets = useSafeAreaInsets();

  const handleSettings = () => {
    if (onSettings) {
      onSettings();
      return;
    }
    router.push("/(tabs)/settings");
  };

  return (
    <Surface
      testID="screen-header"
      style={[
        styles.header,
        {
          paddingTop: insets.top + 12,
          paddingLeft: insets.left + 12,
          paddingRight: insets.right + 12,
        },
      ]}
      elevation={1}
    >
      <View style={styles.row}>
        {onBack ? (
          <IconButton
            icon="arrow-left"
            size={22}
            iconColor={TEXT}
            style={styles.backButton}
            onPress={onBack}
            accessibilityLabel="戻る"
          />
        ) : null}
        <View style={styles.copy}>
          <Text variant="titleLarge" style={styles.title} numberOfLines={1}>
            {title}
          </Text>
          {subtitle ? (
            <Text style={styles.subtitle} numberOfLines={1}>
              {subtitle}
            </Text>
          ) : null}
        </View>
        <View style={styles.actions}>
          {right}
          {showSettings ? (
            <IconButton
              icon="cog-outline"
              size={22}
              iconColor={MUTED}
              style={styles.settingsButton}
              onPress={handleSettings}
              accessibilityLabel="設定"
            />
          ) : null}
        </View>
      </View>
    </Surface>
  );
}

const styles = StyleSheet.create({
  header: {
    paddingLeft: 12,
    paddingRight: 12,
    paddingBottom: 12,
    backgroundColor: SURFACE,
  },
  row: {
    flexDirection: "row",
    alignItems: "center",
    gap: 4,
  },
  backButton: {
    margin: 0,
    marginLeft: -4,
  },
  copy: {
    flex: 1,
    paddingLeft: 4,
  },
  title: {
    color: TEXT,
    fontWeight: "bold",
  },
  subtitle: {
    color: MUTED,
    fontSize: 12,
    marginTop: 2,
  },
  actions: {
    flexDirection: "row",
    alignItems: "center",
    gap: 2,
  },
  settingsButton: {
    margin: 0,
  },
});
