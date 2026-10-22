import React, { createContext, useContext } from "react";
import { Pressable, StyleSheet } from "react-native";
import { Text } from "react-native-paper";

export type AoiDotsContextValue = {
  isOpen: boolean;
  open: () => void;
  close: () => void;
};

export const AoiDotsContext = createContext<AoiDotsContextValue | null>(null);

export function useAoiDots(): AoiDotsContextValue {
  const context = useContext(AoiDotsContext);
  if (!context) throw new Error("useAoiDots must be used within AoiDotsProvider");
  return context;
}

export function AoiDotsTrigger({ compact = false }: { compact?: boolean }) {
  // Header unit tests and pre-bootstrap host shells may render a header
  // outside the root provider. The production root always mounts AoiDotsProvider
  // before any screen, so the trigger is simply omitted in those shells.
  const context = useContext(AoiDotsContext);
  if (!context) return null;
  return (
    <Pressable
      testID="aoidots-trigger"
      accessibilityRole="button"
      accessibilityLabel="AoiDotsを開く"
      onPress={context.open}
      style={({ pressed }) => [
        styles.trigger,
        compact && styles.triggerCompact,
        pressed && styles.triggerPressed,
      ]}
    >
      <Text
        accessible={false}
        allowFontScaling={false}
        style={styles.triggerGlyph}
      >
        ✦
      </Text>
      {!compact ? <Text style={styles.triggerText}>AoiDots</Text> : null}
    </Pressable>
  );
}

const styles = StyleSheet.create({
  trigger: {
    minWidth: 44,
    minHeight: 44,
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "center",
    gap: 5,
    borderRadius: 16,
    paddingHorizontal: 9,
    paddingVertical: 6,
  },
  triggerCompact: { paddingHorizontal: 4 },
  triggerPressed: { backgroundColor: "#313244" },
  triggerGlyph: { color: "#c084fc", fontSize: 17, fontWeight: "700" },
  triggerText: { color: "#cdd6f4", fontSize: 12, fontWeight: "700" },
});
