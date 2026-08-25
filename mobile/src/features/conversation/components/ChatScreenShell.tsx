import React from "react";
import { Animated, AppState, View } from "react-native";
import { ActivityIndicator, Button, Surface, Text } from "react-native-paper";
import {
  conversationPerformanceDiagnostics,
  logConversationPerformanceSnapshot,
} from "../performance-diagnostics";
import { chatScreenStyles as styles } from "./chat-screen.styles";

export function ChatScreenShell({
  loading,
  error,
  opacity,
  onReload,
  children,
}: {
  loading: boolean;
  error: string | null;
  opacity: Animated.Value;
  onReload: () => void;
  children: React.ReactNode;
}) {
  conversationPerformanceDiagnostics.recordRender("ChatScreenShell");
  React.useEffect(() => {
    let stopFrameObserver =
      conversationPerformanceDiagnostics.startFrameObserver("ChatScreen");
    if (!conversationPerformanceDiagnostics.enabled) return stopFrameObserver;
    let previousState = AppState.currentState;
    const subscription = AppState.addEventListener("change", (nextState) => {
      if (previousState === "active" && nextState !== "active") {
        logConversationPerformanceSnapshot();
        stopFrameObserver();
        stopFrameObserver = () => undefined;
      } else if (previousState !== "active" && nextState === "active") {
        stopFrameObserver =
          conversationPerformanceDiagnostics.startFrameObserver("ChatScreen");
      }
      previousState = nextState;
    });
    return () => {
      subscription.remove();
      stopFrameObserver();
    };
  }, []);
  if (loading) {
    return (
      <View style={styles.loadingContainer}>
        <ActivityIndicator size="large" color="#7c3aed" />
      </View>
    );
  }
  return (
    <Animated.View style={[styles.container, { opacity }]}>
      {error ? (
        <Surface style={styles.errorBanner} elevation={0}>
          <Text style={styles.errorText}>{error}</Text>
          <Button
            compact
            textColor="#89b4fa"
            onPress={() =>
              conversationPerformanceDiagnostics.measureInteraction(
                "ChatScreenShell.reload",
                onReload,
              )
            }
          >
            再読み込み
          </Button>
        </Surface>
      ) : null}
      {children}
    </Animated.View>
  );
}
