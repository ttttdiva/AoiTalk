"use client";

import useSWR from "swr";
import { useCurrentUserId } from "@/components/providers/swr-global-provider";
import type { components } from "@/lib/api-types.gen";
import { rigRequest, rigUrl } from "./api";

export type PlaybackSettingsView =
  components["schemas"]["PlaybackSettingsView"];
export const playbackSettingsUrl = (character: string, revision: string) =>
  `${rigUrl(character)}/builds/${encodeURIComponent(revision)}/playback-settings?mode=conversation-v1`;

/** The shared saved snapshot is owner/revision scoped. Drafts never enter SWR. */
export function useRigPlaybackSettings(
  character: string,
  revision?: string | null,
) {
  const owner = useCurrentUserId();
  const url = revision ? playbackSettingsUrl(character, revision) : null;
  return useSWR<PlaybackSettingsView>(
    owner && url ? ["rig-playback-settings", owner, url] : null,
    ([, , endpoint]: readonly string[]) =>
      rigRequest<PlaybackSettingsView>(endpoint),
    {
      keepPreviousData: false,
      revalidateOnFocus: true,
      shouldRetryOnError: false,
    },
  );
}
