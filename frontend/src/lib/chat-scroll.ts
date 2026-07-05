export const CHAT_AUTO_SCROLL_BOTTOM_THRESHOLD_PX = 48;

export type ChatScrollMetrics = {
  scrollHeight: number;
  scrollTop: number;
  clientHeight: number;
};

export function getChatScrollBottomDistance(metrics: ChatScrollMetrics): number {
  return Math.max(
    0,
    metrics.scrollHeight - metrics.scrollTop - metrics.clientHeight,
  );
}

export function isChatScrollPinnedToBottom(
  metrics: ChatScrollMetrics,
  threshold = CHAT_AUTO_SCROLL_BOTTOM_THRESHOLD_PX,
): boolean {
  return getChatScrollBottomDistance(metrics) <= threshold;
}

export function getChatScrollContentHash(content: string): string {
  let hash = 0;
  for (let index = 0; index < content.length; index += 1) {
    hash = (hash * 31 + content.charCodeAt(index)) | 0;
  }
  return `${content.length}:${hash}`;
}
