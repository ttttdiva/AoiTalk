import { Stack } from 'expo-router';

export default function TasksLayout() {
  return (
    <Stack
      screenOptions={{
        headerStyle: { backgroundColor: '#1e1e2e' },
        headerTintColor: '#cdd6f4',
      }}
    >
      <Stack.Screen name="index" options={{ headerShown: false }} />
      <Stack.Screen name="create" options={{ title: "タスクを作成" }} />
      <Stack.Screen name="[taskId]" options={{ title: "タスク詳細" }} />
    </Stack>
  );
}
