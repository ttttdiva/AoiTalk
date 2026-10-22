/**
 * Opt-in build flag for QA that must inspect local state without running sync.
 * Normal builds leave this unset, so their sync behavior stays unchanged.
 */
export function isMobileReadOnlyDiagnosticsEnabled(): boolean {
  return process.env.EXPO_PUBLIC_AOITALK_READ_ONLY_DIAGNOSTICS === "1";
}
