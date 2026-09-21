// Preserve the conversation API while sharing input theming across features.
export {
  ThemedTextInput as ChatTextInput,
  resolveInputColors as resolveChatInputColors,
  colorContrastRatio,
} from "../../components/themed-text-input";
export type {
  InputVisualState as ChatInputVisualState,
  InputResolvedColors as ChatInputResolvedColors,
} from "../../components/themed-text-input";
