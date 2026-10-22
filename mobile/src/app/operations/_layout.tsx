import React from "react";
import { Redirect, Stack } from "expo-router";
import { ActivityIndicator, View } from "react-native";
import { useAuth } from "../../contexts/AuthContext";

export default function OperationsLayout() {
  const { isLoading, isAuthenticated } = useAuth();
  if (isLoading) return <View style={{ flex: 1, justifyContent: "center", backgroundColor: "#11111b" }}><ActivityIndicator accessibilityLabel="認証を確認中" /></View>;
  if (!isAuthenticated) return <Redirect href="/(auth)/login" />;
  return <Stack screenOptions={{ headerShown: false, contentStyle: { backgroundColor: "#11111b" } }} />;
}


