import { Redirect, Tabs } from "expo-router";
import { ActivityIndicator, Text, useWindowDimensions, View } from "react-native";
import { Icon } from "react-native-paper";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { useAuth } from "../../contexts/AuthContext";

const TAB_ICON_SIZE = 24;
const TAB_LABEL_FONT_SIZE = 10;
const TAB_LABEL_NATURAL_LINE_HEIGHT = 14;
const TAB_ITEM_PADDING = 5;
const TAB_BAR_DEFAULT_HEIGHT = 49;
const TAB_BAR_LAYOUT_GUARD = 2;
const TAB_LABEL_MINIMUM_SCALE = 0.8;
const TAB_LABEL_WRAP_FONT_SCALE = 1.3;

type TabLabelProps = {
  color: string;
  children: string;
  fontScale: number;
};

function TabBarLabel({ color, children, fontScale }: TabLabelProps) {
  const numberOfLines = fontScale > TAB_LABEL_WRAP_FONT_SCALE ? 2 : 1;

  return (
    <Text
      accessibilityLabel={children}
      allowFontScaling
      adjustsFontSizeToFit
      minimumFontScale={TAB_LABEL_MINIMUM_SCALE}
      numberOfLines={numberOfLines}
      style={{
        color,
        fontSize: TAB_LABEL_FONT_SIZE,
        includeFontPadding: false,
        maxWidth: "100%",
        width: "100%",
        flexShrink: 1,
        textAlign: "center",
      }}
    >
      {children}
    </Text>
  );
}

export default function TabLayout() {
  const { isLoading, canUseApp } = useAuth();
  const { fontScale } = useWindowDimensions();
  const insets = useSafeAreaInsets();
  const scaledLabelHeight = Math.ceil(
    TAB_LABEL_NATURAL_LINE_HEIGHT * Math.max(1, fontScale),
  );
  const labelLineCount = fontScale > TAB_LABEL_WRAP_FONT_SCALE ? 2 : 1;
  const requiredTabBarHeight =
    TAB_ICON_SIZE +
    scaledLabelHeight * labelLineCount +
    TAB_ITEM_PADDING * 2 +
    TAB_BAR_LAYOUT_GUARD;
  const scaledTabBarHeight =
    fontScale > 1 && requiredTabBarHeight > TAB_BAR_DEFAULT_HEIGHT
      ? requiredTabBarHeight + insets.bottom
      : undefined;

  const renderTabLabel = ({ color, children }: { color: string; children: string }) => (
    <TabBarLabel color={color} fontScale={fontScale}>
      {children}
    </TabBarLabel>
  );

  if (isLoading) {
    return (
      <View
        style={{
          flex: 1,
          justifyContent: "center",
          alignItems: "center",
          backgroundColor: "#11111b",
        }}
      >
        <ActivityIndicator size="large" color="#7c3aed" />
      </View>
    );
  }

  if (!canUseApp) {
    return <Redirect href="/(auth)/login" />;
  }

  return (
    <Tabs
      screenOptions={{
        headerShown: false,
        tabBarStyle: {
          backgroundColor: "#1e1e2e",
          borderTopColor: "#313244",
          ...(scaledTabBarHeight === undefined
            ? {}
            : { height: scaledTabBarHeight }),
        },
        tabBarActiveTintColor: "#7c3aed",
        tabBarInactiveTintColor: "#a6adc8",
        tabBarHideOnKeyboard: true,
        tabBarAllowFontScaling: true,
        tabBarLabelPosition: "below-icon",
        tabBarIconStyle: { width: TAB_ICON_SIZE, height: TAB_ICON_SIZE },
      }}
    >
      <Tabs.Screen
        name="chat"
        options={{
          title: "Chat",
          tabBarAccessibilityLabel: "Chat",
          tabBarLabel: renderTabLabel,
          tabBarIcon: ({ color }) => (
            <Icon source="chat-outline" size={TAB_ICON_SIZE} color={color} />
          ),
        }}
      />
      <Tabs.Screen
        name="tasks"
        options={{
          title: "Tasks",
          tabBarAccessibilityLabel: "Tasks",
          tabBarLabel: renderTabLabel,
          tabBarIcon: ({ color }) => (
            <Icon source="checkbox-marked-outline" size={TAB_ICON_SIZE} color={color} />
          ),
        }}
      />
      <Tabs.Screen
        name="apps"
        options={{
          href: null,
          title: "Apps",
          tabBarAccessibilityLabel: "Apps",
          tabBarLabel: renderTabLabel,
          tabBarIcon: ({ color }) => (
            <Icon source="application-brackets-outline" size={TAB_ICON_SIZE} color={color} />
          ),
        }}
      />
      <Tabs.Screen
        name="calendar"
        options={{
          title: "Calendar",
          tabBarAccessibilityLabel: "Calendar",
          tabBarLabel: renderTabLabel,
          tabBarIcon: ({ color }) => (
            <Icon source="calendar-month-outline" size={TAB_ICON_SIZE} color={color} />
          ),
        }}
      />
      <Tabs.Screen
        name="filer"
        options={{
          title: "Files",
          tabBarAccessibilityLabel: "Files",
          tabBarLabel: renderTabLabel,
          tabBarIcon: ({ color }) => (
            <Icon source="folder-outline" size={TAB_ICON_SIZE} color={color} />
          ),
        }}
      />
      <Tabs.Screen
        name="docs"
        options={{
          title: "Docs",
          tabBarAccessibilityLabel: "Docs",
          tabBarLabel: renderTabLabel,
          tabBarIcon: ({ color }) => (
            <Icon source="file-tree-outline" size={TAB_ICON_SIZE} color={color} />
          ),
        }}
      />
      {/* Settings はタブバーから外し、共通ヘッダー右上の歯車から遷移する（スタックは生存） */}
      <Tabs.Screen name="settings" options={{ href: null }} />
    </Tabs>
  );
}
