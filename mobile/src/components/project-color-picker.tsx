import React from "react";
import { Pressable, StyleSheet, View } from "react-native";
import { Text, TextInput } from "react-native-paper";
import {
  DEFAULT_PROJECT_COLOR,
  PROJECT_COLOR_PRESETS,
  normalizeProjectColor,
} from "../lib/project-colors";

type ProjectColorPickerProps = {
  value: string;
  onChange: (value: string) => void;
};

export function ProjectColorPicker({ value, onChange }: ProjectColorPickerProps) {
  const inputValue = value || "";
  const previewColor = normalizeProjectColor(inputValue || DEFAULT_PROJECT_COLOR);
  const selected = previewColor.toLowerCase();

  return (
    <View style={styles.wrap}>
      <View style={styles.inputRow}>
        <View style={[styles.preview, { backgroundColor: previewColor }]} />
        <TextInput
          label="Color"
          value={inputValue}
          onChangeText={onChange}
          autoCapitalize="none"
          mode="outlined"
          style={styles.input}
        />
      </View>
      <View style={styles.swatches}>
        {PROJECT_COLOR_PRESETS.map((preset) => {
          const isSelected = preset.value.toLowerCase() === selected;
          return (
            <Pressable
              key={preset.value}
              accessibilityRole="button"
              accessibilityLabel={`${preset.name} ${preset.value}`}
              onPress={() => onChange(preset.value)}
              style={[
                styles.swatch,
                { backgroundColor: preset.value },
                isSelected ? styles.swatchSelected : null,
              ]}
            />
          );
        })}
      </View>
      <Text style={styles.hint}>プロジェクト色はタスク、カレンダー、一覧表示に使われます。</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  wrap: { marginBottom: 12 },
  inputRow: { flexDirection: "row", alignItems: "center", gap: 10 },
  preview: {
    width: 34,
    height: 34,
    borderRadius: 17,
    borderWidth: 1,
    borderColor: "#cdd6f4",
  },
  input: { flex: 1 },
  swatches: {
    flexDirection: "row",
    flexWrap: "wrap",
    gap: 8,
    marginTop: 10,
  },
  swatch: {
    width: 30,
    height: 30,
    borderRadius: 15,
    borderWidth: 1,
    borderColor: "#45475a",
  },
  swatchSelected: {
    borderColor: "#f5e9ff",
    borderWidth: 3,
  },
  hint: { color: "#a6adc8", fontSize: 12, lineHeight: 17, marginTop: 8 },
});
