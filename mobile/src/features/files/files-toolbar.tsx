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

// Five 48dp controls use 240dp. Only the path may shrink; primary actions
// never wrap, scroll, or compete with secondary controls on portrait phones.
export function FilesToolbar({
  path, canGoBack, canGoUp, canGoHome, onBack, onUp, onHome,
  createAction, actions,
}: FilesToolbarProps) {
  const [moreVisible, setMoreVisible] = useState(false);
  useEffect(() => setMoreVisible(false), [path]);

  return (
    <View style={styles.row} testID="files-primary-toolbar">
      <IconButton
        icon="arrow-left"
        iconColor="#cdd6f4"
        size={22}
        style={styles.control}
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
        disabled={!canGoHome}
        onPress={onHome}
        accessibilityLabel="現在の領域のホームへ"
        testID="files-home"
      />
      <Pressable
        style={styles.path}
        onPress={() => Alert.alert("現在のパス", path)}
        accessibilityRole="button"
        accessibilityLabel={`現在のパス: ${path}`}
        accessibilityHint="タップするとパス全体を表示します"
        testID="files-path"
      >
        <Text style={styles.pathText} numberOfLines={1} ellipsizeMode="middle">
          {path}
        </Text>
      </Pressable>
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
  );
}

const styles = StyleSheet.create({
  row: {
    flexDirection: "row",
    alignItems: "center",
    minHeight: 48,
    paddingHorizontal: 4,
    backgroundColor: "#181825",
  },
  control: { width: 48, height: 48, margin: 0, flexShrink: 0 },
  path: { flex: 1, minWidth: 0, minHeight: 48, justifyContent: "center" },
  pathText: { color: "#a6adc8", fontSize: 12 },
});
