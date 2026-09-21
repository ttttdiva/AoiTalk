import React, { type ReactNode } from "react";
import {
  KeyboardAvoidingView,
  Modal,
  Platform,
  ScrollView,
  StyleSheet,
  View,
} from "react-native";
import { IconButton, Text, useTheme } from "react-native-paper";
import { SafeAreaProvider, SafeAreaView } from "react-native-safe-area-context";

type FullScreenModalShellProps = {
  visible: boolean;
  title: string;
  onClose: () => void;
  closeDisabled?: boolean;
  testID: string;
  children: ReactNode;
};

/** A modal page, not a router screen: fixed safe-area header and scrolling body. */
export function FullScreenModalShell({
  visible,
  title,
  onClose,
  closeDisabled = false,
  testID,
  children,
}: FullScreenModalShellProps) {
  const theme = useTheme();
  if (!visible) return null;
  const close = () => {
    if (!closeDisabled) onClose();
  };

  return (
    <Modal
      visible
      animationType="slide"
      presentationStyle="fullScreen"
      onRequestClose={close}
      testID={testID}
    >
      <SafeAreaProvider style={{ backgroundColor: theme.colors.background }}>
        <SafeAreaView
          edges={["top", "right", "bottom", "left"]}
          style={styles.fill}
          testID={`${testID}-safe-area`}
        >
          <View style={styles.header} testID={`${testID}-header`}>
            <Text variant="titleLarge" accessibilityRole="header" style={styles.title}>
              {title}
            </Text>
            <IconButton
              icon="close"
              accessibilityLabel="閉じる"
              testID={`${testID}-close`}
              disabled={closeDisabled}
              onPress={close}
              style={styles.close}
            />
          </View>
          <KeyboardAvoidingView
            style={styles.fill}
            behavior={Platform.OS === "ios" ? "padding" : "height"}
          >
            <ScrollView
              style={styles.fill}
              contentContainerStyle={styles.content}
              keyboardShouldPersistTaps="handled"
              keyboardDismissMode="on-drag"
              testID={`${testID}-content`}
            >
              {children}
            </ScrollView>
          </KeyboardAvoidingView>
        </SafeAreaView>
      </SafeAreaProvider>
    </Modal>
  );
}

const styles = StyleSheet.create({
  fill: { flex: 1 },
  header: {
    minHeight: 56,
    flexDirection: "row",
    alignItems: "center",
    paddingLeft: 16,
    paddingRight: 4,
  },
  title: { flex: 1, minWidth: 0, paddingVertical: 8 },
  close: { width: 48, height: 48, margin: 0, flexShrink: 0 },
  content: { padding: 16, gap: 12, flexGrow: 1 },
});
