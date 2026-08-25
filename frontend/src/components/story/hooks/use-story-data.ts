"use client";

import { useMemo } from "react";
import useSWR from "swr";
import { storyApi } from "@/lib/story/api";
import {
  listFrom,
  normalizeEpisode,
  normalizeGraph,
  normalizeJob,
  normalizeWork,
  type StoryEpisodeView,
  type StoryGraphView,
  type StoryJobView,
  type StoryWorkView,
} from "@/lib/story/view-model";

export const EMPTY_WORK: StoryWorkView = {
  id: "",
  title: "作品を読み込み中",
  kind: "novel",
  status: "planning",
  synopsis: "",
  plot: "",
  styleGuide: "",
  plannedEpisodeCount: null,
  targetEpisodeChars: 6000,
  modelOverride: {},
  resolvedModel: null,
  resolvedModelLayer: null,
  startEpisodeId: null,
  currentRoute: {},
  episodeCount: 0,
  totalChars: 0,
  notesCount: 0,
  charactersCount: 0,
  rulebooksCount: 0,
  branchCount: 0,
  updatedAt: null,
  imageSettings: {
    enabled: false,
    engine: "comfyui",
    maxImagesPerEpisode: 3,
    workflowPath: null,
    style: "",
    negativePrompt: "",
  },
};

export const EMPTY_GRAPH: StoryGraphView = { episodes: [], links: [], startEpisodeId: null };

const EMPTY_EPISODES: StoryEpisodeView[] = [];

/**
 * normalize* は毎回新しいオブジェクト / 配列を返すため、useMemo を挟まないと
 * レンダーのたびに参照が変わる。参照を依存配列に入れている useEffect
 * （分岐マップのノード同期など）が無限ループ（React error #185）になるので、
 * SWR の data が変わった時だけ再計算する。
 */
export function useStoryWork(workId: string) {
  const key = workId ? `story-work-overview:${workId}` : null;
  const result = useSWR(key, () => storyApi.getOverview(workId));
  const work = useMemo(() => (result.data ? normalizeWork(result.data) : EMPTY_WORK), [result.data]);
  return { ...result, work };
}

export function useStoryGraph(workId: string) {
  const key = workId ? `story-work-graph:${workId}` : null;
  const result = useSWR(key, () => storyApi.getGraph(workId));
  const graph = useMemo(() => (result.data ? normalizeGraph(result.data) : EMPTY_GRAPH), [result.data]);
  return { ...result, graph };
}

export function useStoryEpisode(episodeId: string | null) {
  const key = episodeId ? `story-episode:${episodeId}` : null;
  const result = useSWR(key, () => storyApi.getEpisode(episodeId as string));
  const episode = useMemo(() => (result.data ? normalizeEpisode(result.data) : null), [result.data]);
  return { ...result, episode };
}

export function useStoryEpisodeList(workId: string) {
  const key = workId ? `story-episodes:${workId}` : null;
  const result = useSWR(key, () => storyApi.listEpisodes(workId));
  const episodes = useMemo(
    () => (result.data ? listFrom(result.data, "episodes").map(normalizeEpisode) : EMPTY_EPISODES),
    [result.data],
  );
  return { ...result, episodes };
}

export function useStoryJob(jobId: string | null, enabled = true) {
  const key = jobId && enabled ? `story-job:${jobId}` : null;
  const result = useSWR(key, () => storyApi.getJob(jobId as string), { refreshInterval: 1500 });
  const job = useMemo(() => (result.data ? normalizeJob(result.data) : null), [result.data]);
  return { ...result, job };
}

export type { StoryEpisodeView, StoryGraphView, StoryJobView, StoryWorkView };
