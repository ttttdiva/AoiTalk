import { LayoutAnimation, Platform, UIManager } from "react-native";
import type { ConversationSession } from "../../types/api";

let androidLayoutAnimationEnabled = false;

export function areConversationListsEqual(
  previous: readonly ConversationSession[],
  next: readonly ConversationSession[],
): boolean {
  if (previous.length !== next.length) return false;
  return previous.every((session, index) => {
    const candidate = next[index];
    return (
      candidate?.id === session.id &&
      candidate.title === session.title &&
      candidate.character_name === session.character_name &&
      candidate.message_count === session.message_count &&
      candidate.last_activity === session.last_activity &&
      candidate.development_status === session.development_status &&
      candidate.is_unread === session.is_unread
    );
  });
}

export function animateNextListChange(reduceMotion: boolean): void {
  if (reduceMotion) return;
  if (Platform.OS === "android" && !androidLayoutAnimationEnabled) {
    const manager = UIManager as typeof UIManager & {
      setLayoutAnimationEnabledExperimental?: (enabled: boolean) => void;
    };
    manager.setLayoutAnimationEnabledExperimental?.(true);
    androidLayoutAnimationEnabled = true;
  }
  LayoutAnimation.configureNext({
    duration: 180,
    create: {
      type: LayoutAnimation.Types.easeInEaseOut,
      property: LayoutAnimation.Properties.opacity,
    },
    update: {
      type: LayoutAnimation.Types.easeInEaseOut,
    },
    delete: {
      type: LayoutAnimation.Types.easeInEaseOut,
      property: LayoutAnimation.Properties.opacity,
    },
  });
}
