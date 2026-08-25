import {
  buildFallbackMigemoTerms,
  findIncrementalSearchMatch,
  findNextIncrementalSearchMatch,
  type FilerSearchItem,
} from "@/lib/migemo-lite";

/** 続けて入力された文字を同じ検索文字列として扱う猶予。 */
export const INCREMENTAL_SEARCH_CONTINUATION_MS = 1000;

const MIGEMO_TERM_LIMIT = 240;

export interface IncrementalSearchSurface {
  /**
   * 検索対象リストの同一性。ディレクトリ移動・タブ切替・scope/principal 変更・
   * Bookmark ↔ Launcher 切替などで変わる値を返す。変化した時点で直近の検索条件は
   * 失効し、別リストへ持ち越されない。
   */
  getContextKey: () => string;
  /** 現在実際に選択可能な表示項目。 */
  getItems: () => FilerSearchItem[];
  /** 現在フォーカス中の項目キー（`FilerSearchItem.path`）。 */
  getActiveKey: () => string | null;
  focusMatch: (item: FilerSearchItem) => void;
  /**
   * 取得できなかった場合は null、取得できたが候補が無い場合は空配列を返す。
   * 空配列のときは fallback terms で選択をやり直し、null のときは同期適用済みの
   * fallback をそのまま残す。
   */
  fetchTerms?: (query: string) => Promise<string[] | null>;
}

export interface IncrementalSearchController {
  /** 通常文字入力を1文字受け取り、一致項目へフォーカスを移す。 */
  handleCharacter: (input: string) => void;
  /** 直近に成立した検索条件で、現在の一致項目を飛ばして次の一致へ移る。 */
  focusNextMatch: () => boolean;
  /** 現在の context で有効な検索条件を持っているか。 */
  hasActiveSearch: () => boolean;
  reset: () => void;
  /**
   * 参照する surface を差し替える。controller の identity を保ったまま最新の
   * 表示状態を読ませるための注入点。React では commit 後の layout effect から
   * 呼び、render 本体からは呼ばない。
   */
  setSurface: (next: IncrementalSearchSurface) => void;
}

export async function fetchMigemoTerms(query: string): Promise<string[] | null> {
  const params = new URLSearchParams({
    q: query,
    limit: String(MIGEMO_TERM_LIMIT),
  });
  try {
    const response = await fetch(`/api/migemo?${params.toString()}`, {
      credentials: "include",
      cache: "no-store",
    });
    if (!response.ok) return null;
    const data = (await response.json()) as { terms?: string[] };
    return data.terms ?? [];
  } catch {
    // fallback terms は同期的に適用済み。
    return null;
  }
}

/**
 * Files のインクリメンタル検索を UI 非依存に扱う。
 *
 * 「入力継続の判定（{@link INCREMENTAL_SEARCH_CONTINUATION_MS}）」と
 * 「最後に成立した検索条件」を別の責務として保持するため、継続猶予が切れた後でも
 * {@link IncrementalSearchController.focusNextMatch} は直近の terms を使える。
 */
export function createIncrementalSearchController(
  initialSurface: IncrementalSearchSurface,
): IncrementalSearchController {
  let surface = initialSurface;
  const state = {
    /** 連続入力バッファ。継続猶予を超えると空になる。 */
    pendingQuery: "",
    lastInputAt: 0,
    continuationTimeoutId: null as number | null,
    /** 最後に成立した検索条件。継続猶予では失効させない。 */
    activeQuery: "",
    activeTerms: [] as string[],
    contextKey: "",
    requestId: 0,
  };

  const clearContinuation = () => {
    if (state.continuationTimeoutId !== null) {
      window.clearTimeout(state.continuationTimeoutId);
      state.continuationTimeoutId = null;
    }
  };

  const focusFirstMatch = (terms: string[], baseKey: string | null) => {
    const match = findIncrementalSearchMatch(surface.getItems(), baseKey, terms);
    if (!match) return null;
    surface.focusMatch(match);
    return match.path;
  };

  const isCurrentContext = () => state.contextKey === surface.getContextKey();

  /**
   * 非同期 Migemo 結果を適用してよいか。requestId / query に加え、
   * 「今の surface の context」がリクエスト開始時と同一であることを必須とする。
   * state.contextKey だけだと、文字入力も reset も無い context 切替では
   * 旧値が残ったままになり、切替後 surface へ古い terms が届く。
   */
  const isLiveRequest = (requestId: number, contextKey: string, query: string) =>
    state.requestId === requestId &&
    state.contextKey === contextKey &&
    state.activeQuery === query &&
    surface.getContextKey() === contextKey;

  const handleCharacter = (input: string) => {
    const contextKey = surface.getContextKey();
    const now = Date.now();
    const continued =
      state.contextKey === contextKey &&
      now - state.lastInputAt <= INCREMENTAL_SEARCH_CONTINUATION_MS;

    clearContinuation();
    state.pendingQuery = continued ? state.pendingQuery + input : input;
    state.lastInputAt = now;
    state.continuationTimeoutId = window.setTimeout(() => {
      state.pendingQuery = "";
      state.continuationTimeoutId = null;
    }, INCREMENTAL_SEARCH_CONTINUATION_MS);

    const query = state.pendingQuery;
    const requestId = state.requestId + 1;
    state.requestId = requestId;
    state.contextKey = contextKey;
    state.activeQuery = query;

    const fallbackTerms = buildFallbackMigemoTerms(query);
    state.activeTerms = fallbackTerms;
    const baseKey = surface.getActiveKey();
    const fallbackFocusedKey = focusFirstMatch(fallbackTerms, baseKey) ?? baseKey;

    const fetchTerms = surface.fetchTerms ?? fetchMigemoTerms;
    void (async () => {
      const terms = await fetchTerms(query);
      if (!terms) return;
      if (!isLiveRequest(requestId, contextKey, query)) return;
      // 候補が無くても fallback terms で選択をやり直す。同期実行時にまだ描画が
      // 揃っていなかった場合（ディレクトリ移動直後など）、ここが唯一の再試行になる。
      const nextTerms = terms.length > 0 ? terms : fallbackTerms;
      state.activeTerms = nextTerms;
      // 継続猶予が切れた後の response は terms だけ更新し、選択は動かさない。
      if (state.pendingQuery !== query) return;
      if (!isLiveRequest(requestId, contextKey, query)) return;
      focusFirstMatch(nextTerms, fallbackFocusedKey);
    })();
  };

  const focusNextMatch = () => {
    if (state.activeTerms.length === 0 || !isCurrentContext()) return false;
    const match = findNextIncrementalSearchMatch(
      surface.getItems(),
      surface.getActiveKey(),
      state.activeTerms,
    );
    if (!match) return false;
    surface.focusMatch(match);
    return true;
  };

  const reset = () => {
    clearContinuation();
    state.pendingQuery = "";
    state.lastInputAt = 0;
    state.activeQuery = "";
    state.activeTerms = [];
    state.contextKey = "";
    state.requestId += 1;
  };

  return {
    handleCharacter,
    focusNextMatch,
    hasActiveSearch: () => state.activeTerms.length > 0 && isCurrentContext(),
    reset,
    setSurface: (next) => {
      const nextKey = next.getContextKey();
      const contextChanged = state.contextKey !== "" && nextKey !== state.contextKey;
      surface = next;
      // Bookmark↔Launcher・ディレクトリ移動など、文字入力を伴わない切替でも
      // 進行中の Migemo request を明示的に無効化する。
      if (contextChanged) reset();
    },
  };
}
