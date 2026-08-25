"use client";

/**
 * @deprecated Apps is rendered inside the shared shell. Keep this export as a
 * compatibility adapter for extensions that still import the old symbol, but
 * never mount a second Apps-specific global rail.
 */
export function AppsGlobalRail() {
  return null;
}
