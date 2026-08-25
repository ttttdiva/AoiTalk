import React from "react";
import { Surface } from "react-native-paper";
import { ChatMarkdown } from "./ChatMarkdown";
import { chatScreenStyles as styles } from "./chat-screen.styles";
import { conversationPerformanceDiagnostics } from "../performance-diagnostics";

export const StreamingMessageRow = React.memo(function StreamingMessageRow({
  content,
}: {
  content: string;
}) {
  conversationPerformanceDiagnostics.recordRender("StreamingMessageRow");
  if (!content) return null;
  return (
    <Surface
      style={[styles.messageBubble, styles.assistantBubble]}
      elevation={0}
    >
      <ChatMarkdown content={content} />
    </Surface>
  );
}, (previous, next) => previous.content === next.content);
