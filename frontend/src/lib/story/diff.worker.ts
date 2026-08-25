import { diffArrays, diffChars, type ChangeObject } from "diff";

export type StoryDiffGranularity = "character" | "word" | "paragraph";

export type StoryDiffFineGranularity = Exclude<StoryDiffGranularity, "paragraph">;

export type StoryDiffOperation = {
  value: string;
  added?: boolean;
  removed?: boolean;
};

/** 段落と段落区切りを交互に保つ形で分割する。join すると元の本文に戻る。 */
function splitParagraphs(value: string): string[] {
  return value.split(/(\r?\n\s*\r?\n)/g).filter((part) => part.length > 0);
}

function segmentJapaneseWords(value: string): string[] {
  if (typeof Intl !== "undefined" && "Segmenter" in Intl) {
    const Segmenter = (Intl as typeof Intl & {
      Segmenter?: new (locales?: string | string[], options?: { granularity: "word" }) => {
        segment(input: string): Iterable<{ segment: string; isWordLike?: boolean }>;
      };
    }).Segmenter;
    if (Segmenter) {
      return Array.from(new Segmenter("ja-JP", { granularity: "word" }).segment(value), (part) => part.segment);
    }
  }
  return Array.from(value);
}

function sameToken(left: string, right: string): boolean {
  return left === right;
}

function changesToOperations(changes: readonly ChangeObject<string | string[]>[]): StoryDiffOperation[] {
  return changes.map((change) => ({
    value: Array.isArray(change.value) ? change.value.join("") : change.value,
    ...(change.added ? { added: true } : {}),
    ...(change.removed ? { removed: true } : {}),
  }));
}

/** 同種の操作が連続したらまとめる。空文字の操作は捨てる。 */
function appendOperation(target: StoryDiffOperation[], operation: StoryDiffOperation): void {
  if (!operation.value) return;
  const last = target[target.length - 1];
  if (
    last &&
    Boolean(last.added) === Boolean(operation.added) &&
    Boolean(last.removed) === Boolean(operation.removed)
  ) {
    last.value += operation.value;
    return;
  }
  target.push({ ...operation });
}

/** 変化のある段落ペアだけに掛ける細粒度 diff。 */
function fineDiff(oldText: string, newText: string, granularity: StoryDiffFineGranularity): StoryDiffOperation[] {
  if (granularity === "character") return changesToOperations(diffChars(oldText, newText));
  return changesToOperations(
    diffArrays(segmentJapaneseWords(oldText), segmentJapaneseWords(newText), { comparator: sameToken }),
  );
}

function paragraphOperations(oldBody: string, newBody: string): StoryDiffOperation[] {
  return changesToOperations(
    diffArrays(splitParagraphs(oldBody), splitParagraphs(newBody), { comparator: sameToken }),
  );
}

/**
 * メインスレッドでもテストできる、Worker と同一の差分純関数。
 *
 * 10 万字級に備え、まず段落アンカーで粗く差分を取り、変化のある段落ペアだけを
 * 文字 / 単語粒度へ落とす（設計 §6.5）。一致する段落は細粒度 diff に掛からない。
 */
export function computeStoryDiff(
  oldBody: string,
  newBody: string,
  granularity: StoryDiffGranularity,
): StoryDiffOperation[] {
  const coarse = paragraphOperations(oldBody, newBody);
  if (granularity === "paragraph") return coarse;

  const result: StoryDiffOperation[] = [];
  for (let index = 0; index < coarse.length; index += 1) {
    const current = coarse[index];
    const next = coarse[index + 1];
    // 削除段落の直後に追加段落が来るペアは「置き換え」とみなして細かく差分を取る。
    if (current.removed && next && next.added) {
      for (const operation of fineDiff(current.value, next.value, granularity)) appendOperation(result, operation);
      index += 1;
      continue;
    }
    if (current.added && next && next.removed) {
      for (const operation of fineDiff(next.value, current.value, granularity)) appendOperation(result, operation);
      index += 1;
      continue;
    }
    appendOperation(result, current);
  }
  return result;
}

export type StoryDiffWorkerRequest = {
  id?: string;
  oldBody: string;
  newBody: string;
  granularity: StoryDiffGranularity;
};

export type StoryDiffWorkerResponse = {
  id?: string;
  operations: StoryDiffOperation[];
};

// ブラウザ本体にもこのモジュールを import するため、window の self を
// Worker の self と取り違えない。Dedicated Worker では document が存在しない。
if (typeof self !== "undefined" && typeof document === "undefined") {
  self.onmessage = (event: MessageEvent<StoryDiffWorkerRequest>) => {
    const { id, oldBody, newBody, granularity } = event.data;
    const response: StoryDiffWorkerResponse = {
      id,
      operations: computeStoryDiff(oldBody, newBody, granularity),
    };
    self.postMessage(response);
  };
}
