import React from "react";
import {
  ActivityIndicator,
  ScrollView,
  StyleProp,
  StyleSheet,
  View,
  ViewStyle,
} from "react-native";
import { Surface, Text } from "react-native-paper";

const BACKGROUND = "#11111b";
const MUTED = "#a6adc8";
const ERROR = "#f38ba8";

export type ScreenShellProps = {
  children: React.ReactNode;
  /** Shared screen background and optional scroll container. */
  scroll?: boolean;
  contentContainerStyle?: StyleProp<ViewStyle>;
  style?: StyleProp<ViewStyle>;
  header?: React.ReactNode;
  bottomActionBar?: React.ReactNode;
};

/**
 * Common screen frame. Headers stay outside the scroll viewport while content
 * and an optional bottom action bar keep a single, predictable flex layout.
 */
export function ScreenShell({
  children,
  scroll = false,
  contentContainerStyle,
  style,
  header,
  bottomActionBar,
}: ScreenShellProps) {
  const content = scroll ? (
    <ScrollView
      style={styles.content}
      contentContainerStyle={contentContainerStyle}
      keyboardShouldPersistTaps="handled"
    >
      {children}
    </ScrollView>
  ) : (
    <View style={[styles.content, contentContainerStyle]}>{children}</View>
  );

  return (
    <View style={[styles.screen, style]}>
      {header}
      {content}
      {bottomActionBar}
    </View>
  );
}

export type LoadingStateProps = {
  label?: string;
  testID?: string;
};

export function LoadingState({
  label = "読み込み中…",
  testID = "loading-state",
}: LoadingStateProps) {
  return (
    <View testID={testID} accessibilityRole="progressbar" style={styles.state}>
      <ActivityIndicator color="#7c3aed" />
      <Text style={styles.stateText}>{label}</Text>
    </View>
  );
}

export type ErrorStateProps = {
  message?: string;
  action?: React.ReactNode;
  testID?: string;
};

export function ErrorState({
  message = "読み込みに失敗しました。",
  action,
  testID = "error-state",
}: ErrorStateProps) {
  return (
    <View testID={testID} style={styles.state}>
      <Text style={styles.errorText}>{message}</Text>
      {action}
    </View>
  );
}

export type EmptyStateProps = {
  message: string;
  action?: React.ReactNode;
  testID?: string;
};

export function EmptyState({
  message,
  action,
  testID = "empty-state",
}: EmptyStateProps) {
  return (
    <View testID={testID} style={styles.state}>
      <Text style={styles.emptyText}>{message}</Text>
      {action}
    </View>
  );
}

export function BottomActionBar({ children }: { children: React.ReactNode }) {
  return <Surface style={styles.bottomActionBar}>{children}</Surface>;
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: BACKGROUND },
  content: { flex: 1 },
  state: {
    flex: 1,
    alignItems: "center",
    justifyContent: "center",
    padding: 24,
    gap: 10,
  },
  stateText: { color: MUTED, textAlign: "center" },
  emptyText: { color: MUTED, textAlign: "center" },
  errorText: { color: ERROR, textAlign: "center" },
  bottomActionBar: {
    paddingHorizontal: 16,
    paddingTop: 10,
    paddingBottom: 12,
    backgroundColor: "#1e1e2e",
    borderTopWidth: StyleSheet.hairlineWidth,
    borderTopColor: "#313244",
  },
});
