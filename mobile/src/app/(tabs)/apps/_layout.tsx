import { Stack } from "expo-router";

export default function AppsLayout() {
  return (
    <Stack
      screenOptions={{
        headerStyle: { backgroundColor: "#1e1e2e" },
        headerTintColor: "#cdd6f4",
      }}
    >
      <Stack.Screen name="index" options={{ headerShown: false }} />
      <Stack.Screen name="[appId]" options={{ headerShown: false }} />
    </Stack>
  );
}

