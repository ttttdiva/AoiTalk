import { Stack } from 'expo-router';

export default function SettingsLayout() {
  return (
    <Stack
      screenOptions={{
        headerStyle: { backgroundColor: '#1e1e2e' },
        headerTintColor: '#cdd6f4',
      }}
    >
      <Stack.Screen name="index" options={{ headerShown: false }} />
      <Stack.Screen name="profile" options={{ title: 'Profile' }} />
      <Stack.Screen name="connection" options={{ title: 'Server / Network' }} />
      <Stack.Screen name="remote-servers" options={{ title: '外部サーバー接続' }} />
      <Stack.Screen name="notifications" options={{ title: 'Task notifications / Calendar' }} />
      <Stack.Screen name="character" options={{ title: 'Characters' }} />
      <Stack.Screen name="memory" options={{ title: 'User Memory' }} />
      <Stack.Screen name="mcp" options={{ title: 'MCP & Agents' }} />
      <Stack.Screen name="about" options={{ title: 'アプリ情報' }} />
    </Stack>
  );
}
