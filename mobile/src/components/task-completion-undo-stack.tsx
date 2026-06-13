import React from "react";
import { StyleSheet, View } from "react-native";
import { Button, Portal, Surface, Text } from "react-native-paper";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { useTaskCompletionUndoStore } from "../stores/task-completion-undo";

export function TaskCompletionUndoStack() {
  const batches = useTaskCompletionUndoStore((state) => state.batches);
  const undoBatch = useTaskCompletionUndoStore((state) => state.undoBatch);
  const insets = useSafeAreaInsets();

  if (!batches.length) return null;

  const visibleBatches = [...batches].slice(-3).reverse();

  return (
    <Portal>
      <View
        pointerEvents="box-none"
        style={[styles.container, { bottom: insets.bottom + 72 }]}
      >
        {visibleBatches.map((batch) => (
          <Surface key={batch.id} style={styles.card} elevation={4}>
            <Text numberOfLines={2} style={styles.message}>
              {batch.message}
            </Text>
            <Button
              compact
              mode="text"
              onPress={() => void undoBatch(batch.id)}
            >
              Undo
            </Button>
          </Surface>
        ))}
      </View>
    </Portal>
  );
}

const styles = StyleSheet.create({
  container: {
    position: "absolute",
    left: 12,
    right: 12,
    gap: 8,
    alignItems: "flex-start",
  },
  card: {
    minWidth: 220,
    maxWidth: 320,
    paddingHorizontal: 12,
    paddingVertical: 10,
    borderRadius: 14,
    backgroundColor: "#1e1e2e",
    borderWidth: StyleSheet.hairlineWidth,
    borderColor: "#45475a",
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    gap: 10,
  },
  message: {
    flex: 1,
    color: "#cdd6f4",
    fontSize: 13,
  },
});
