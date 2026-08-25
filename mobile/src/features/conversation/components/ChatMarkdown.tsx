import React from "react";
import Markdown from "react-native-markdown-display";
import { markdownStyles } from "./chat-screen.styles";

export type ChatMarkdownProps = {
  content: string;
};

export const ChatMarkdown = React.memo(
  function ChatMarkdown({ content }: ChatMarkdownProps) {
    return <Markdown style={markdownStyles}>{content}</Markdown>;
  },
  (previous, next) => previous.content === next.content,
);
