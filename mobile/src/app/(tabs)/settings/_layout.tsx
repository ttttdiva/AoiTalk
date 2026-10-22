import React from "react";
import { Stack } from "expo-router";
import type { NativeStackHeaderProps } from "@react-navigation/native-stack";
import { ScreenHeader } from "../../../components/screen-header";

function SettingsNativeHeader({
  title,
  navigation,
}: { title: string; navigation: NativeStackHeaderProps["navigation"] }) {
  return <ScreenHeader title={title} onBack={() => navigation.goBack()} />;
}

export default function SettingsLayout() {
  return (
    <Stack
      screenOptions={{
        headerStyle: { backgroundColor: '#1e1e2e' },
        headerTintColor: '#cdd6f4',
      }}
    >
      <Stack.Screen name="index" options={{ headerShown: false }} />
      <Stack.Screen name="profile" options={{ title: 'Profile', headerShown: false }} />
      <Stack.Screen
        name="audio"
        options={{
          title: "音声・再生",
          header: ({ navigation }) => (
            <SettingsNativeHeader title="音声・再生" navigation={navigation} />
          ),
        }}
      />
      <Stack.Screen name="appearance" options={{ title: '表示・外観', headerShown: false }} />
      <Stack.Screen name="ai-model" options={{ title: 'AIモデル', headerShown: false }} />
      <Stack.Screen
        name="custom-instructions"
        options={{
          title: "会話カスタム指示",
          header: ({ navigation }) => (
            <SettingsNativeHeader title="会話カスタム指示" navigation={navigation} />
          ),
        }}
      />
      <Stack.Screen
        name="docs-resync"
        options={{
          title: "Docs再同期",
          header: ({ navigation }) => (
            <SettingsNativeHeader title="Docs再同期" navigation={navigation} />
          ),
        }}
      />
      <Stack.Screen name="connection" options={{ title: 'Server / Network', headerShown: false }} />
      <Stack.Screen name="remote-servers" options={{ title: '外部サーバー接続', headerShown: false }} />
      <Stack.Screen name="notifications" options={{ title: 'Task notifications / Calendar', headerShown: false }} />
      <Stack.Screen name="character" options={{ title: 'Characters', headerShown: false }} />
      <Stack.Screen name="memory" options={{ title: 'User Memory', headerShown: false }} />
      <Stack.Screen name="mcp" options={{ title: 'MCP & Agents', headerShown: false }} />
      <Stack.Screen name="about" options={{ title: 'アプリ情報', headerShown: false }} />
    </Stack>
  );
}
