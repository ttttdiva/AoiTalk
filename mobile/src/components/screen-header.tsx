/**
 * 共通スクリーンヘッダー
 *
 * 各タブ画面（Chat / Tasks / Calendar / Files / Docs）の自前ヘッダを統一し、
 * 操作行に常設の歯車ボタン（設定 Stack への導線）を加える。
 * 画面固有のアクション（検索・追加・表示範囲切替など）は `right` に渡す。
 */

import React, { useCallback, useContext, useEffect, useState } from "react";
import {
  Alert,
  BackHandler,
  Pressable,
  ScrollView,
  StyleSheet,
  useWindowDimensions,
  View,
} from "react-native";
import { useFocusEffect, useRouter } from "expo-router";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { IconButton, Portal, Surface, Text } from "react-native-paper";
import { useAppSidebar } from "./app-sidebar-context";
import { SyncStatusIndicator } from "./sync-status-indicator";
import { AoiDotsContext, AoiDotsTrigger } from "../features/aoidots/context";

const TEXT = "#cdd6f4";
const MUTED = "#a6adc8";
const SURFACE = "#1e1e2e";
const NARROW_HEADER_WIDTH = 420;
const TITLE_MINIMUM_FONT_SCALE = 0.75;
const SAFE_ERROR_NAME_PATTERN = /^[A-Za-z][A-Za-z0-9_.-]{0,63}$/;

export type ScreenHeaderInteractionEvent =
  | {
      phase: "press-in" | "press" | "route-request";
      at: number;
      target: "settings" | "custom";
    }
  | {
      phase: "route-error";
      at: number;
      target: "settings" | "custom";
      errorName: string;
    };

function safeErrorName(error: unknown): string {
  const name = error instanceof Error ? error.name : "UnknownError";
  return SAFE_ERROR_NAME_PATTERN.test(name) ? name : "Error";
}

export type ScreenHeaderProps = {
  title: string;
  subtitle?: string;
  /** 画面固有アクション（検索/追加/表示範囲切替 等）。歯車の左に並ぶ。 */
  right?: React.ReactNode;
  /** 歯車押下時の遷移。既定は /(tabs)/settings。 */
  onSettings?: () => void;
  /** Optional bounded diagnostic events for settings interaction delivery. */
  onSettingsInteraction?: (event: ScreenHeaderInteractionEvent) => void;
  /** 指定時は左端に戻る矢印を表示（Stack の戻る用）。 */
  onBack?: () => void;
  /** Root画面ではSidebarを開く明示ボタンを表示する。 */
  showMenu?: boolean;
  /** Settings root itself can hide the self-referential settings action. */
  showSettings?: boolean;
  /** 本文中心の画面ではsafe areaを保ったまま上下の余白を抑える。 */
  compact?: boolean;
};

export function ScreenHeader({
  title,
  subtitle,
  right,
  onSettings,
  onSettingsInteraction,
  onBack,
  showMenu = true,
  showSettings = true,
  compact = false,
}: ScreenHeaderProps) {
  const router = useRouter();
  const insets = useSafeAreaInsets();
  const sidebar = useAppSidebar();
  const aoiDots = useContext(AoiDotsContext);
  const { width: windowWidth, height: windowHeight } = useWindowDimensions();
  const [overflowOpen, setOverflowOpen] = useState(false);
  const overflowActions = Boolean(right)
    && Number.isFinite(windowWidth)
    && windowWidth <= NARROW_HEADER_WIDTH;
  const overflowPanelTop = insets.top + 56;
  const overflowPanelMaxHeight = Number.isFinite(windowHeight) && windowHeight > 0
    ? Math.max(96, Math.floor(windowHeight - overflowPanelTop - insets.bottom - 8))
    : 320;
  const overflowActionsMaxHeight = Math.max(44, overflowPanelMaxHeight - 68);
  const settingsTarget = onSettings ? "custom" : "settings";

  useFocusEffect(
    useCallback(() => {
      return () => setOverflowOpen(false);
    }, []),
  );

  useEffect(() => {
    if (!overflowOpen) return undefined;
    const subscription = BackHandler.addEventListener("hardwareBackPress", () => {
      if (aoiDots?.isOpen) {
        aoiDots.close();
        return true;
      }
      setOverflowOpen(false);
      return true;
    });
    return () => subscription.remove();
  }, [aoiDots, overflowOpen]);

  const reportSettingsInteraction = (event: ScreenHeaderInteractionEvent) => {
    try {
      onSettingsInteraction?.(event);
    } catch {
      // Diagnostics must never change the navigation or custom-handler result.
    }
  };

  const handleSettingsPressIn = () => {
    reportSettingsInteraction({
      phase: "press-in",
      at: Date.now(),
      target: settingsTarget,
    });
  };

  const handleSettings = () => {
    reportSettingsInteraction({
      phase: "press",
      at: Date.now(),
      target: settingsTarget,
    });

    try {
      if (onSettings) {
        reportSettingsInteraction({
          phase: "route-request",
          at: Date.now(),
          target: "custom",
        });
        onSettings();
        return;
      }

      reportSettingsInteraction({
        phase: "route-request",
        at: Date.now(),
        target: "settings",
      });
      router.push("/(tabs)/settings");
    } catch (error) {
      reportSettingsInteraction({
        phase: "route-error",
        at: Date.now(),
        target: settingsTarget,
        errorName: safeErrorName(error),
      });
      throw error;
    }
  };

  const handleTitlePress = () => {
    Alert.alert(
      "タイトル",
      [title, subtitle].filter((value): value is string => Boolean(value)).join("\n\n"),
    );
  };

  return (
    <Surface
      testID="screen-header"
      style={[
        styles.header,
        {
          paddingTop: insets.top + (compact ? 4 : 12),
          paddingBottom: compact ? 4 : 12,
          paddingLeft: insets.left + 12,
          paddingRight: insets.right + 12,
        },
      ]}
      elevation={1}
    >
      <View testID="screen-header-row" style={styles.row}>
        {onBack ? (
          <IconButton
            icon="arrow-left"
            size={22}
            iconColor={TEXT}
            style={styles.backButton}
            onPress={onBack}
            accessibilityLabel="戻る"
          />
        ) : showMenu && sidebar ? (
          <IconButton
            icon="menu"
            size={22}
            iconColor={TEXT}
            style={styles.backButton}
            onPress={sidebar.openSidebar}
            accessibilityLabel="メニューを開く"
          />
        ) : null}
        <View style={styles.copy}>
          <View style={styles.titleLine}>
            <Pressable
              testID="screen-header-title"
              accessibilityRole="button"
              accessibilityLabel={`${title}（全文を表示）`}
              accessibilityHint="タイトルの全文を表示します"
              onPress={handleTitlePress}
              style={styles.titlePressable}
            >
              <Text
                variant="titleLarge"
                style={styles.title}
                numberOfLines={1}
                adjustsFontSizeToFit
                minimumFontScale={TITLE_MINIMUM_FONT_SCALE}
                ellipsizeMode="tail"
              >
                {title}
              </Text>
            </Pressable>
          </View>
          {subtitle ? (
            <Text style={styles.subtitle} numberOfLines={1} ellipsizeMode="tail">
              {subtitle}
            </Text>
          ) : null}
        </View>
        <SyncStatusIndicator />
        <View testID="screen-header-actions" style={styles.actions}>
          {overflowActions ? (
            <IconButton
              icon="dots-horizontal"
              size={22}
              iconColor={MUTED}
              style={styles.moreButton}
              onPress={() => setOverflowOpen(true)}
              accessibilityLabel="その他の操作"
              accessibilityHint="この画面の追加操作を開きます"
            />
          ) : (
            right
          )}
          <AoiDotsTrigger compact />
          {showSettings ? (
            <IconButton
              icon="cog-outline"
              size={22}
              iconColor={MUTED}
              style={styles.settingsButton}
              onPressIn={handleSettingsPressIn}
              onPress={handleSettings}
              accessibilityLabel="設定"
            />
          ) : null}
        </View>
      </View>
      {overflowActions ? (
        <Portal>
          <View
            testID="screen-header-overflow-root"
            style={[
              styles.overflowPortalRoot,
              {
                display: overflowOpen ? "flex" : "none",
                width: windowWidth,
                height: windowHeight,
              },
            ]}
          >
            <Portal.Host>
              <View
                testID="screen-header-overflow"
                pointerEvents={overflowOpen ? "box-none" : "none"}
                accessibilityElementsHidden={!overflowOpen}
                importantForAccessibility={overflowOpen ? "yes" : "no-hide-descendants"}
                style={styles.overflowOverlay}
              >
                <Pressable
                  style={styles.overflowBackdrop}
                  onPress={() => setOverflowOpen(false)}
                  accessibilityLabel="その他の操作を閉じる"
                />
                <View
                  testID="screen-header-overflow-panel"
                  accessibilityViewIsModal={overflowOpen}
                  style={[
                    styles.overflowPanel,
                    {
                      top: overflowPanelTop,
                      right: insets.right + 12,
                      maxHeight: overflowPanelMaxHeight,
                    },
                  ]}
                >
                  <View style={styles.overflowHeader}>
                    <Text style={styles.overflowTitle}>その他の操作</Text>
                    <IconButton
                      icon="close"
                      size={22}
                      iconColor={MUTED}
                      style={styles.closeButton}
                      onPress={() => setOverflowOpen(false)}
                      accessibilityLabel="その他の操作を閉じる"
                    />
                  </View>
                  <ScrollView
                    testID="screen-header-overflow-scroll"
                    style={[styles.overflowActionsScroll, { maxHeight: overflowActionsMaxHeight }]}
                    contentContainerStyle={styles.overflowActionsContent}
                    keyboardShouldPersistTaps="handled"
                    showsVerticalScrollIndicator
                  >
                    <View testID="screen-header-overflow-actions" style={styles.overflowActions}>
                      {right}
                    </View>
                  </ScrollView>
                </View>
              </View>
            </Portal.Host>
          </View>
        </Portal>
      ) : null}
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
    minWidth: 0,
  },
  backButton: {
    width: 44,
    height: 44,
    minWidth: 44,
    minHeight: 44,
    margin: 0,
  },
  copy: {
    flex: 1,
    minWidth: 0,
    paddingLeft: 4,
  },
  titleLine: {
    minWidth: 0,
    flexDirection: "row",
    alignItems: "center",
  },
  titlePressable: {
    minWidth: 0,
    flexShrink: 1,
    minHeight: 44,
    justifyContent: "center",
  },
  title: {
    color: TEXT,
    fontWeight: "bold",
    flexShrink: 1,
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
    flexShrink: 0,
    minWidth: 0,
  },
  moreButton: {
    width: 44,
    height: 44,
    minWidth: 44,
    minHeight: 44,
    margin: 0,
  },
  settingsButton: {
    width: 44,
    height: 44,
    minWidth: 44,
    minHeight: 44,
    margin: 0,
  },
  overflowPortalRoot: {
    ...StyleSheet.absoluteFillObject,
    zIndex: 10,
    elevation: 10,
    overflow: "visible",
  },
  overflowOverlay: {
    ...StyleSheet.absoluteFillObject,
    overflow: "visible",
  },
  overflowBackdrop: {
    ...StyleSheet.absoluteFillObject,
    backgroundColor: "rgba(0, 0, 0, 0.35)",
  },
  overflowPanel: {
    position: "absolute",
    minWidth: 220,
    maxWidth: "92%",
    borderRadius: 12,
    padding: 8,
    backgroundColor: SURFACE,
    elevation: 6,
  },
  overflowHeader: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    minHeight: 44,
  },
  overflowTitle: {
    color: TEXT,
    fontWeight: "600",
    paddingLeft: 8,
  },
  closeButton: {
    width: 44,
    height: 44,
    minWidth: 44,
    minHeight: 44,
    margin: 0,
  },
  overflowActions: {
    flexDirection: "row",
    alignItems: "center",
    flexWrap: "wrap",
    gap: 2,
    minHeight: 44,
  },
  overflowActionsScroll: {
    flexGrow: 0,
  },
  overflowActionsContent: {
    flexGrow: 0,
  },
});
