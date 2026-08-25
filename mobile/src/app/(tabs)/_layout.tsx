import { Redirect, Tabs } from "expo-router";
import { ActivityIndicator, View } from "react-native";
import { Icon } from "react-native-paper";
import { useAuth } from "../../contexts/AuthContext";

export default function TabLayout() {
  const { isLoading, canUseApp } = useAuth();

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
        tabBarStyle: { backgroundColor: "#1e1e2e", borderTopColor: "#313244" },
        tabBarActiveTintColor: "#7c3aed",
        tabBarInactiveTintColor: "#a6adc8",
        tabBarHideOnKeyboard: true,
      }}
    >
      <Tabs.Screen
        name="chat"
        options={{
          title: "Chat",
          tabBarIcon: ({ color, size }) => (
            <Icon source="chat-outline" size={size} color={color} />
          ),
        }}
      />
      <Tabs.Screen
        name="tasks"
        options={{
          title: "Tasks",
          tabBarIcon: ({ color, size }) => (
            <Icon source="checkbox-marked-outline" size={size} color={color} />
          ),
        }}
      />
      <Tabs.Screen
        name="apps"
        options={{
          href: null,
          title: "Apps",
          tabBarIcon: ({ color, size }) => (
            <Icon source="application-brackets-outline" size={size} color={color} />
          ),
        }}
      />
      <Tabs.Screen
        name="calendar"
        options={{
          title: "Calendar",
          tabBarIcon: ({ color, size }) => (
            <Icon source="calendar-month-outline" size={size} color={color} />
          ),
        }}
      />
      <Tabs.Screen
        name="filer"
        options={{
          title: "Files",
          tabBarIcon: ({ color, size }) => (
            <Icon source="folder-outline" size={size} color={color} />
          ),
        }}
      />
      <Tabs.Screen
        name="docs"
        options={{
          title: "Docs",
          tabBarIcon: ({ color, size }) => (
            <Icon source="file-tree-outline" size={size} color={color} />
          ),
        }}
      />
      {/* Settings はタブバーから外し、共通ヘッダー右上の歯車から遷移する（スタックは生存） */}
      <Tabs.Screen name="settings" options={{ href: null }} />
      <Tabs.Screen name="docs/[nodeId]" options={{ href: null }} />
      <Tabs.Screen name="filer/text" options={{ href: null }} />
      <Tabs.Screen name="chat/[sessionId]" options={{ href: null }} />
      <Tabs.Screen name="tasks/[taskId]" options={{ href: null }} />
      <Tabs.Screen name="apps/[appId]" options={{ href: null }} />
      <Tabs.Screen name="settings/profile" options={{ href: null }} />
      <Tabs.Screen name="settings/connection" options={{ href: null }} />
      <Tabs.Screen name="settings/notifications" options={{ href: null }} />
      <Tabs.Screen name="settings/character" options={{ href: null }} />
      <Tabs.Screen name="settings/memory" options={{ href: null }} />
      <Tabs.Screen name="settings/mcp" options={{ href: null }} />
      <Tabs.Screen name="settings/about" options={{ href: null }} />
    </Tabs>
  );
}
