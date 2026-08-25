/**
 * ルート計算が扱うエピソード。API 応答は `lib/story/view-model.ts` の
 * `normalizeEpisode` / `normalizeGraph` で camelCase へ正規化してから渡す。
 */
export type StoryRouteEpisode = {
  id: string;
  title?: string | null;
  body?: string | null;
  summary?: string | null;
  charCount?: number | null;
  targetChars?: number | null;
  status?: string | null;
};

/** ルート計算が扱う接続。`normalizeLink` / `normalizeGraph` の出力を渡す。 */
export type StoryRouteLink = {
  id?: string | null;
  from: string;
  to: string;
  choiceLabel?: string | null;
  isPrimary?: boolean | null;
  position?: number | null;
};

export type StoryRouteInput = {
  startEpisodeId?: string | null;
  episodes: readonly StoryRouteEpisode[];
  links: readonly StoryRouteLink[];
  currentRoute?: Record<string, string> | null;
};

export type StoryRouteItem = StoryRouteEpisode & {
  index: number;
  linkFromPrevious?: StoryRouteLink;
};

function sortLinks(left: StoryRouteLink, right: StoryRouteLink): number {
  const leftPrimary = left.isPrimary ? 0 : 1;
  const rightPrimary = right.isPrimary ? 0 : 1;
  if (leftPrimary !== rightPrimary) return leftPrimary - rightPrimary;
  return (left.position ?? Number.MAX_SAFE_INTEGER) - (right.position ?? Number.MAX_SAFE_INTEGER);
}

/**
 * start_episode_id から主ルートを解決する純関数。
 * 無効な選択は主ルートへ戻し、壊れた循環データも visited で停止する。
 */
export function resolveStoryRoute({
  startEpisodeId,
  episodes,
  links,
  currentRoute,
}: StoryRouteInput): StoryRouteItem[] {
  if (!startEpisodeId || !episodes.some((episode) => episode.id === startEpisodeId)) return [];
  const byId = new Map(episodes.map((episode) => [episode.id, episode]));
  const route: StoryRouteItem[] = [];
  const visited = new Set<string>();
  let currentId: string | null = startEpisodeId;
  let previousLink: StoryRouteLink | undefined;

  while (currentId && !visited.has(currentId)) {
    const episode = byId.get(currentId);
    if (!episode) break;
    visited.add(currentId);
    route.push({
      ...episode,
      index: route.length,
      linkFromPrevious: previousLink,
    });

    const outgoing = links
      .filter((link) => link.from === currentId && byId.has(link.to) && link.from !== link.to)
      .sort(sortLinks);
    if (!outgoing.length) break;
    const selectedId: string | undefined = currentRoute?.[currentId];
    const selected: StoryRouteLink | undefined = selectedId ? outgoing.find((link) => link.to === selectedId) : undefined;
    const next: StoryRouteLink = selected ?? outgoing[0];
    previousLink = next;
    currentId = next.to;
  }

  return route;
}

export function getUnplacedStoryEpisodes(
  episodes: readonly StoryRouteEpisode[],
  links: readonly StoryRouteLink[],
): StoryRouteEpisode[] {
  const connected = new Set<string>();
  for (const link of links) {
    connected.add(link.from);
    connected.add(link.to);
  }
  return episodes.filter((episode) => !connected.has(episode.id));
}

export function routeLinkChoices(
  episodeId: string,
  links: readonly StoryRouteLink[],
  episodes: readonly StoryRouteEpisode[],
): StoryRouteLink[] {
  const episodeIds = new Set(episodes.map((episode) => episode.id));
  return links
    .filter((link) => link.from === episodeId && episodeIds.has(link.to))
    .sort(sortLinks);
}

export function countStoryCharacters(value: string | null | undefined): number {
  return Array.from(value ?? "").length;
}

export function countRouteCharacters(route: readonly StoryRouteEpisode[]): number {
  return route.reduce((total, episode) => total + (episode.charCount ?? countStoryCharacters(episode.body)), 0);
}

export function routeBreadcrumbs(route: readonly StoryRouteItem[]): string[] {
  return route.map((episode) => episode.title?.trim() || `第${episode.index + 1}章`);
}

export type StoryBranchSiblingGroup = {
  parentId: string;
  choices: StoryRouteLink[];
  episodeIds: string[];
};

/**
 * 分岐点の兄弟章グループを返す。同一親からの outgoing が 2 件以上ある場合のみ。
 * 対象章がその分岐の子でない場合は null。
 */
export function getBranchSiblingGroup(
  episodeId: string,
  links: readonly StoryRouteLink[],
  episodes: readonly StoryRouteEpisode[],
): StoryBranchSiblingGroup | null {
  const incoming = links.filter((link) => link.to === episodeId);
  for (const parentLink of incoming) {
    const choices = routeLinkChoices(parentLink.from, links, episodes);
    if (choices.length <= 1) continue;
    if (!choices.some((choice) => choice.to === episodeId)) continue;
    return {
      parentId: parentLink.from,
      choices,
      episodeIds: choices.map((choice) => choice.to),
    };
  }
  return null;
}

export function isEpisodeOnRoute(episodeId: string, routeEpisodeIds: readonly string[]): boolean {
  return routeEpisodeIds.includes(episodeId);
}
