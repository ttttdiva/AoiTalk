import { Stack } from 'expo-router';

export default function FilerLayout() {
  return (
    <Stack
      screenOptions={{
        headerStyle: { backgroundColor: '#1e1e2e' },
        headerTintColor: '#cdd6f4',
      }}
    >
      <Stack.Screen name="index" options={{ headerShown: false }} />
      <Stack.Screen name="text" options={{ headerShown: false }} />
    </Stack>
  );
}
