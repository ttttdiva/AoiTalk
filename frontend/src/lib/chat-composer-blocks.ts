/**
 * A paired Markdown fenced-code range in a chat composer value.
 *
 * All offsets are UTF-16 offsets, matching HTMLTextAreaElement's selection
 * APIs. `contentStart` is immediately after the opening fence line and
 * `contentEnd` is immediately before the line break that precedes the closing
 * fence. The fence lines themselves are therefore never part of the content
 * range.
 */
export type ChatComposerCodeRange = {
  contentStart: number;
  contentEnd: number;
  blockStart: number;
  blockEnd: number;
  language?: string;
};

type LineRange = {
  start: number;
  end: number;
  text: string;
};

type InternalCodeRange = ChatComposerCodeRange & {
  openingLineEnd: number;
  closingLineStart: number;
  closingLineEnd: number;
};

/** Split a value into lines while retaining offsets and CRLF boundaries. */
function splitLines(value: string): LineRange[] {
  const lines: LineRange[] = [];
  let start = 0;

  while (start < value.length || lines.length === 0) {
    const newlineIndex = value.indexOf("\n", start);
    if (newlineIndex === -1) {
      lines.push({
        start,
        end: value.length,
        text: value.slice(start),
      });
      break;
    }

    const textEnd =
      newlineIndex > start && value[newlineIndex - 1] === "\r"
        ? newlineIndex - 1
        : newlineIndex;
    lines.push({
      start,
      end: newlineIndex + 1,
      text: value.slice(start, textEnd),
    });
    start = newlineIndex + 1;
  }

  return lines;
}

function openingFenceLanguage(line: string): string | undefined | null {
  const match = line.match(/^[ \t]*```([^`]*)$/);
  if (!match) return null;
  const language = match[1].trim();
  return language || undefined;
}

function isClosingFence(line: string): boolean {
  return /^[ \t]*```\s*$/.test(line);
}

/**
 * Find only paired fenced blocks. An opening fence without a later closing
 * fence is deliberately ignored so an in-progress ` ``` ` remains ordinary
 * text until the user closes it.
 */
function findInternalCodeRanges(value: string): InternalCodeRange[] {
  const lines = splitLines(value);
  const ranges: InternalCodeRange[] = [];

  for (let lineIndex = 0; lineIndex < lines.length; lineIndex += 1) {
    const language = openingFenceLanguage(lines[lineIndex].text);
    if (language === null) continue;

    const openingLine = lines[lineIndex];
    let closingLineIndex = -1;
    for (
      let candidate = lineIndex + 1;
      candidate < lines.length;
      candidate += 1
    ) {
      if (isClosingFence(lines[candidate].text)) {
        closingLineIndex = candidate;
        break;
      }
    }
    if (closingLineIndex === -1) continue;

    const closingLine = lines[closingLineIndex];
    const contentStart = openingLine.end;
    // The line break before the closing fence is a separator, not code
    // content. This handles both LF and CRLF values.
    const separatorLength =
      closingLine.start >= 2 &&
      value.slice(closingLine.start - 2, closingLine.start) === "\r\n"
        ? 2
        : value[closingLine.start - 1] === "\n"
          ? 1
          : 0;
    const contentEnd = Math.max(
      contentStart,
      closingLine.start - separatorLength,
    );

    ranges.push({
      contentStart,
      contentEnd,
      blockStart: openingLine.start,
      blockEnd: closingLine.end,
      openingLineEnd: openingLine.end,
      closingLineStart: closingLine.start,
      closingLineEnd: closingLine.end,
      ...(language ? { language } : {}),
    });

    // A closing fence cannot also be the opening fence of the same range.
    // Skip past it so nested-looking fences are paired in source order.
    lineIndex = closingLineIndex;
  }

  return ranges;
}

/**
 * Return paired fenced-code ranges in a composer value.
 *
 * Opening/closing lines are excluded from the returned content offsets. The
 * returned objects intentionally contain only stable public offsets and the
 * optional language; line bookkeeping used by cursor classification remains
 * private.
 */
export function findChatComposerCodeRanges(
  value: string,
): ChatComposerCodeRange[] {
  return findInternalCodeRanges(value).map(
    ({
      contentStart,
      contentEnd,
      blockStart,
      blockEnd,
      language,
    }) => ({
      contentStart,
      contentEnd,
      blockStart,
      blockEnd,
      ...(language ? { language } : {}),
    }),
  );
}

/**
 * Whether a textarea caret is on a paired code block's content line.
 *
 * Fence lines are explicitly excluded. Positions are UTF-16 offsets and the
 * end of a content line is considered inside the block, which matches native
 * textarea caret behavior. Unmatched opening fences never produce a range.
 */
export function isChatComposerCursorInCodeBlock(
  value: string,
  cursor: number,
): boolean {
  if (!Number.isFinite(cursor) || cursor < 0 || cursor > value.length) {
    return false;
  }

  return findInternalCodeRanges(value).some((range) => {
    // Check the closing line first: for an empty block its start can equal the
    // content start, but that position is still the closing fence line.
    if (
      cursor >= range.closingLineStart &&
      cursor <= range.closingLineEnd
    ) {
      return false;
    }
    if (cursor < range.openingLineEnd) return false;
    return cursor >= range.contentStart && cursor <= range.contentEnd;
  });
}
