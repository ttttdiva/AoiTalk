export function resolveChatToolsRequired(
  commandCapabilities: readonly unknown[] | undefined,
  toolFreeMode: boolean,
): boolean | undefined {
  if (commandCapabilities?.length) return true;
  return toolFreeMode ? false : undefined;
}
