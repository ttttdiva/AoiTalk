import React from "react";
import { AppState, View } from "react-native";
import { ActivityIndicator, Button, Surface, Text } from "react-native-paper";
import {
  conversationPerformanceDiagnostics,
  logConversationPerformanceSnapshot,
} from "../performance-diagnostics";
import { chatScreenStyles as styles } from "./chat-screen.styles";

export function ChatScreenShell({
  loading,
  error,
  onReload,
  children,
  header,
}: {
  loading: boolean;
  error: string | null;
  onReload: () => void;
  children: React.ReactNode;
  header?: React.ReactNode;
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
  return (
    <View style={styles.container}>
      {header}
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
      {loading ? <View pointerEvents="none" style={{ position: "absolute", top: "45%", alignSelf: "center" }}>
        <ActivityIndicator size="large" color="#7c3aed" />
      </View> : null}
    </View>
  );
}
