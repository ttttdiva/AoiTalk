import React, { useState } from "react";
import {
  Pressable,
  StyleSheet,
  View,
  type StyleProp,
  type ViewStyle,
} from "react-native";
import { Icon, Text } from "react-native-paper";

/** Small visual controls retain a 44-point touch target while keeping labels readable. */
export function TaskDetailAction({
  label,
  icon,
  onPress,
  accessibilityLabel,
  color = "#cdd6f4",
  disabled = false,
  style,
  testID,
}: {
  label?: string;
  icon: string;
  onPress: () => void;
  accessibilityLabel: string;
  color?: string;
  disabled?: boolean;
  style?: StyleProp<ViewStyle>;
  testID?: string;
}) {
  return (
    <Pressable
      testID={testID}
      accessibilityRole="button"
      accessibilityLabel={accessibilityLabel}
      accessibilityState={{ disabled }}
      disabled={disabled}
      onPress={onPress}
      style={({ pressed }) => [
        styles.action,
        style,
        (pressed || disabled) && styles.dimmed,
      ]}
    >
      <Icon source={icon} size={18} color={color} />
      {label ? (
        <Text
          style={[styles.actionText, { color }]}
        >
          {label}
        </Text>
      ) : null}
    </Pressable>
  );
}

/** Secondary content is neither visible nor focusable until explicitly expanded. */
export function TaskDetailSection({
  title,
  icon,
  count,
  children,
  initiallyExpanded = false,
  action,
}: {
  title: string;
  icon: string;
  count?: string | number;
  children: React.ReactNode;
  initiallyExpanded?: boolean;
  action?: React.ReactNode;
}) {
  const [expanded, setExpanded] = useState(initiallyExpanded);
  return (
    <View style={styles.section}>
      <View style={styles.sectionRow}>
        <Pressable
          accessibilityRole="button"
          accessibilityLabel={title}
          accessibilityState={{ expanded }}
          onPress={() => setExpanded((value) => !value)}
          style={({ pressed }) => [
            styles.sectionToggle,
            pressed && styles.dimmed,
          ]}
        >
          <Icon source={icon} size={18} color="#a6adc8" />
          <Text style={styles.sectionTitle}>{title}</Text>
          {count !== undefined ? (
            <Text style={styles.count}>{count}</Text>
          ) : null}
          <Icon
            source={expanded ? "chevron-up" : "chevron-down"}
            size={18}
            color="#9399b2"
          />
        </Pressable>
        {action}
      </View>
      {expanded ? <View style={styles.body}>{children}</View> : null}
    </View>
  );
}

const styles = StyleSheet.create({
  action: {
    minHeight: 44,
    minWidth: 44,
    maxWidth: "100%",
    flexShrink: 1,
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "center",
    gap: 6,
    paddingHorizontal: 8,
    borderRadius: 8,
  },
  actionText: {
    maxWidth: "100%",
    fontSize: 13,
    flexShrink: 1,
    flexWrap: "wrap",
    fontVariant: ["tabular-nums"],
  },
  dimmed: { opacity: 0.5 },
  section: {
    borderTopWidth: StyleSheet.hairlineWidth,
    borderTopColor: "#313244",
  },
  sectionRow: { flexDirection: "row", alignItems: "center" },
  sectionToggle: {
    flex: 1,
    minHeight: 48,
    flexDirection: "row",
    alignItems: "center",
    gap: 9,
  },
  sectionTitle: { color: "#cdd6f4", fontSize: 14, fontWeight: "600", flex: 1 },
  count: { color: "#9399b2", fontSize: 12, fontVariant: ["tabular-nums"] },
  body: { paddingBottom: 12 },
});
