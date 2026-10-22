import React, { useCallback, useState, type ReactNode } from "react";
import { ScrollView, StyleSheet, View, type LayoutChangeEvent } from "react-native";

const CONTROLS_FRACTION = 0.5;

export type FilesWorkspaceLayoutProps = {
  controls: ReactNode;
  children: ReactNode;
  testID?: string;
};

/**
 * Keeps the file controls reachable without allowing them to consume the
 * list/editor viewport. The controls and body are siblings so a vertical
 * FlatList is never nested in this ScrollView.
 */
export function FilesWorkspaceLayout({ controls, children, testID = "files-workspace-layout" }: FilesWorkspaceLayoutProps) {
  const [availableHeight, setAvailableHeight] = useState(0);
  const onLayout = useCallback((event: LayoutChangeEvent) => {
    const nextHeight = Math.max(0, Math.round(event.nativeEvent.layout.height));
    setAvailableHeight((previous) => (previous === nextHeight ? previous : nextHeight));
  }, []);
  const controlsMaxHeight = availableHeight > 0
    ? Math.max(1, Math.floor(availableHeight * CONTROLS_FRACTION))
    : "50%";

  return (
    <View style={styles.container} onLayout={onLayout} testID={testID}>
      <ScrollView
        style={[styles.controlsScroll, { maxHeight: controlsMaxHeight }]}
        contentContainerStyle={styles.controlsContent}
        keyboardShouldPersistTaps="handled"
        testID={`${testID}-controls`}
      >
        {controls}
      </ScrollView>
      <View style={styles.body} testID={`${testID}-body`}>
        {children}
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, minHeight: 0 },
  controlsScroll: { flexGrow: 0, flexShrink: 1 },
  controlsContent: { flexGrow: 0 },
  body: { flex: 1, minHeight: 0 },
});
