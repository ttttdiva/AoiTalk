import React, { useEffect, useState, type ReactNode } from "react";
import { Alert, Pressable, StyleSheet, View } from "react-native";
import { IconButton, Menu, Text } from "react-native-paper";

export type FilesToolbarAction = {
  id: string;
  icon: string;
  title: string;
  disabled?: boolean;
  onPress: () => void;
};

type FilesToolbarProps = {
  path: string;
  canGoBack: boolean;
  canGoUp: boolean;
  canGoHome: boolean;
  onBack: () => void;
  onUp: () => void;
  onHome: () => void;
  createAction: ReactNode;
  actions: readonly FilesToolbarAction[];
};

// Five 40dp controls (hitSlop 4 keeps a 48dp touch area) use 200dp. Only the path may shrink; primary actions
// never wrap, scroll, or compete with secondary controls on portrait phones.
export function FilesToolbar({
  path, canGoBack, canGoUp, canGoHome, onBack, onUp, onHome,
  createAction, actions,
}: FilesToolbarProps) {
  const [moreVisible, setMoreVisible] = useState(false);
  useEffect(() => setMoreVisible(false), [path]);

  return (
    <View style={styles.container} testID="files-primary-toolbar">
      <View style={styles.controlsRow}>
        <IconButton
          icon="arrow-left"
          iconColor="#cdd6f4"
          size={22}
          style={styles.control}
          hitSlop={4}
          disabled={!canGoBack}
          onPress={onBack}
          accessibilityLabel="前のフォルダーへ戻る"
          testID="files-back"
        />
        <IconButton
          icon="arrow-up"
          iconColor="#cdd6f4"
          size={22}
          style={styles.control}
          hitSlop={4}
          disabled={!canGoUp}
          onPress={onUp}
          accessibilityLabel="上のフォルダーへ"
          testID="files-up"
        />
        <IconButton
          icon="home-outline"
          iconColor="#cdd6f4"
          size={22}
          style={styles.control}
          hitSlop={4}
          disabled={!canGoHome}
          onPress={onHome}
          accessibilityLabel="現在の領域のホームへ"
          testID="files-home"
        />
        <View style={styles.control} testID="files-primary-create">
          {createAction}
        </View>
        <Menu
          visible={moreVisible}
          onDismiss={() => setMoreVisible(false)}
          anchor={
            <IconButton
              icon="dots-vertical"
              iconColor="#cdd6f4"
              size={22}
              style={styles.control}
              hitSlop={4}
              onPress={() => setMoreVisible(true)}
              accessibilityLabel="その他のファイル操作"
              testID="files-more"
            />
          }
        >
          {actions.map((action) => (
            <Menu.Item
              key={action.id}
              leadingIcon={action.icon}
              title={action.title}
              accessibilityLabel={action.title}
              testID={action.id}
              disabled={action.disabled}
              onPress={() => {
                setMoreVisible(false);
                action.onPress();
              }}
            />
          ))}
        </Menu>
      </View>
      <Pressable
        style={styles.path}
        onPress={() => Alert.alert("現在のパス", path)}
        accessibilityRole="button"
        accessibilityLabel={`現在のパス: ${path}`}
        accessibilityHint="タップするとパス全体を表示します"
        testID="files-path"
      >
        <Text style={styles.pathText}>{path}</Text>
      </Pressable>
    </View>
  );
}

const styles = StyleSheet.create({
  container: {
    backgroundColor: "#181825",
  },
  controlsRow: {
    flexDirection: "row",
    alignItems: "center",
    minHeight: 40,
    paddingHorizontal: 4,
  },
  control: { width: 40, height: 40, margin: 0, flexShrink: 0 },
  path: {
    minWidth: 0,
    minHeight: 28,
    justifyContent: "center",
    paddingHorizontal: 12,
    paddingTop: 0,
    paddingBottom: 6,
  },
  pathText: { color: "#a6adc8", fontSize: 12, flexShrink: 1 },
});
