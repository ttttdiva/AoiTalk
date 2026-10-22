import React from "react";
import { View } from "react-native";
import Markdown from "react-native-markdown-display";
import { markdownStyles } from "./chat-screen.styles";
import { useChatWaveTouchGuard } from "./chat-wave-touch-context";

export type ChatMarkdownProps = {
  content: string;
  onLinkPress?: (href: string) => boolean;
};

export const ChatMarkdown = React.memo(
  function ChatMarkdown({ content, onLinkPress }: ChatMarkdownProps) {
    const markWaveTouchIgnored = useChatWaveTouchGuard();
    const containsLink = /(?:\[[^\]]+\]\([^\)]+\)|https?:\/\/\S+)/i.test(content);
    const markdown = <Markdown style={markdownStyles} onLinkPress={onLinkPress}>{content}</Markdown>;
    return containsLink ? (
      <View onTouchStart={markWaveTouchIgnored}>{markdown}</View>
    ) : markdown;
  },
  (previous, next) => previous.content === next.content && previous.onLinkPress === next.onLinkPress,
);
